"""`runway tokens`: rotate keys and move grants between stores (spec 05 section 3).

- `rewrap` re-encrypts every `account_tokens` row under the newest
  `RUNWAY_TOKEN_KEYS` version; older versions must still be in the ring so
  the rows stay decryptable.
- `migrate-file-to-db` copies every grant from the local file store into
  `account_tokens` for a local owner opting into
  `RUNWAY_TOKEN_STORE=database`. Refs no account binds are reported and
  skipped; the file is only removed with `--delete-file` when nothing was
  skipped.
"""

from __future__ import annotations

import json

import typer
from sqlalchemy import select

from runway.config import Settings, get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.db.models import ConnectedAccount
from runway.platforms.tokens import (
    DatabaseTokenStore,
    FileTokenStore,
    TokenStoreError,
    token_store_from_settings,
)

tokens_app = typer.Typer(help="Rotate token keys and migrate grants between stores.")

EXIT_REFUSED = 2


def _fail(message: str, code: int = EXIT_REFUSED) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _settings_for_database_store(*, hosted_ok: bool = False) -> Settings:
    """Settings for a command that needs the database token store.

    `migrate-file-to-db` stays a local-owner command (hosted has no file
    store to migrate from); `rewrap` runs in hosted too, where the database
    store is the only one and the ring this rotates is the configured one.
    """
    settings = get_settings()
    if settings.profile == "hosted" and not hosted_ok:
        raise _fail("runway tokens is for a local owner; hosted manages its own keyring")
    if settings.resolved_token_store != "database":
        raise _fail(
            "the file token store is active; set RUNWAY_TOKEN_STORE=database "
            "and RUNWAY_TOKEN_KEYS first"
        )
    return settings


@tokens_app.command("rewrap")
def tokens_rewrap() -> None:
    """Re-encrypt every stored grant under the newest RUNWAY_TOKEN_KEYS version.

    Committed in batches: a crash leaves the rest on an older key, which is
    still in the ring, so a rerun simply finishes the job.
    """
    settings = _settings_for_database_store(hosted_ok=True)
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        raise _fail(str(exc), code=1) from exc
    database = Database(settings)
    try:
        store = token_store_from_settings(settings, database)
        assert isinstance(store, DatabaseTokenStore)
        report = store.rewrap()
    finally:
        database.engine.dispose()
    typer.echo(json.dumps(report, indent=2))
    if report["undecryptable"]:
        raise _fail(
            f"{report['undecryptable']} row(s) could not be decrypted under "
            "RUNWAY_TOKEN_KEYS; restore the missing key version and rerun",
            code=1,
        )


@tokens_app.command("migrate-file-to-db")
def tokens_migrate_file_to_db(
    delete_file: bool = typer.Option(
        False,
        "--delete-file",
        help="Remove platform-tokens.json and token.key after every ref migrated.",
    ),
) -> None:
    """Copy every grant the file store holds into `account_tokens` (idempotent)."""
    settings = _settings_for_database_store()
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        raise _fail(str(exc), code=1) from exc
    database = Database(settings)
    try:
        file_store = FileTokenStore.from_settings(settings)
        db_store = token_store_from_settings(settings, database)
        assert isinstance(db_store, DatabaseTokenStore)
        # Every bound ref across all workspaces, not one tenant's set.
        with database.system_session() as session:
            bound_refs = {
                str(ref)
                for (ref,) in session.execute(
                    select(ConnectedAccount.token_ref).where(
                        ConnectedAccount.token_ref.is_not(None)
                    )
                )
            }
        migrated: list[str] = []
        skipped: list[str] = []
        for ref in file_store.list_refs():
            if ref not in bound_refs:
                skipped.append(ref)
                continue
            token = file_store.load(ref)
            if token is None:
                skipped.append(ref)
                continue
            db_store.store(ref, token)
            migrated.append(ref)
        report = {
            "migrated": len(migrated),
            "skipped_unbound": len(skipped),
            "skipped_refs": skipped,
            "deleted_file": False,
        }
        if delete_file and not skipped and migrated:
            file_store.store_path.unlink(missing_ok=True)
            if file_store.key_path.is_file():
                file_store.key_path.unlink()
            report["deleted_file"] = True
    except TokenStoreError as exc:
        raise _fail(str(exc), code=1) from exc
    finally:
        database.engine.dispose()
    typer.echo(json.dumps(report, indent=2))
    if skipped:
        raise _fail(
            "some file-store refs are not bound to a connected account; fix "
            "or disconnect them, then rerun with --delete-file",
            code=1,
        )


if __name__ == "__main__":
    tokens_app()
