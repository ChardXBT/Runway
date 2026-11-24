"""`account_tokens` and the database token store (spec 05 section 3).

The store is keyed by ``connected_accounts.id``: a ``token_ref`` resolves to
exactly one account row, so a stored grant can never cross a workspace line.
Ciphertext is AES-256-GCM with ``<workspace>:<account>:<key_version>`` as
authenticated additional data - a row copied to another account or workspace
fails the tag check instead of decrypting to the wrong owner's grant.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken
from pydantic import SecretStr, ValidationError

from runway.config import Settings
from runway.db.base import Database
from runway.db.bootstrap import initialize_database
from runway.db.models import ConnectedAccount, OAuthState
from runway.platforms.base import TokenSet
from runway.platforms.tokens import (
    DatabaseTokenStore,
    FileTokenStore,
    TokenRefreshBusy,
    TokenStore,
    TokenStoreError,
    parse_token_keys,
    token_store_from_settings,
)

KEY_1 = base64.urlsafe_b64encode(b"a" * 32).decode("ascii")
KEY_2 = base64.urlsafe_b64encode(b"b" * 32).decode("ascii")
RING_1 = f"v1:{KEY_1}"
RING_2_1 = f"v2:{KEY_2},v1:{KEY_1}"


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    base: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "token_store": "database",
        "token_keys": SecretStr(RING_1),
    }
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture()
def db_store(tmp_path: Path):
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    store = token_store_from_settings(settings, database)
    yield settings, database, store
    database.engine.dispose()


def _token() -> TokenSet:
    return TokenSet(
        access_token="access-abc",
        refresh_token="refresh-def",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["scope.one", "scope.two"],
        extra={"channel_id": "UC123", "login": "unified"},
    )


def _account(database: Database, token_ref: str, **fields: object) -> int:
    account = ConnectedAccount(
        platform="youtube",
        external_account_id=f"ext-{token_ref[:8]}",
        status="connected",
        token_ref=token_ref,
        **fields,
    )
    with database.session() as session:
        session.add(account)
        session.flush()
        return account.id


def _row(database: Database, account_id: int) -> sqlite3.Row:

    from runway.db.models import AccountToken

    with database.session() as session:
        row = session.get(AccountToken, account_id)
        assert row is not None
        session.expunge(row)
        return row


# ---------------------------------------------------------------- selection


def test_factory_uses_the_file_store_by_default(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    store = token_store_from_settings(settings, database)
    assert isinstance(store, FileTokenStore)
    database.engine.dispose()


def test_factory_uses_the_database_store_when_opted_in(db_store) -> None:
    _settings, _database, store = db_store
    assert isinstance(store, DatabaseTokenStore)


def test_hosted_resolves_to_the_database_store(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"profile": "hosted"})
    assert settings.resolved_token_store == "database"
    local = _settings(tmp_path / "two", token_store=None, token_keys=None)
    assert local.resolved_token_store == "file"


def test_the_protocol_covers_every_store(db_store) -> None:
    settings, _database, db = db_store
    for store in (db, FileTokenStore(settings.resolved_data_dir / "x")):
        assert isinstance(store, TokenStore)


# ------------------------------------------------------------------ keyring


def test_parse_token_keys_accepts_the_documented_format() -> None:
    keys = parse_token_keys(RING_2_1)
    assert [key.version for key in keys] == ["v2", "v1"]
    assert keys[0].key == b"b" * 32
    # First key encrypts, every key may decrypt; versions never repeat.
    assert parse_token_keys(RING_1)[0].version == "v1"


@pytest.mark.parametrize(
    "raw",
    [
        "v1:not-base64!!",
        "v1:" + base64.urlsafe_b64encode(b"too short").decode(),
        "v1:" + KEY_1 + ",v1:" + KEY_2,
        "",
        "  ",
        "v1",
    ],
)
def test_parse_token_keys_rejects_malformed_rings_without_leaking(raw: str) -> None:
    with pytest.raises(TokenStoreError) as raised:
        parse_token_keys(raw)
    # A validation error names versions, never key material.
    assert KEY_1 not in str(raised.value) and KEY_2 not in str(raised.value)


def test_settings_rejects_a_malformed_keyring(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="RUNWAY_TOKEN_KEYS"):
        Settings(
            data_dir=tmp_path / "data",
            token_store="database",
            token_keys=SecretStr("v1:not-a-32-byte-key"),
        )


def test_missing_keyring_fails_when_the_store_is_used(tmp_path: Path) -> None:
    # Settings() refuses the combination outright; the model-copy path (used
    # by tests and by `model_copy` call sites) still raises at the factory.
    with pytest.raises(ValidationError, match="RUNWAY_TOKEN_KEYS"):
        Settings(data_dir=tmp_path / "data", token_store="database", token_keys=None)
    settings = Settings(data_dir=tmp_path / "data").model_copy(update={"token_store": "database"})
    database = initialize_database(settings)
    with pytest.raises(TokenStoreError, match="RUNWAY_TOKEN_KEYS"):
        token_store_from_settings(settings, database)
    database.engine.dispose()


# ----------------------------------------------------------------- roundtrip


def test_store_load_delete_roundtrip(db_store) -> None:
    _settings, database, store = db_store
    token_ref = store.new_token_ref()
    account_id = _account(database, token_ref)

    store.store(token_ref, _token())
    loaded = store.load(token_ref)
    assert loaded is not None
    assert loaded.access_token == "access-abc"
    assert loaded.refresh_token == "refresh-def"
    assert loaded.scopes == ["scope.one", "scope.two"]
    assert loaded.extra["channel_id"] == "UC123"

    row = _row(database, account_id)
    assert row.account_id == account_id
    assert row.workspace_id == 1
    assert row.key_version == "v1"
    # The plaintext grant never reaches the row.
    assert b"access-abc" not in row.ciphertext
    assert b"refresh-def" not in row.ciphertext

    store.delete(token_ref)
    assert store.load(token_ref) is None
    store.delete(token_ref)  # idempotent


def test_storing_under_an_unbound_ref_is_refused(db_store) -> None:
    _settings, _database, store = db_store
    with pytest.raises(TokenStoreError, match="not bound"):
        store.store(store.new_token_ref(), _token())


def test_a_rekey_roundtrip_keeps_old_keys_decrypting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    store = token_store_from_settings(settings, database)
    token_ref = store.new_token_ref()
    _account(database, token_ref)
    store.store(token_ref, _token())

    # Rotation: the new keyring encrypts with v2 but still reads v1 rows.
    rotated = token_store_from_settings(
        settings.model_copy(update={"token_keys": SecretStr(RING_2_1)}), database
    )
    loaded = rotated.load(token_ref)
    assert loaded is not None and loaded.access_token == "access-abc"

    # Rewrap moves every row to the newest key, idempotently.
    report = rotated.rewrap()
    assert report["rewrapped"] == 1 and report["checked"] == 1
    assert rotated.rewrap() == {"checked": 1, "rewrapped": 0, "undecryptable": 0}
    assert _row(database, _account_id(database, token_ref)).key_version == "v2"

    # A keyring that has forgotten v1 can no longer read a v1 row.
    stale = token_store_from_settings(
        settings.model_copy(update={"token_keys": SecretStr(f"v2:{KEY_2}")}), database
    )
    store.store(token_ref, _token())  # still v1-encrypted? no: store writes v2
    token_ref2 = stale.new_token_ref()
    _account(database, token_ref2)
    # Write with the rotated ring (v2), then read with a v1-only ring: refused.
    rotated.store(token_ref2, _token())
    old_ring = token_store_from_settings(settings, database)
    with pytest.raises(TokenStoreError, match="v2"):
        old_ring.load(token_ref2)
    database.engine.dispose()


def test_moved_ciphertext_fails_the_tag_check(db_store) -> None:
    """Ciphertext transplanted to another account must not decrypt (AAD)."""
    _settings, database, store = db_store
    from runway.db.models import AccountToken

    ref_a, ref_b = store.new_token_ref(), store.new_token_ref()
    id_a = _account(database, ref_a)
    id_b = _account(database, ref_b)
    store.store(ref_a, _token())
    store.store(ref_b, _token())
    with database.session() as session:
        row_a = session.get(AccountToken, id_a)
        row_b = session.get(AccountToken, id_b)
        row_b.ciphertext = row_a.ciphertext
        row_b.nonce = row_a.nonce
        row_b.key_version = row_a.key_version
    with pytest.raises(TokenStoreError):
        store.load(ref_b)
    # Same for a row pointed at another workspace.
    from runway.db.models import Workspace

    with database.session() as session:
        session.add(Workspace(slug="other", name="Other"))
    with database.session() as session:
        row_a = session.get(AccountToken, id_a)
        row_a.workspace_id = 2
    with pytest.raises(TokenStoreError):
        store.load(ref_a)


def test_cipher_rotates_with_the_keyring(tmp_path: Path) -> None:
    """``cipher()`` covers the shared secret helper (webhook signing secrets)."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    cipher_v1 = token_store_from_settings(settings, database).cipher()
    blob = cipher_v1.encrypt(b"webhook-secret")

    rotated = token_store_from_settings(
        settings.model_copy(update={"token_keys": SecretStr(RING_2_1)}), database
    ).cipher()
    # Old ciphertext still opens; new ciphertext is minted under the newest key.
    assert rotated.decrypt(blob) == b"webhook-secret"
    with pytest.raises(InvalidToken):
        token_store_from_settings(settings, database).cipher().decrypt(rotated.encrypt(b"fresh"))
    database.engine.dispose()


