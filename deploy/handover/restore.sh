#!/usr/bin/env bash
# Restores a handover snapshot on a fresh server: the database dump into PostgreSQL and the file
# archive into the bucket. Run on the new server, from the repository, after deploy/.env is
# filled in and the stack was started once (deploy/README.md, sections 1 to 5):
#
#     bash deploy/handover/restore.sh data/handover/2026-10-08
#
# The site is stopped while the database is written and started again at the end. Everything
# the database held before is replaced.
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC="${1:?folder with urbanview-db-<date>.dump and urbanview-files-<date>.tar.gz}"
DUMP="$(ls "$SRC"/urbanview-db-*.dump | head -1)"
FILES="$(ls "$SRC"/urbanview-files-*.tar.gz | head -1)"
C="docker compose -f deploy/compose.yml --env-file deploy/.env"

echo "== 1/3 database <- $DUMP"
$C stop api worker web
PG="$($C ps -q postgres)"
docker cp "$DUMP" "$PG:/tmp/handover.dump"
# pg_restore reports an error for each object it drops that is not there yet; the counts below
# are the check that matters.
$C exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists /tmp/handover.dump' \
  || echo "pg_restore ended with errors; see the counts below"
docker exec "$PG" rm -f /tmp/handover.dump
$C up -d
$C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select '"'"'publish_versions '"'"' || count(*) from publish_versions union all select '"'"'planning_parameter_values '"'"' || count(*) from planning_parameter_values union all select '"'"'urban_parcels '"'"' || count(*) from urban_parcels union all select '"'"'orders '"'"' || count(*) from orders union all select '"'"'staff_users '"'"' || count(*) from staff_users union all select '"'"'alembic_version '"'"' || version_num from alembic_version"'

echo "== 2/3 files <- $FILES"
TMP="$(mktemp -d)"
tar -xzf "$FILES" -C "$TMP"
API="$($C ps -q api)"
docker cp "$TMP/files" "$API:/tmp/restore-files"
docker exec -i "$API" python - /tmp/restore-files <<'PY'
import mimetypes, os, pathlib, sys
import boto3
from botocore.config import Config

root = pathlib.Path(sys.argv[1])
bucket = os.environ["S3_BUCKET"]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name=os.environ.get("S3_REGION", "us-east-1"),
    config=Config(s3={"addressing_style": "path"}),
)
try:
    s3.head_bucket(Bucket=bucket)
except Exception:
    s3.create_bucket(Bucket=bucket)
count = total = 0
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    key = path.relative_to(root).as_posix()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
    count += 1
    total += path.stat().st_size
print(f"{count} objects, {total} bytes uploaded to {bucket}")
PY
docker exec "$API" rm -rf /tmp/restore-files
rm -rf "$TMP"

echo "== 3/3 the site is up again"
$C ps --format "{{.Service}} {{.Status}}"
echo "Next: deploy/README.md section 6 (first admin sign-in). The staff accounts of the snapshot"
echo "exist already; a sign-in link is e-mailed to them, or minted with core.staff login-link."
