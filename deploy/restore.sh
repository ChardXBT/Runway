#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY Postgres database.
#
#   deploy/restore.sh latest
#   deploy/restore.sh runway-runway-20261122T031700Z.dump
#
# Same configuration as backup.sh (PG* variables, RUNWAY_BACKUP_TOOL,
# RUNWAY_BACKUP_REMOTE, RUNWAY_BACKUP_S3_ENDPOINT). Connect as the migration
# owner role so restored tables are owned by it and the app role's default
# privileges apply. The target must be a fresh database: this script refuses
# one that already has tables, so it cannot overwrite production by accident.
# Set RUNWAY_RESTORE_CONFIRM to the target database name to proceed.
set -euo pipefail

: "${PGDATABASE:?set PGDATABASE (and PGHOST, PGUSER, PGPASSWORD or ~/.pgpass)}"
: "${RUNWAY_BACKUP_REMOTE:?set RUNWAY_BACKUP_REMOTE, e.g. r2:runway-backups/postgres}"
wanted="${1:?usage: restore.sh latest|<backup file name>}"
tool="${RUNWAY_BACKUP_TOOL:-rclone}"
remote="${RUNWAY_BACKUP_REMOTE%/}"

if [[ "${RUNWAY_RESTORE_CONFIRM:-}" != "$PGDATABASE" ]]; then
    echo "refusing: set RUNWAY_RESTORE_CONFIRM=$PGDATABASE to restore into that database" >&2
    exit 2
fi

aws_args=()
if [[ -n "${RUNWAY_BACKUP_S3_ENDPOINT:-}" ]]; then
    aws_args=(--endpoint-url "$RUNWAY_BACKUP_S3_ENDPOINT")
fi
list() {
    case "$tool" in
        rclone) rclone lsf --files-only "$remote/" ;;
        aws) aws "${aws_args[@]}" s3 ls "$remote/" | awk '{print $4}' ;;
        *) echo "RUNWAY_BACKUP_TOOL must be rclone or aws" >&2; exit 2 ;;
    esac
}
fetch() {
    case "$tool" in
        rclone) rclone copyto "$remote/$1" "$work/$1" ;;
        aws) aws "${aws_args[@]}" s3 cp --only-show-errors "$remote/$1" "$work/$1" ;;
    esac
}

if [[ "$wanted" == "latest" ]]; then
    wanted="$(list | grep -E '^runway-.+-[0-9]{8}T[0-9]{6}Z\.dump$' | sort | tail -n 1 || true)"
    [[ -n "$wanted" ]] || { echo "no backups found at $remote" >&2; exit 1; }
fi
[[ "$wanted" =~ ^[A-Za-z0-9._-]+\.dump$ ]] || { echo "not a backup file name: $wanted" >&2; exit 2; }

tables="$(psql --no-psqlrc --tuples-only --no-align --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
if [[ "$tables" != "0" ]]; then
    echo "refusing: $PGDATABASE already has $tables tables; restore into a fresh database" >&2
    exit 2
fi

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fetch "$wanted"
fetch "$wanted.sha256"
(cd "$work" && sha256sum --check --quiet "$wanted.sha256")

pg_restore --no-owner --no-privileges --exit-on-error --single-transaction \
    --dbname "$PGDATABASE" "$work/$wanted"

revision="$(psql --no-psqlrc --tuples-only --no-align --command \
    "SELECT version_num FROM alembic_version")"
echo "restored $wanted into $PGDATABASE at migration $revision"
echo "next: run 'runway db current' against it, then the checks in docs/HOSTED_DEPLOY.md"
