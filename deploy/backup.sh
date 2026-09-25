#!/usr/bin/env bash
# Database dump (PostgreSQL custom format), kept 14 days. Nightly from root's crontab:
#   15 3 * * * /opt/urbanview/deploy/backup.sh >> /var/log/urbanview-backup.log 2>&1
# Restore into the running database:
#   docker compose -f deploy/compose.yml --env-file deploy/.env exec -T postgres \
#     pg_restore -U urbanview -d urbanview --clean --if-exists < /opt/urbanview-backups/<file>.dump
# Files in the bucket (PDFs, tiles, reports) live in the minio volume: Hetzner's server backups or
# snapshots cover them.
set -euo pipefail
cd "$(dirname "$0")/.."

dir="${BACKUP_DIR:-/opt/urbanview-backups}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$dir"
file="$dir/urbanview-$(date +%Y-%m-%d-%H%M).dump"

docker compose -f deploy/compose.yml --env-file deploy/.env exec -T postgres \
  pg_dump -U urbanview -Fc urbanview > "$file"
find "$dir" -name 'urbanview-*.dump' -mtime "+${keep_days}" -delete
echo "$(date -Is) backup written: $file ($(du -h "$file" | cut -f1))"
