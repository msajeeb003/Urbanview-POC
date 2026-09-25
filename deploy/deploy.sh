#!/usr/bin/env bash
# Update the running app to the latest commit of the checked-out branch:
#   /opt/urbanview/deploy/deploy.sh
# Pulls, rebuilds the images whose sources changed, applies database migrations (the `migrate`
# service runs on every `up`), restarts what changed and removes old images.
set -euo pipefail
cd "$(dirname "$0")/.."

compose=(docker compose -f deploy/compose.yml --env-file deploy/.env)

if [[ ! -f deploy/.env ]]; then
  echo "deploy/.env is missing: copy deploy/.env.example and fill it in" >&2
  exit 1
fi
if grep -qE '^[^#].*change-me' deploy/.env; then
  echo "deploy/.env still has change-me placeholders" >&2
  exit 1
fi

git pull --ff-only
"${compose[@]}" up -d --build --remove-orphans
docker image prune -f >/dev/null
"${compose[@]}" ps
