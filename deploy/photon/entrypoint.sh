#!/usr/bin/env bash
# Import Photon's country dump when there is no database yet (or the imported one is older than
# PHOTON_REIMPORT_DAYS), then serve. Settings (compose passes them): PHOTON_DUMP_URL (the dump to
# import), PHOTON_REIMPORT_DAYS (0 = import on every start), JAVA_OPTS (heap), PHOTON_IMPORT_ARGS
# and PHOTON_SERVE_ARGS (extra photon flags, e.g. "-languages en").
set -euo pipefail

DATA_DIR=${PHOTON_DATA_DIR:-/photon/data}
DUMP_URL=${PHOTON_DUMP_URL:-https://download1.graphhopper.com/public/europe/montenegro/photon-dump-montenegro-1.0-latest.jsonl.zst}
MAX_AGE_DAYS=${PHOTON_REIMPORT_DAYS:-30}
JAVA_OPTS=${JAVA_OPTS:--Xms256m -Xmx768m}
STAMP="$DATA_DIR/imported-at"
mkdir -p "$DATA_DIR"

need_import=1
if [ -f "$STAMP" ] && [ -d "$DATA_DIR/photon_data" ]; then
  age_days=$(( ( $(date +%s) - $(cat "$STAMP") ) / 86400 ))
  if [ "$age_days" -lt "$MAX_AGE_DAYS" ]; then
    need_import=0
    echo "photon: database imported ${age_days} day(s) ago, serving it"
  else
    echo "photon: database is ${age_days} day(s) old, importing a fresh dump"
  fi
fi

if [ "$need_import" = 1 ]; then
  echo "photon: downloading $DUMP_URL"
  curl -fsSL --retry 3 --retry-delay 5 -o "$DATA_DIR/dump.jsonl.zst.part" "$DUMP_URL"
  mv "$DATA_DIR/dump.jsonl.zst.part" "$DATA_DIR/dump.jsonl.zst"
  echo "photon: importing ($(stat -c %s "$DATA_DIR/dump.jsonl.zst") bytes compressed)"
  # shellcheck disable=SC2086
  zstd -dc "$DATA_DIR/dump.jsonl.zst" \
    | java $JAVA_OPTS -jar /photon/photon.jar import -data-dir "$DATA_DIR" -import-file - ${PHOTON_IMPORT_ARGS:-}
  date +%s > "$STAMP"
  echo "photon: import done"
fi

# shellcheck disable=SC2086
exec java $JAVA_OPTS -jar /photon/photon.jar serve -data-dir "$DATA_DIR" \
  -listen-ip 0.0.0.0 -listen-port 2322 ${PHOTON_SERVE_ARGS:-}
