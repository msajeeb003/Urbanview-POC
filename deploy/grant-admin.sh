#!/usr/bin/env bash
# Give one staff user the admin role through the audited admin API, acting as the "ops" service
# principal (PATCH /v1/admin/users/{id}; the change shows in the audit log). Run on the server:
#
#   bash /opt/urbanview/deploy/grant-admin.sh <user id> <e-mail>
#
# The id and e-mail come from `python -m core.staff list`; both must match or nothing changes.
# Prints the user's e-mail and role only, never the token.
set -euo pipefail
cd "$(dirname "$0")/.."
USER_ID=${1:?staff user id}
EXPECT_EMAIL=${2:?expected e-mail}
API_DOMAIN=$(grep -E '^API_DOMAIN=' deploy/.env | cut -d= -f2-)
TOKEN=$(grep -E '^ADMIN_API_TOKENS=' deploy/.env | cut -d= -f2- | tr -d '"' | tr ',' '\n' | grep ':admin:ops$' | cut -d: -f1)
if [[ -z "$TOKEN" ]]; then echo "no admin:ops entry in ADMIN_API_TOKENS (deploy/.env)" >&2; exit 1; fi
base="https://$API_DOMAIN/v1/admin/users/$USER_ID"
auth="Authorization: Bearer $TOKEN"
before=$(curl -s -m 20 -H "$auth" "$base")
email=$(printf '%s' "$before" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("email",""))')
role=$(printf '%s' "$before" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("role",""))')
echo "user $USER_ID: $email ($role)"
if [[ "$email" != "$EXPECT_EMAIL" ]]; then echo "e-mail does not match $EXPECT_EMAIL, nothing changed" >&2; exit 1; fi
if [[ "$role" == "admin" ]]; then echo "already admin, nothing changed"; exit 0; fi
after=$(curl -s -m 20 -X PATCH -H "$auth" -H 'Content-Type: application/json' -d '{"role":"admin"}' "$base")
printf '%s' "$after" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("now:", d.get("email"), d.get("role"), "active" if d.get("is_active") else "inactive")'
