#!/usr/bin/env bash
# Daily logical backup of the hosted Runway Postgres database to object storage,
# keeping RUNWAY_BACKUP_RETENTION_DAYS (default 14) days (spec 05 section 7).
#
# Connection: the standard libpq variables PGHOST, PGPORT, PGDATABASE, PGUSER,
# with the password in PGPASSWORD or a ~/.pgpass file, never on the command line.
# Use the migration owner role, which can read every table.
#
# Destination, configured by environment only (no credentials in this file):
#   RUNWAY_BACKUP_TOOL=rclone (default)  RUNWAY_BACKUP_REMOTE=r2:runway-backups/postgres
#       rclone reads its remote's credentials from its own config or RCLONE_CONFIG_* vars.
#   RUNWAY_BACKUP_TOOL=aws               RUNWAY_BACKUP_REMOTE=s3://runway-backups/postgres
#       optional RUNWAY_BACKUP_S3_ENDPOINT (for R2: https://<account>.r2.cloudflarestorage.com);
#       the AWS CLI reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or its profile.
#
# Object storage holds only re-uploadable media, so it is not backed up here.
# Schedule: once a day from cron or a systemd timer on the host, for example
#   17 3 * * *  /opt/runway/deploy/backup.sh >>/var/log/runway-backup.log 2>&1
set -euo pipefail

: "${PGDATABASE:?set PGDATABASE (and PGHOST, PGUSER, PGPASSWORD or ~/.pgpass)}"
: "${RUNWAY_BACKUP_REMOTE:?set RUNWAY_BACKUP_REMOTE, e.g. r2:runway-backups/postgres}"
tool="${RUNWAY_BACKUP_TOOL:-rclone}"
retention_days="${RUNWAY_BACKUP_RETENTION_DAYS:-14}"
remote="${RUNWAY_BACKUP_REMOTE%/}"
prefix="runway-${PGDATABASE}-"

case "$tool" in
    rclone | aws) ;;
    *) echo "RUNWAY_BACKUP_TOOL must be rclone or aws, not '$tool'" >&2; exit 2 ;;
esac
[[ "$retention_days" =~ ^[0-9]+$ ]] && (( retention_days >= 1 )) || {
    echo "RUNWAY_BACKUP_RETENTION_DAYS must be a positive whole number" >&2; exit 2;
}

aws_args=()
if [[ -n "${RUNWAY_BACKUP_S3_ENDPOINT:-}" ]]; then
    aws_args=(--endpoint-url "$RUNWAY_BACKUP_S3_ENDPOINT")
fi

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="${prefix}${stamp}.dump"

# Custom format: compressed, restorable table by table. Ownership and grants
# are left out; restore.sh recreates them as the owner role (see the init script).
pg_dump --format=custom --compress=6 --no-owner --no-privileges --file "$work/$name"
# A dump that pg_restore cannot list is not a backup.
pg_restore --list "$work/$name" >/dev/null
(cd "$work" && sha256sum "$name" >"$name.sha256")

upload() {
    case "$tool" in
        rclone) rclone copyto "$work/$1" "$remote/$1" ;;
        aws) aws "${aws_args[@]}" s3 cp --only-show-errors "$work/$1" "$remote/$1" ;;
    esac
}
upload "$name"
upload "$name.sha256"
echo "uploaded $remote/$name ($(du -h "$work/$name" | cut -f1))"

# Retention by the timestamp in the file name, the same for both tools.
cutoff="$(date -u -d "-${retention_days} days" +%Y%m%dT%H%M%SZ)"
list() {
    case "$tool" in
        rclone) rclone lsf --files-only "$remote/" ;;
        aws) aws "${aws_args[@]}" s3 ls "$remote/" | awk '{print $4}' ;;
    esac
}
remove() {
    case "$tool" in
        rclone) rclone deletefile "$remote/$1" ;;
        aws) aws "${aws_args[@]}" s3 rm --only-show-errors "$remote/$1" ;;
    esac
}
kept=0
while IFS= read -r object; do
    [[ "$object" == "$prefix"* ]] || continue
    object_stamp="${object#"$prefix"}"
    object_stamp="${object_stamp%%.*}"
    if [[ "$object_stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ && "$object_stamp" < "$cutoff" ]]; then
        remove "$object"
        echo "pruned $object"
    else
        kept=$((kept + 1))
    fi
done < <(list)
echo "kept $kept objects newer than $retention_days days"
