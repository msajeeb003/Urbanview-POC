#!/usr/bin/env bash
# Takes a handover snapshot of the live system: the database dump, the file bucket as one
# archive, the settings file with every credential blanked, and a manifest with checksums.
# Reads only; nothing on the running site changes. Run on the server, from the repository:
#
#     bash deploy/handover/snapshot.sh
#
# Output: /root/handover-<today>/ (see deploy/handover/README.md for the next steps).
set -euo pipefail
cd "$(dirname "$0")/../.."
DATE="${1:-$(date -u +%Y-%m-%d)}"
OUT="/root/handover-$DATE"
mkdir -p "$OUT"
C="docker compose -f deploy/compose.yml --env-file deploy/.env"

echo "== 1/4 database -> $OUT/urbanview-db-$DATE.dump"
# spatial_ref_sys belongs to the PostGIS extension and is filled by it on the new server.
$C exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --exclude-table-data=spatial_ref_sys' \
  > "$OUT/urbanview-db-$DATE.dump"
$C exec -T postgres sh -c 'pg_dump --version; psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version(); select postgis_full_version();"' \
  > "$OUT/db-version.txt"

echo "== 2/4 file bucket -> $OUT/urbanview-files-$DATE.tar.gz"
API="$($C ps -q api)"
docker exec -i -e HANDOVER_DATE="$DATE" "$API" python - <<'PY'
import datetime as dt, json, os, pathlib, tarfile
import boto3
from botocore.config import Config

date = os.environ["HANDOVER_DATE"]
bucket = os.environ["S3_BUCKET"]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name=os.environ.get("S3_REGION", "us-east-1"),
    config=Config(s3={"addressing_style": "path"}),
)
root = pathlib.Path("/tmp/handover")
files = root / "files"
files.mkdir(parents=True, exist_ok=True)
objects = []
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
    for obj in page.get("Contents", []):
        dest = files / obj["Key"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, obj["Key"], str(dest))
        objects.append({"key": obj["Key"], "size": obj["Size"], "etag": obj["ETag"].strip('"'),
                        "last_modified": obj["LastModified"].isoformat()})
manifest = root / "files-manifest.json"
manifest.write_text(json.dumps({
    "bucket": bucket,
    "taken_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "object_count": len(objects),
    "total_bytes": sum(o["size"] for o in objects),
    "objects": objects,
}, indent=1), encoding="utf-8")
tar_path = root / f"urbanview-files-{date}.tar.gz"
with tarfile.open(tar_path, "w:gz") as tar:
    tar.add(files, arcname="files")
    tar.add(manifest, arcname="files-manifest.json")
print(f"{len(objects)} objects, {sum(o['size'] for o in objects)} bytes")
PY
docker cp "$API:/tmp/handover/urbanview-files-$DATE.tar.gz" "$OUT/"
docker exec "$API" rm -rf /tmp/handover

echo "== 3/4 settings without credentials -> $OUT/deploy.env.redacted"
python3 - deploy/.env "$OUT/deploy.env.redacted" <<'PY'
import pathlib, sys

src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
KEEP = {
    "SITE_DOMAIN", "API_DOMAIN", "FILES_DOMAIN", "APP_ENV", "MAIL_ALLOWLIST", "S3_BUCKET",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USE_TLS", "SMTP_FROM", "ORDER_SUPPORT_EMAIL",
    "ORDER_BANK_BENEFICIARY", "ORDER_BANK_BENEFICIARY_ADDRESS", "ORDER_BANK_NAME",
    "ORDER_BANK_ACCOUNT", "ORDER_BANK_IBAN", "ORDER_BANK_SWIFT", "ORDER_PRICE_TIERS",
    "ORDER_TURNAROUND_BUSINESS_DAYS", "GEOCODER_CONTACT", "GEOCODER_TIMEOUT_MS",
    "NEXT_PUBLIC_MAPBOX_STYLE", "NEXT_PUBLIC_MARKET_DATA_FREE", "NEXT_PUBLIC_ANALYTICS_ENABLED",
    "NEXT_PUBLIC_DEFAULT_LANG", "SERVER_IP", "AUTH_SESSION_MAX_AGE", "STAFF_SESSION_DAYS",
    "ANTHROPIC_BASE_URL", "EXTRACTION_MODEL",
}
HEADER = """# UrbanView production settings, handover copy.
# Every credential is replaced by change-me and handed over separately: the database and MinIO
# passwords, the staff API tokens, the mail account's login, the Mapbox token, the session
# secrets and the Anthropic API key. Everything else is the live value.
# Fill in the credentials and place the file at deploy/.env (see deploy/README.md, section 4).
"""
out, kept, redacted = [], [], []
for line in src.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        out.append(line)
        continue
    key, _, value = line.partition("=")
    key = key.strip()
    if key in KEEP or value.strip() == "":
        out.append(line)
        kept.append(key)
    else:
        out.append(f"{key}=change-me")
        redacted.append(key)
dst.write_text(HEADER + "\n".join(out) + "\n", encoding="utf-8")
print("kept:", " ".join(kept))
print("blanked:", " ".join(redacted))
PY

echo "== 4/4 manifest -> $OUT/MANIFEST.txt"
{
  echo "UrbanView handover snapshot $DATE"
  echo "taken (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "code: $(git rev-parse HEAD)"
  echo
  cat "$OUT/db-version.txt"
  echo
  echo "sha256:"
  (cd "$OUT" && sha256sum "urbanview-db-$DATE.dump" "urbanview-files-$DATE.tar.gz" deploy.env.redacted)
  echo
  echo "sizes (bytes):"
  (cd "$OUT" && ls -l "urbanview-db-$DATE.dump" "urbanview-files-$DATE.tar.gz" deploy.env.redacted | awk '{print $5, $9}')
} > "$OUT/MANIFEST.txt"
cat "$OUT/MANIFEST.txt"
echo
echo "Done. Copy the folder to your computer, for example:"
echo "    scp -r root@<server>:$OUT data/handover/"
