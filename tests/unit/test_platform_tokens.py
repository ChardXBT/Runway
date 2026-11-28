from __future__ import annotations

import os
import threading
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


def test_lock_is_reentrant_for_composed_token_operations(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    ref = store.new_token_ref()

    with store.locked():
        store.store(ref, _token())
        loaded = store.load(ref)

    assert loaded is not None
    assert loaded.access_token == "access-abc"


def test_load_missing_ref_returns_none(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    assert store.load("does-not-exist") is None


def _quarantine_dirs(secrets_dir: Path) -> list[Path]:
    root = secrets_dir / "quarantine"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _tamper_store_blob(store: TokenStore) -> bytes:
    blob = bytearray(store.store_path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    tampered = bytes(blob)
    store.store_path.write_bytes(tampered)
    return tampered


def test_store_quarantines_tampered_blob_and_writes_healthy_replacement(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    original_ref = store.new_token_ref()
    store.store(original_ref, _token())
    original_key = store.key_path.read_bytes()
    tampered_blob = _tamper_store_blob(store)

    replacement_ref = store.new_token_ref()
    store.store(replacement_ref, TokenSet(access_token="replacement-access"))

    quarantined = _quarantine_dirs(secrets_dir)
    assert len(quarantined) == 1
    assert (quarantined[0] / "platform-tokens.json").read_bytes() == tampered_blob
    assert (quarantined[0] / "token.key").read_bytes() == original_key
    if os.name == "posix":
        assert quarantined[0].stat().st_mode & 0o777 == 0o700
        assert (quarantined[0] / "token.key").stat().st_mode & 0o777 == 0o600
    manifest = (quarantined[0] / "recovery.json").read_text(encoding="utf-8")
    assert "unreadable" in manifest
    assert "replacement-access" not in manifest
    assert "access-abc" not in manifest

    replacement = store.load(replacement_ref)
    assert replacement is not None
    assert replacement.access_token == "replacement-access"
    assert store.load(original_ref) is None
    assert store.key_path.read_bytes() != original_key
    assert b"replacement-access" not in store.store_path.read_bytes()
    assert b"access-abc" not in (quarantined[0] / "platform-tokens.json").read_bytes()
    # Recovery is idempotent once the replacement store is healthy.
    assert store.quarantine_unreadable() is None
    assert len(_quarantine_dirs(secrets_dir)) == 1


def test_store_quarantines_blob_when_key_is_missing(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    original_blob = store.store_path.read_bytes()
    store.key_path.unlink()

    replacement_ref = store.new_token_ref()
    store.store(replacement_ref, TokenSet(access_token="replacement-after-key-loss"))

    quarantined = _quarantine_dirs(secrets_dir)
    assert len(quarantined) == 1
    assert (quarantined[0] / "platform-tokens.json").read_bytes() == original_blob
    assert not (quarantined[0] / "token.key").exists()
    replacement = store.load(replacement_ref)
    assert replacement is not None
    assert replacement.access_token == "replacement-after-key-loss"


def test_store_quarantines_blob_and_wrong_key_together(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    original_blob = store.store_path.read_bytes()
    wrong_key = Fernet.generate_key() + b"\n"
    store.key_path.write_bytes(wrong_key)

    replacement_ref = store.new_token_ref()
    store.store(replacement_ref, TokenSet(access_token="replacement-after-wrong-key"))

    quarantined = _quarantine_dirs(secrets_dir)
    assert len(quarantined) == 1
    assert (quarantined[0] / "platform-tokens.json").read_bytes() == original_blob
    assert (quarantined[0] / "token.key").read_bytes() == wrong_key
    replacement = store.load(replacement_ref)
    assert replacement is not None
    assert replacement.access_token == "replacement-after-wrong-key"


def test_explicit_quarantine_reports_recovery_before_replacement_write(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    original_key = store.key_path.read_bytes()
    tampered_blob = _tamper_store_blob(store)

    recovery = store.quarantine_unreadable()

    assert recovery is not None
    assert recovery.store_preserved is True
    assert recovery.key_preserved is True
    assert "unreadable" in recovery.reason
    assert recovery.quarantine_dir == _quarantine_dirs(secrets_dir)[0]
    assert (recovery.quarantine_dir / "platform-tokens.json").read_bytes() == tampered_blob
    assert (recovery.quarantine_dir / "token.key").read_bytes() == original_key
    assert not store.store_path.exists()
    assert not store.key_path.exists()

    replacement_ref = store.new_token_ref()
    store.store(replacement_ref, TokenSet(access_token="replacement-after-explicit-recovery"))
    assert store.load(replacement_ref) is not None


def test_repeated_store_recovery_attempts_do_not_repeat_quarantine(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    _tamper_store_blob(store)

    first_ref = store.new_token_ref()
    second_ref = store.new_token_ref()
    store.store(first_ref, TokenSet(access_token="replacement-one"))
    store.store(second_ref, TokenSet(access_token="replacement-two"))

    assert len(_quarantine_dirs(secrets_dir)) == 1
    assert store.load(first_ref) is not None
    assert store.load(second_ref) is not None


def test_repeated_disasters_create_distinct_quarantines(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())

    first_tampered = _tamper_store_blob(store)
    first_ref = store.new_token_ref()
    store.store(first_ref, TokenSet(access_token="replacement-one"))
    second_tampered = _tamper_store_blob(store)
    second_ref = store.new_token_ref()
    store.store(second_ref, TokenSet(access_token="replacement-two"))

    quarantined = _quarantine_dirs(secrets_dir)
    assert len(quarantined) == 2
    preserved = {(path / "platform-tokens.json").read_bytes() for path in quarantined}
    assert preserved == {first_tampered, second_tampered}
    assert all((path / "token.key").is_file() for path in quarantined)
    loaded = store.load(second_ref)
    assert loaded is not None
    assert loaded.access_token == "replacement-two"


def test_concurrent_store_access_recovers_once_and_preserves_all_replacements(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    _tamper_store_blob(store)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []
    refs = [store.new_token_ref() for _ in range(4)]

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.store(refs[index], TokenSet(access_token=f"replacement-{index}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(_quarantine_dirs(secrets_dir)) == 1
    for index, ref in enumerate(refs):
        loaded = store.load(ref)
        assert loaded is not None
        assert loaded.access_token == f"replacement-{index}"


def test_partial_quarantine_move_failure_rolls_back_active_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    original_key = store.key_path.read_bytes()
    tampered_blob = _tamper_store_blob(store)
    original_replace = os.replace

    def fail_key_move(source, destination):
        if Path(source) == store.key_path and Path(destination).name == "token.key":
            raise OSError("simulated key move failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_key_move)

    with pytest.raises(OSError, match="simulated key move failure"):
        store.quarantine_unreadable()

    assert store.store_path.read_bytes() == tampered_blob
    assert store.key_path.read_bytes() == original_key
    assert _quarantine_dirs(secrets_dir) == []


def test_explicit_quarantine_leaves_a_healthy_store_untouched(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    ref = store.new_token_ref()
    store.store(ref, _token())
    active_blob = store.store_path.read_bytes()
    active_key = store.key_path.read_bytes()

    assert store.quarantine_unreadable() is None

    assert store.store_path.read_bytes() == active_blob
    assert store.key_path.read_bytes() == active_key
    assert _quarantine_dirs(secrets_dir) == []
    assert store.load(ref) is not None


def test_empty_store_blob_is_treated_as_unreadable_and_quarantined(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = TokenStore(secrets_dir)
    store.store(store.new_token_ref(), _token())
    original_key = store.key_path.read_bytes()
    store.store_path.write_bytes(b"")

    replacement_ref = store.new_token_ref()
    store.store(replacement_ref, TokenSet(access_token="replacement-after-empty-blob"))

    quarantined = _quarantine_dirs(secrets_dir)
    assert len(quarantined) == 1
    assert (quarantined[0] / "platform-tokens.json").read_bytes() == b""
    assert (quarantined[0] / "token.key").read_bytes() == original_key
    assert store.load(replacement_ref) is not None
