from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from runway.platforms.base import TokenSet, deserialize_token, serialize_token


class TokenStoreError(Exception):
    pass


@dataclass(frozen=True)
class TokenStoreRecovery:
    """Secret-free description of quarantined encrypted token-store material."""

    quarantine_dir: Path
    store_preserved: bool
    key_preserved: bool
    reason: str


class TokenStore:
    """Fernet-encrypted token persistence under ``data/secrets/``.

    The plaintext payload is a JSON object keyed by ``token_ref``. The Fernet key
    lives in ``token.key`` next to the encrypted ``platform-tokens.json`` blob.
    """

    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = secrets_dir
        self.key_path = secrets_dir / "token.key"
        self.store_path = secrets_dir / "platform-tokens.json"
        self.lock_path = secrets_dir / "token.lock"
        self._lock_state = threading.local()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize token-store operations across threads and processes.

        ``os.replace`` keeps each write atomic for readers, but without this
        lock two processes can read the same blob, mutate different keys, and
        silently overwrite each other's token updates. A sidecar
        ``token.lock`` file is held exclusively for the whole sequence. The
        lock is reentrant for one thread so higher-level token operations can
        compose the public load/store methods without self-deadlocking.
        """
        depth = getattr(self._lock_state, "depth", 0)
        if depth:
            self._lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._lock_state.depth = depth
            return

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                # LK_LOCK gives up after about ten seconds, but a refreshing
                # peer can legitimately hold this lock through a network call.
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
                self._lock_state.depth = 1
                try:
                    yield
                finally:
                    self._lock_state.depth = 0
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._lock_state.depth = 1
                try:
                    yield
                finally:
                    self._lock_state.depth = 0
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_key(self) -> bytes:
        if self.key_path.is_file():
            key = self.key_path.read_bytes().strip()
            if key:
                return key
        if self.store_path.is_file() and self.store_path.stat().st_size > 0:
            raise TokenStoreError(
                "platform token key file is missing while an encrypted token "
                "store exists; restore data/secrets/token.key for recovery or "
                "reconnect accounts so the unreadable store can be quarantined"
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

    def _fernet(self) -> Fernet:
        try:
            return Fernet(self._load_key())
        except ValueError as exc:
            raise TokenStoreError("platform token key file is malformed") from exc

    def _unreadable_reason(self) -> str | None:
        """Classify unreadable encrypted-store material without exposing its contents."""
        if not self.store_path.is_file():
            return None
        blob = self.store_path.read_bytes().strip()
        if not blob:
            return "unreadable empty encrypted token store"
        key = self.key_path.read_bytes().strip() if self.key_path.is_file() else b""
        if not key:
            return "unreadable encrypted token store with a missing key"
        try:
            self._read_store(self._fernet())
        except TokenStoreError:
            return "unreadable encrypted token store"
        return None

    def _quarantine_unreadable_locked(self) -> TokenStoreRecovery | None:
        """Move unreadable encrypted material aside while holding the store lock.

        Recovery must never silently discard potentially decryptable ciphertext or
        key material. The active filenames are moved into a unique quarantine
        directory, still inside the ignored ``data/secrets`` tree, and a small
        secret-free manifest records why they are there. The caller can then
        create a fresh key and empty store for reauthorization.
        """
        reason = self._unreadable_reason()
        if reason is None:
            return None

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        quarantine_root = self.secrets_dir / "quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(quarantine_root, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        quarantine_dir = quarantine_root / f"token-store-{stamp}-{secrets.token_hex(4)}"
        quarantine_dir.mkdir(mode=0o700)
        if sys.platform != "win32":
            os.chmod(quarantine_dir, 0o700)

        store_preserved = False
        key_preserved = False
        moved: list[tuple[Path, Path]] = []
        try:
            if self.store_path.exists():
                destination = quarantine_dir / self.store_path.name
                os.replace(self.store_path, destination)
                moved.append((self.store_path, destination))
                store_preserved = True
                if sys.platform != "win32":
                    os.chmod(destination, 0o600)
            if self.key_path.exists():
                destination = quarantine_dir / self.key_path.name
                os.replace(self.key_path, destination)
                moved.append((self.key_path, destination))
                key_preserved = True
                if sys.platform != "win32":
                    os.chmod(destination, 0o600)
        except BaseException:
            # A partial move must not strand the active store in a worse state.
            # Roll back what can be rolled back; anything that cannot be moved
            # back remains preserved in quarantine rather than being deleted.
            for source, destination in reversed(moved):
                with suppress(OSError):
                    os.replace(destination, source)
            with suppress(OSError):
                quarantine_dir.rmdir()
            raise

        manifest = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "reason": reason,
            "store_preserved": store_preserved,
            "key_preserved": key_preserved,
            "note": (
                "Encrypted token-store material quarantined for owner recovery; "
                "no plaintext tokens are stored in this manifest."
            ),
        }
        fd, temp_name = tempfile.mkstemp(dir=quarantine_dir, prefix="recovery-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            os.replace(temp_name, quarantine_dir / "recovery.json")
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return TokenStoreRecovery(
            quarantine_dir=quarantine_dir,
            store_preserved=store_preserved,
            key_preserved=key_preserved,
            reason=reason,
        )

    def quarantine_unreadable(self) -> TokenStoreRecovery | None:
        """Quarantine a proven-unreadable encrypted store, or return ``None``.

        Read-only token loads never mutate the store. Operators and callers that
        need recovery can invoke this explicitly, while token mutations use it
        internally before writing replacement credentials.
        """
        with self.locked():
            return self._quarantine_unreadable_locked()

    def _read_for_update(self) -> tuple[Fernet, dict[str, Any]]:
        """Read the monolithic payload for a mutation, recovering if impossible."""
        try:
            fernet = self._fernet()
            return fernet, self._read_store(fernet)
        except TokenStoreError:
            recovery = self._quarantine_unreadable_locked()
            if recovery is None:
                raise
        fernet = self._fernet()
        return fernet, self._read_store(fernet)

    def _read_store(self, fernet: Fernet) -> dict[str, Any]:
        if not self.store_path.is_file():
            return {}
        blob = self.store_path.read_bytes().strip()
        if not blob:
            raise TokenStoreError("platform token store is empty or incomplete")
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
        with self.locked():
            fernet, payload = self._read_for_update()
            payload[token_ref] = serialize_token(token)
            self._write_store(fernet, payload)

    def load(self, token_ref: str) -> TokenSet | None:
        with self.locked():
            fernet = self._fernet()
            payload = self._read_store(fernet)
            entry = payload.get(token_ref)
            if entry is None:
                return None
            if not isinstance(entry, dict):
                raise TokenStoreError(f"token entry {token_ref!r} is malformed")
            return deserialize_token(entry)

    def delete(self, token_ref: str) -> None:
        with self.locked():
            fernet, payload = self._read_for_update()
            if token_ref in payload:
                del payload[token_ref]
                self._write_store(fernet, payload)

    def new_token_ref(self) -> str:
        return secrets.token_hex(16)


def pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
