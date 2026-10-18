from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from runway.platforms.base import TokenSet
from runway.platforms.tokens import TokenStore, TokenStoreError


def _token() -> TokenSet:
    return TokenSet(
        access_token="access-abc",
        refresh_token="refresh-def",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["scope.one", "scope.two"],
        extra={"channel_id": "UC123"},
    )


def test_roundtrip_restores_token_and_creates_key_file(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    ref = store.new_token_ref()
    store.store(ref, _token())

    assert store.key_path.is_file()
    assert store.store_path.is_file()
    loaded = store.load(ref)
    assert loaded is not None
    assert loaded.access_token == "access-abc"
    assert loaded.refresh_token == "refresh-def"
    assert loaded.scopes == ["scope.one", "scope.two"]
    assert loaded.extra["channel_id"] == "UC123"
    # The stored blob is encrypted, not plaintext JSON.
    assert b"access-abc" not in store.store_path.read_bytes()


def test_tampered_blob_raises_clear_error(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    ref = store.new_token_ref()
    store.store(ref, _token())
    blob = bytearray(store.store_path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    store.store_path.write_bytes(bytes(blob))

    with pytest.raises(TokenStoreError):
        store.load(ref)


def test_missing_key_with_existing_blob_raises_clear_error(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    ref = store.new_token_ref()
    store.store(ref, _token())
    store.key_path.unlink()

    with pytest.raises(TokenStoreError, match="key file is missing"):
        store.load(ref)


def test_blob_written_with_other_key_raises(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True)
    secrets_dir.joinpath("platform-tokens.json").write_bytes(
        Fernet(Fernet.generate_key()).encrypt(b'{"ref": {"access_token": "x"}}')
    )
    store = TokenStore(secrets_dir)
    store.key_path.write_bytes(Fernet.generate_key() + b"\n")

    with pytest.raises(TokenStoreError):
        store.load("ref")


def test_delete_removes_entry(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    ref = store.new_token_ref()
    store.store(ref, _token())
    store.delete(ref)
    assert store.load(ref) is None
    store.delete(ref)  # deleting a missing ref is a no-op


def test_load_missing_ref_returns_none(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    assert store.load("does-not-exist") is None
