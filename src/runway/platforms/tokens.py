"""Platform token persistence (spec 05 section 3).

``TokenStore`` is the protocol every caller codes against. Two
implementations exist:

- ``FileTokenStore`` - Fernet-encrypted JSON under ``data/secrets/``; the
  local-profile default.
- ``DatabaseTokenStore`` - AES-256-GCM rows in ``account_tokens``, one per
  connected account. Required in ``hosted``; a local owner opts in with
  ``RUNWAY_TOKEN_STORE=database`` plus a versioned ``RUNWAY_TOKEN_KEYS``
  ring (``"v2:<base64-32B>,v1:<base64-32B>"``). The first key encrypts,
  every listed key decrypts, so a rotation keeps reading the old rows until
  ``runway tokens rewrap`` re-encrypts them.

``token_store_from_settings`` picks the implementation; nothing else
branches on it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from runway.locks import LockTimeout, exclusive_file_lock
from runway.platforms.base import TokenSet, deserialize_token, serialize_token

if TYPE_CHECKING:
    from runway.config import Settings
    from runway.db.base import Database


class TokenStoreError(Exception):
    pass


class TokenRefreshBusy(TokenStoreError):
    """Another thread or process held the account's refresh lock for too long."""


_TOKEN_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Longer than one provider round trip (20 s client timeout) plus the store write.
REFRESH_LOCK_TIMEOUT_SECONDS = 90.0


# ------------------------------------------------------------------ keyring


@dataclass(frozen=True, slots=True)
class TokenKey:
    """One versioned 32-byte AES-256 key from ``RUNWAY_TOKEN_KEYS``."""

    version: str
    key: bytes = field(repr=False)


_TOKEN_KEY_ITEM = re.compile(r"^([A-Za-z0-9_-]{1,20}):([A-Za-z0-9+/=_-]+)$")


def parse_token_keys(raw: str | None) -> tuple[TokenKey, ...]:
    """Parse ``"v2:<base64>,v1:<base64>"`` into ordered keys.

    The first entry encrypts; every entry may decrypt, so a rotation list
    keeps old rows readable. A malformed ring is refused listing only the
    versions involved - key material never reaches an error message.
    """
    if raw is None or not raw.strip():
        raise TokenStoreError(
            "RUNWAY_TOKEN_KEYS is not set; the database token store needs "
            "'<version>:<base64-32-byte-key>' entries (openssl rand -base64 32)"
        )
    keys: list[TokenKey] = []
    seen: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        match = _TOKEN_KEY_ITEM.match(item)
        if match is None:
            raise TokenStoreError(
                "RUNWAY_TOKEN_KEYS entries must be '<version>:<base64-key>' "
                "(got an entry that does not match)"
            )
        version, encoded = match.groups()
        try:
            key = base64.b64decode(encoded, validate=True)
        except ValueError:
            # urlsafe base64 is accepted too; the strict alphabet failed first.
            try:
                key = base64.urlsafe_b64decode(encoded)
            except (ValueError, TypeError) as exc:
                raise TokenStoreError(f"RUNWAY_TOKEN_KEYS entry {version!r} is not base64") from exc
        if len(key) != 32:
            raise TokenStoreError(
                f"RUNWAY_TOKEN_KEYS entry {version!r} must decode to exactly 32 bytes"
            )
        if version in seen:
            raise TokenStoreError(f"RUNWAY_TOKEN_KEYS repeats key version {version!r}")
        seen.add(version)
        keys.append(TokenKey(version=version, key=key))
    if not keys:
        raise TokenStoreError("RUNWAY_TOKEN_KEYS is set but names no key versions")
    return tuple(keys)


# ------------------------------------------------------------------ protocol


@runtime_checkable
class TokenStore(Protocol):
    """The operations a token store owes its callers."""

    def new_token_ref(self) -> str: ...
    def store(self, token_ref: str, token: TokenSet) -> None: ...
    def load(self, token_ref: str) -> TokenSet | None: ...
    def delete(self, token_ref: str) -> None: ...
    def ensure_writable(self) -> None: ...
    def refresh_lock(
        self, token_ref: str, *, timeout: float = REFRESH_LOCK_TIMEOUT_SECONDS
    ) -> AbstractContextManager[None]: ...
    @property
    def refresh_lock_dir(self) -> Path: ...
    def cipher(self) -> Any:
        """A Fernet-compatible cipher for small secrets (webhook signing)."""
        ...


# --------------------------------------------------------------- file store


