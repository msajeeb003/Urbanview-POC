#!/usr/bin/env bash
# Move the live site to its production names: steps 3-6 of "Moving to the production domain"
# in deploy/README.md, run on the server as root once the DNS records point here:
#
#   bash /opt/urbanview/deploy/switch-domain.sh                 # urbanview.io
#   bash /opt/urbanview/deploy/switch-domain.sh example.org     # another apex (api./files. in front)
#
# Writes SITE_DOMAIN / API_DOMAIN / FILES_DOMAIN / GEOCODER_CONTACT in deploy/.env (backup kept
# beside it), keeps the old sslip.io names as redirects (deploy/caddy.d/legacy.caddy), rebuilds and
# restarts, then checks the new names. Idempotent: a second run changes nothing already set.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=deploy/.env
NEW_SITE=${1:-urbanview.io}
NEW_API=api.$NEW_SITE
NEW_FILES=files.$NEW_SITE

old_site=$(grep -E '^SITE_DOMAIN=' "$ENV" | cut -d= -f2-)
old_api=$(grep -E '^API_DOMAIN=' "$ENV" | cut -d= -f2-)
old_files=$(grep -E '^FILES_DOMAIN=' "$ENV" | cut -d= -f2-)
echo "before: site=$old_site api=$old_api files=$old_files"

stamp=$(date -u +%Y%m%d-%H%M%S)
cp -p "$ENV" "deploy/.env.bak-$stamp-domain"
echo "backup: deploy/.env.bak-$stamp-domain"

set_kv() {
  local key=$1 val=$2
  if grep -qE "^$key=" "$ENV"; then
    sed -i "s|^$key=.*|$key=$val|" "$ENV"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV"
  fi
}
set_kv SITE_DOMAIN "$NEW_SITE"
set_kv API_DOMAIN "$NEW_API"
set_kv FILES_DOMAIN "$NEW_FILES"
set_kv GEOCODER_CONTACT "https://$NEW_SITE"
echo "after:"
grep -E '^(SITE_DOMAIN|API_DOMAIN|FILES_DOMAIN|GEOCODER_CONTACT|SMTP_FROM|ORDER_SUPPORT_EMAIL)=' "$ENV"

# The old names keep working as redirects (links in e-mails sent before the move, bookmarks).
mkdir -p deploy/caddy.d
if [[ "$old_site" != "$NEW_SITE" && "$old_site" == *sslip.io ]]; then
  cat > deploy/caddy.d/legacy.caddy <<EOF
# Names the site had before the move to $NEW_SITE (written $stamp UTC).
$old_site {
	redir https://$NEW_SITE{uri} permanent
}

$old_api {
	redir https://$NEW_API{uri} permanent
}
EOF
fi
echo "legacy redirects:"
cat deploy/caddy.d/legacy.caddy 2>/dev/null || echo "(none)"

# Rebuild (the map's bundle carries the API's name) and restart; Caddy fetches the certificates.
bash deploy/deploy.sh
docker compose -f deploy/compose.yml --env-file deploy/.env up -d --force-recreate caddy minio

echo "waiting for the certificates..."
for _ in $(seq 1 12); do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$NEW_API/health" || true)
  [[ "$code" == "200" ]] && break
  sleep 10
done
echo "api health: $(curl -s -m 15 "https://$NEW_API/health" || echo unreachable)"
curl -s -o /dev/null -m 20 -w "map https://$NEW_SITE/ -> %{http_code}\n" "https://$NEW_SITE/" || true
curl -s -o /dev/null -m 20 -w "www -> %{http_code} %{redirect_url}\n" "https://www.$NEW_SITE/" || true
curl -s -o /dev/null -m 20 -w "files -> %{http_code}\n" "https://$NEW_FILES/" || true
if [[ "$old_site" != "$NEW_SITE" ]]; then
  curl -s -o /dev/null -m 20 -w "old map -> %{http_code} %{redirect_url}\n" "https://$old_site/" || true
  curl -s -o /dev/null -m 20 -w "old api -> %{http_code} %{redirect_url}\n" "https://$old_api/health" || true
fi
docker compose -f deploy/compose.yml --env-file deploy/.env ps --format 'table {{.Service}}\t{{.Status}}'
echo "DONE"