def test_refresh_lock_serialize_and_expire(db_store) -> None:
    _settings, _database, store = db_store
    with (
        store.refresh_lock("ref-abc"),
        pytest.raises(TokenRefreshBusy),
        store.refresh_lock("ref-abc", timeout=0.05),
    ):
        pass
    with store.refresh_lock("ref-abc", timeout=1):
        pass


def test_oauth_state_can_park_a_ciphertext(db_store) -> None:
    """The unified-select flow parks a token encrypted under cipher() on the state row."""
    _settings, database, store = db_store
    cipher = store.cipher()
    state = OAuthState(
        platform="instagram",
        state="state-1",
        redirect_uri="http://127.0.0.1/callback",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    state.token_ciphertext = cipher.encrypt(b"serialized-token").decode("ascii")
    with database.session() as session:
        session.add(state)
    with database.session() as session:
        row = session.query(OAuthState).filter_by(state="state-1").one()
        assert cipher.decrypt(row.token_ciphertext.encode("ascii")) == b"serialized-token"


def test_hosted_requires_a_keyring(tmp_path: Path) -> None:
    """RUNWAY_PROFILE=hosted lists RUNWAY_TOKEN_KEYS among its startup requirements."""
    with pytest.raises(ValidationError) as raised:
        Settings(data_dir=tmp_path / "data", profile="hosted")
    assert "RUNWAY_TOKEN_KEYS" in str(raised.value)


# ---------------------------------------------------------------------- cli


def test_tokens_rewrap_cli_reencrypts_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import runway.cli.tokens_commands as tokens_cli
    from runway.cli.tokens_commands import tokens_app

    settings = _settings(tmp_path)
    database = initialize_database(settings)
    store = token_store_from_settings(settings, database)
    token_ref = store.new_token_ref()
    account_id = _account(database, token_ref)
    store.store(token_ref, _token())
    database.engine.dispose()

    rotated = settings.model_copy(update={"token_keys": SecretStr(RING_2_1)})
    monkeypatch.setattr(tokens_cli, "get_settings", lambda: rotated)
    result = CliRunner().invoke(tokens_app, ["rewrap"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == {"checked": 1, "rewrapped": 1, "undecryptable": 0}
    database = Database(rotated)
    assert _row(database, account_id).key_version == "v2"
    database.engine.dispose()


def test_tokens_migrate_file_to_db_copies_bound_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import runway.cli.tokens_commands as tokens_cli
    from runway.cli.tokens_commands import tokens_app

    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    file_store = FileTokenStore(settings.token_store_dir)
    bound_ref = file_store.new_token_ref()
    _account(database, bound_ref)
    file_store.store(bound_ref, _token())
    file_store.store(file_store.new_token_ref(), _token())  # unbound: skipped
    database.engine.dispose()

    opted_in = settings.model_copy(
        update={"token_store": "database", "token_keys": SecretStr(RING_1)}
    )
    monkeypatch.setattr(tokens_cli, "get_settings", lambda: opted_in)
    result = CliRunner().invoke(tokens_app, ["migrate-file-to-db"])
    # One migrated, one skipped -> nonzero exit and the file stays.
    assert result.exit_code == 1, result.output
    # stderr trails the JSON report (CliRunner merges the streams).
    report = json.loads(result.output[: result.output.rindex("}") + 1])
    assert report["migrated"] == 1 and report["skipped_unbound"] == 1
    assert report["deleted_file"] is False and file_store.store_path.is_file()
    database = Database(opted_in)
    db_store = token_store_from_settings(opted_in, database)
    loaded = db_store.load(bound_ref)
    assert loaded is not None and loaded.access_token == "access-abc"
    database.engine.dispose()


def _account_id(database: Database, token_ref: str) -> int:
    import sqlalchemy as sa

    with database.session() as session:
        row = session.scalar(
            sa.select(ConnectedAccount).where(ConnectedAccount.token_ref == token_ref)
        )
        assert row is not None
        return row.id