class FileTokenStore:
    """Fernet-encrypted token persistence under ``data/secrets/``.

    The plaintext payload is a JSON object keyed by ``token_ref``. The Fernet key
    lives in ``token.key`` next to the encrypted ``platform-tokens.json`` blob,
    unless a ``key`` is supplied (a private instance keeps it only in its
    encrypted secrets, so no plain key file is ever written).

    ``write_guard`` runs before every refresh and write; a private instance uses
    it to keep machines that do not hold the lease in standby.
    """

    def __init__(
        self,
        secrets_dir: Path,
        *,
        key: bytes | None = None,
        key_file: bool = True,
        write_guard: Callable[[], None] | None = None,
    ) -> None:
        self.secrets_dir = secrets_dir
        self.key_path = secrets_dir / "token.key"
        self.store_path = secrets_dir / "platform-tokens.json"
        self.lock_path = secrets_dir / "token.lock"
        self._refresh_lock_dir = secrets_dir / "refresh-locks"
        self._key = key
        self._key_file = key_file
        self._write_guard = write_guard

    @property
    def refresh_lock_dir(self) -> Path:
        return self._refresh_lock_dir

    @classmethod
    def from_settings(cls, settings: Settings) -> FileTokenStore:
        """The file store this installation uses: ``data/secrets`` or ``<instance>/tokens``."""
        if settings.resolved_instance_dir is None:
            return cls(settings.token_store_dir)
        from runway.operations.instance_lease import ensure_active

        key = settings.token_key.get_secret_value().encode("ascii") if settings.token_key else None
        return cls(
            settings.token_store_dir,
            key=key,
            key_file=False,
            write_guard=lambda: ensure_active(settings),
        )

    def ensure_writable(self) -> None:
        """Raise before a token refresh or write that this machine may not perform."""
        if self._write_guard is not None:
            self._write_guard()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-modify-write cycles across threads and processes.

        ``os.replace`` keeps each write atomic for readers, but without this
        lock two processes can read the same blob, mutate different keys, and
        silently overwrite each other's token updates. A sidecar
        ``token.lock`` file is held exclusively for the whole sequence.
        """
        with exclusive_file_lock(self.lock_path, timeout=None):
            yield

    @contextmanager
    def refresh_lock(
        self, token_ref: str, *, timeout: float = REFRESH_LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Serialize refreshing one account's token across threads and processes.

        Held for the whole refresh: re-read, call the token endpoint, store the
        new token set. Providers that rotate refresh tokens (TikTok) invalidate
        the old one on use, so a second concurrent refresh would lose the grant.
        """
        if not _TOKEN_REF_PATTERN.match(token_ref):
            raise TokenStoreError("token reference is not a safe lock name")
        try:
            with exclusive_file_lock(self._refresh_lock_dir / f"{token_ref}.lock", timeout=timeout):
                yield
        except LockTimeout as exc:
            raise TokenRefreshBusy(str(exc)) from exc

    def cipher(self) -> Fernet:
        """The store's Fernet key, for other small secrets (webhook signing secrets)."""
        with self._locked():
            return Fernet(self._load_key())

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        if not self._key_file:
            raise TokenStoreError(
                "the platform token key is not configured: add RUNWAY_TOKEN_KEY to the "
                "instance's secrets.enc.env ('runway instance init' creates one)"
            )
        if self.key_path.is_file():
            key = self.key_path.read_bytes().strip()
            if key:
                return key
        if self.store_path.is_file() and self.store_path.stat().st_size > 0:
            raise TokenStoreError(
                "platform token key file is missing while an encrypted token "
                "store exists; restore data/secrets/token.key or remove the store"
            )
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd, temp_name = tempfile.mkstemp(dir=self.secrets_dir, prefix="token-", suffix=".key")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key + b"\n")
            os.replace(temp_name, self.key_path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        if sys.platform != "win32":
            os.chmod(self.key_path, 0o600)
        return key

    def _read_store(self, fernet: Fernet) -> dict[str, Any]:
        if not self.store_path.is_file():
            return {}
        blob = self.store_path.read_bytes().strip()
        if not blob:
            return {}
        try:
            plaintext = fernet.decrypt(blob)
        except InvalidToken as exc:
            raise TokenStoreError(
                "platform token store cannot be decrypted: the blob is corrupt "
                "or was written with a different key"
            ) from exc
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TokenStoreError("platform token store is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise TokenStoreError("platform token store payload must be an object")
        return payload

    def _write_store(self, fernet: Fernet, payload: dict[str, Any]) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        blob = fernet.encrypt(json.dumps(payload).encode("utf-8"))
        fd, temp_name = tempfile.mkstemp(
            dir=self.secrets_dir, prefix="platform-tokens-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            os.replace(temp_name, self.store_path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def store(self, token_ref: str, token: TokenSet) -> None:
        self.ensure_writable()
        with self._locked():
            fernet = Fernet(self._load_key())
            payload = self._read_store(fernet)
            payload[token_ref] = serialize_token(token)
            self._write_store(fernet, payload)

    def load(self, token_ref: str) -> TokenSet | None:
        fernet = Fernet(self._load_key())
        payload = self._read_store(fernet)
        entry = payload.get(token_ref)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise TokenStoreError(f"token entry {token_ref!r} is malformed")
        return deserialize_token(entry)

    def delete(self, token_ref: str) -> None:
        self.ensure_writable()
        with self._locked():
            fernet = Fernet(self._load_key())
            payload = self._read_store(fernet)
            if token_ref in payload:
                del payload[token_ref]
                self._write_store(fernet, payload)

    def list_refs(self) -> list[str]:
        """Every stored ``token_ref``; `runway tokens migrate-file-to-db` uses it."""
        with self._locked():
            fernet = Fernet(self._load_key())
            return sorted(self._read_store(fernet))

    def new_token_ref(self) -> str:
        return secrets.token_hex(16)


# ----------------------------------------------------------- database store


class DatabaseTokenStore:
    """``account_tokens``: one AES-256-GCM grant row per connected account.

    ``token_ref`` resolves through ``connected_accounts``, so a grant can
    never be stored for or read into a different workspace. The GCM
    additional data is ``<workspace>:<account>:<key_version>``: a row copied
    to another account or workspace fails the tag check instead of
    decrypting.

    ``refresh_lock`` uses a Postgres advisory transaction lock when the
    database is Postgres (replica-safe), and the same per-ref file lock as
    the file store on SQLite.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        keys: Sequence[TokenKey],
        write_guard: Callable[[], None] | None = None,
    ) -> None:
        if not keys:
            raise TokenStoreError("the database token store needs at least one key")
        self.database = database
        self.settings = settings
        self._keys = tuple(keys)
        self._write_guard = write_guard
        self._refresh_lock_dir = settings.token_store_dir / "refresh-locks"

    @property
    def refresh_lock_dir(self) -> Path:
        return self._refresh_lock_dir

    @property
    def _primary(self) -> TokenKey:
        return self._keys[0]

    def ensure_writable(self) -> None:
        if self._write_guard is not None:
            self._write_guard()

    def new_token_ref(self) -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _aad(workspace_id: int, account_id: int, key_version: str) -> bytes:
        return f"{workspace_id}:{account_id}:{key_version}".encode("ascii")

    def _resolve_account(self, session: Any, token_ref: str) -> Any:
        from runway.db.models import ConnectedAccount

        return session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.token_ref == token_ref)
        )

    def _bound_account(self, session: Any, token_ref: str) -> Any:
        account = self._resolve_account(session, token_ref)
        if account is None:
            raise TokenStoreError(f"token ref {token_ref!r} is not bound to a connected account")
        return account

    def store(self, token_ref: str, token: TokenSet) -> None:
        """Upsert the grant row for the account ``token_ref`` resolves to."""
        self.ensure_writable()
        from runway.db.models import AccountToken, utcnow

        key = self._primary
        nonce = os.urandom(12)
        plaintext = json.dumps(serialize_token(token), sort_keys=True).encode("utf-8")
        scopes_json = json.dumps(list(token.scopes))
        refresh_expires_at = _parse_stored_datetime(token.extra.get("refresh_expires_at"))
        for attempt in (True, False):
            try:
                # Token upkeep, purges and reconnects reach refs owned by any
                # workspace; the ref -> account resolution is the guard.
                with self.database.system_session() as session:
                    account = self._bound_account(session, token_ref)
                    ciphertext = AESGCM(key.key).encrypt(
                        nonce, plaintext, self._aad(account.workspace_id, account.id, key.version)
                    )
                    row = session.get(AccountToken, account.id)
                    if row is None:
                        row = AccountToken(account_id=account.id)
                        session.add(row)
                    row.workspace_id = account.workspace_id
                    row.key_version = key.version
                    row.ciphertext = ciphertext
                    row.nonce = nonce
                    row.expires_at = token.expires_at
                    row.refresh_expires_at = refresh_expires_at
                    row.scopes_json = scopes_json
                    row.updated_at = utcnow()
                    return
            except IntegrityError:
                if not attempt:
                    raise
        return

    def load(self, token_ref: str) -> TokenSet | None:
        with self.database.system_session() as session:
            account = self._resolve_account(session, token_ref)
            if account is None:
                return None
            from runway.db.models import AccountToken

            row = session.get(AccountToken, account.id)
            if row is None:
                return None
            key = next((k for k in self._keys if k.version == row.key_version), None)
            if key is None:
                raise TokenStoreError(
                    f"the account token is encrypted under key version {row.key_version!r}, "
                    "which RUNWAY_TOKEN_KEYS no longer carries"
                )
            # The row's own workspace is AAD too: repointing the row (not just
            # the ciphertext) fails the tag check.
            aad = self._aad(row.workspace_id, row.account_id, row.key_version)
            try:
                plaintext = AESGCM(key.key).decrypt(row.nonce, row.ciphertext, aad)
            except Exception as exc:
                raise TokenStoreError(
                    "account token cannot be decrypted: the row is corrupt or "
                    "belongs to a different account"
                ) from exc
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TokenStoreError("account token payload is not valid JSON") from exc
        return deserialize_token(payload)

    def delete(self, token_ref: str) -> None:
        self.ensure_writable()
        from runway.db.models import AccountToken

        with self.database.system_session() as session:
            account = self._resolve_account(session, token_ref)
            if account is None:
                return
            row = session.get(AccountToken, account.id)
            if row is not None:
                session.delete(row)

    def cipher(self) -> MultiFernet:
        """Fernet over the keyring: encrypts with the newest, decrypts all.

        Webhook signing secrets share this helper, so they rotate with the
        token keys for free (``MultiFernet.rotate`` re-wraps a blob).
        """
        return MultiFernet([Fernet(base64.urlsafe_b64encode(entry.key)) for entry in self._keys])

    @contextmanager
    def refresh_lock(
        self, token_ref: str, *, timeout: float = REFRESH_LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Serialize refreshing one account's token across processes.

        Postgres uses ``pg_advisory_xact_lock`` under ``lock_timeout``, so the
        lock works across replicas and dies with the connection. SQLite (a
        local owner who opted in) falls back to the per-ref file lock.
        """
        if self.database.engine.url.get_backend_name() == "postgresql":
            lock_key = int.from_bytes(
                hashlib.sha256(token_ref.encode("utf-8")).digest()[:8], "big", signed=True
            )
            with self.database.system_session() as session:
                session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout * 1000)}ms'"))
                try:
                    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
                except OperationalError as exc:
                    raise TokenRefreshBusy(
                        f"another refresh holds the lock for {token_ref!r}"
                    ) from exc
                yield
            return
        if not _TOKEN_REF_PATTERN.match(token_ref):
            raise TokenStoreError("token reference is not a safe lock name")
        try:
            with exclusive_file_lock(self._refresh_lock_dir / f"{token_ref}.lock", timeout=timeout):
                yield
        except LockTimeout as exc:
            raise TokenRefreshBusy(str(exc)) from exc

    def rewrap(self, *, batch_size: int = 200) -> dict[str, int]:
        """Re-encrypt every row under the newest key while old keys still read.

        Runs in committed batches: a crash mid-rewrap leaves some rows on the
        old key, which stays in the ring, so a rerun just finishes the rest.
        """
        from runway.db.models import AccountToken

        checked = rewrapped = undecryptable = 0
        primary = self._primary
        cursor = 0
        while True:
            # Rewrap walks every workspace's rows: it is a maintenance sweep.
            with self.database.system_session() as session:
                rows = list(
                    session.scalars(
                        select(AccountToken)
                        .where(AccountToken.account_id > cursor)
                        .order_by(AccountToken.account_id)
                        .limit(batch_size)
                    )
                )
                for row in rows:
                    checked += 1
                    cursor = row.account_id
                    aad = self._aad(row.workspace_id, row.account_id, row.key_version)
                    current = next((k for k in self._keys if k.version == row.key_version), None)
                    if current is None:
                        undecryptable += 1
                        continue
                    try:
                        plaintext = AESGCM(current.key).decrypt(row.nonce, row.ciphertext, aad)
                    except Exception:
                        undecryptable += 1
                        continue
                    if row.key_version == primary.version:
                        continue
                    nonce = os.urandom(12)
                    row.ciphertext = AESGCM(primary.key).encrypt(
                        nonce,
                        plaintext,
                        self._aad(row.workspace_id, row.account_id, primary.version),
                    )
                    row.nonce = nonce
                    row.key_version = primary.version
                    rewrapped += 1
            if len(rows) < batch_size:
                break
        return {"checked": checked, "rewrapped": rewrapped, "undecryptable": undecryptable}


def _parse_stored_datetime(value: Any) -> Any:
    """``TokenSet.extra`` may carry ``refresh_expires_at`` (TikTok) as ISO text."""
    from datetime import datetime as _dt

    if isinstance(value, _dt):
        return value
    if isinstance(value, str):
        try:
            return _dt.fromisoformat(value)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ factory


def token_store_from_settings(settings: Settings, database: Database) -> TokenStore:
    """The one place that picks a store; callers never branch on the profile."""
    if settings.resolved_token_store == "database":
        raw_keys = (
            settings.token_keys.get_secret_value() if settings.token_keys is not None else None
        )
        write_guard = None
        if settings.resolved_instance_dir is not None:
            from runway.operations.instance_lease import ensure_active

            write_guard = lambda: ensure_active(settings)  # noqa: E731
        return DatabaseTokenStore(
            database,
            settings,
            keys=parse_token_keys(raw_keys),
            write_guard=write_guard,
        )
    return FileTokenStore.from_settings(settings)


def pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
