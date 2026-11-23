from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from runway.locks import LockTimeout, exclusive_file_lock
from runway.platforms.base import TokenSet, deserialize_token, serialize_token


class TokenStoreError(Exception):
    pass


class TokenRefreshBusy(TokenStoreError):
    """Another thread or process held the account's refresh lock for too long."""


_TOKEN_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Longer than one provider round trip (20 s client timeout) plus the store write.
REFRESH_LOCK_TIMEOUT_SECONDS = 90.0


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
        self.refresh_lock_dir = secrets_dir / "refresh-locks"

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
            with exclusive_file_lock(self.refresh_lock_dir / f"{token_ref}.lock", timeout=timeout):
                yield
        except LockTimeout as exc:
            raise TokenRefreshBusy(str(exc)) from exc

    def cipher(self) -> Fernet:
        """The store's Fernet key, for other small secrets (webhook signing secrets)."""
        with self._locked():
            return Fernet(self._load_key())

    def _load_key(self) -> bytes:
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
        with self._locked():
            fernet = Fernet(self._load_key())
            payload = self._read_store(fernet)
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
