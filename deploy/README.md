# Deploying UrbanView on Hetzner Cloud

Everything runs on one server with Docker Compose (`deploy/compose.yml`):

| Service | What | Reachable at |
|---|---|---|
| `caddy` | HTTPS (Let's Encrypt, automatic renewal) and the reverse proxy | ports 80 / 443 |
| `web` | the public map (Next.js, `frontend/Dockerfile`) | `https://SITE_DOMAIN` |
| `api` | the API (FastAPI, `backend/Dockerfile`) | `https://API_DOMAIN` |
| `minio` | private file bucket (planning PDFs, map tiles, expert reports) | signed links on `https://FILES_DOMAIN` |
| `worker` | background jobs: e-mails, AI extraction and market imports (Anthropic API), connection tests, the publish job (tippecanoe) | internal |
| `postgres` | PostgreSQL 16 + PostGIS | internal |
| `redis` | job queue, caches, rate limiting | internal |
| `migrate`, `storage-init` | one-shot: database migrations, bucket creation (every `up`) | — |

Nothing but Caddy publishes a port; the database, Redis and MinIO are only on Docker's internal
network.

## 1. Create the server

- Hetzner Cloud → new server: **Ubuntu 24.04**, x86 (the **CX** line; 2 vCPU / 4 GB is enough for
  the pilot, 8 GB is more comfortable for builds), location Falkenstein / Nuremberg / Helsinki.
- **SSH key:** add the public key(s) of whoever deploys (never a password). Keep the private key on
  your machine only.
- Optional but recommended: **Backups** (Hetzner's daily server backups, about +20 % of the server
  price) and a **Cloud Firewall** allowing only TCP 22, 80, 443 and UDP 443.

## 2. DNS

Three names pointing at the server's IPv4 (A record) and IPv6 (AAAA record):

```
urbanview.example.com        → the map
api.urbanview.example.com    → the API
files.urbanview.example.com  → signed file links
```

No domain yet: [sslip.io](https://sslip.io) names work with HTTPS and need no DNS, e.g. for the IP
`203.0.113.10`: `map.203-0-113-10.sslip.io`, `api.203-0-113-10.sslip.io`,
`files.203-0-113-10.sslip.io`.

## 3. Prepare the server (once)

```bash
ssh root@SERVER_IP
apt-get update && apt-get install -y git
git clone https://github.com/msajeeb003/Urbanview-POC.git /opt/urbanview
bash /opt/urbanview/deploy/server-setup.sh
```

`server-setup.sh` installs Docker and the compose plugin, adds 4 GB swap, opens only SSH / HTTP /
HTTPS in the firewall, turns on automatic security updates and creates `/opt/urbanview-backups`
(run again once `deploy/.env` exists, it also generates the secrets missing from it).

**Private repository:** create a deploy key on the server (`ssh-keygen -t ed25519 -f
~/.ssh/github_deploy -N ""`), add `~/.ssh/github_deploy.pub` to GitHub → repository → Settings →
Deploy keys (read-only), and clone with
`GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" git clone git@github.com:msajeeb003/Urbanview-POC.git /opt/urbanview`
(then `git -C /opt/urbanview config core.sshCommand "ssh -i ~/.ssh/github_deploy"`).

## 4. Configure

```bash
cd /opt/urbanview
cp deploy/.env.example deploy/.env
nano deploy/.env
```

Fill in every `change-me` (`openssl rand -hex 24` makes a secret; `ADMIN_API_TOKENS` needs at least
24 characters outside dev), the three domains, the SMTP account, the bank details, and the Mapbox
public token (`pk.…`; in the Mapbox account restrict it to `https://SITE_DOMAIN`).
`deploy/.env` stays on the server: it is git-ignored.

`APP_ENV=staging` keeps `/docs` on and only e-mails the addresses in `MAIL_ALLOWLIST`. Switch to
`APP_ENV=prod` (and clear the allow-list) when the site is announced.

Leave `SECRETS_ENCRYPTION_KEY` empty: `deploy/ensure-secrets.sh` (step 5, and `deploy.sh` on
every update) generates it once and never replaces it. It encrypts what the admin console saves
(the Anthropic API key). Keep a copy off the server (a password manager) and never change it once
a key is saved: the saved key would become unreadable. `ANTHROPIC_API_KEY` is optional: the key
can be pasted in the admin console instead (step 9).

## 5. Start

```bash
cd /opt/urbanview
bash deploy/ensure-secrets.sh    # generates SECRETS_ENCRYPTION_KEY in deploy/.env (once)
docker compose -f deploy/compose.yml --env-file deploy/.env up -d --build
```

The first build takes about 10–15 minutes (Python images, tippecanoe from source, the Next.js
build). `migrate` applies the database migrations before the API starts; Caddy gets the
certificates on the first request to each name.

Check:

```bash
docker compose -f deploy/compose.yml --env-file deploy/.env ps
curl -s https://API_DOMAIN/health
docker compose -f deploy/compose.yml --env-file deploy/.env logs -f api caddy
```

## 6. First admin sign-in

A fresh server has no staff users and, until an SMTP account is set, e-mails no sign-in links.
This prints a one-time link instead:

```bash
docker compose -f deploy/compose.yml --env-file deploy/.env exec api \
  python -m core.staff login-link --email you@example.com --create --role admin --name "You"
```

It prints `https://SITE_DOMAIN/admin/login?token=…`: open it in a browser to sign in to the admin
console. The link works once and expires in 15 minutes (`--minutes`, at most 60), with or without
SMTP. Run the command again for a new link; leave out `--create --role … --name …` for a user that
already exists (`--role reviewer` or `expert` creates other staff the same way). Use `exec api`
(the running container) rather than `run --rm api`, which starts the one-shot `migrate` /
`storage-init` services again. Issuing a link is recorded in the audit log.

## 7. Load the sample data (the pilot's Podgorica sample)

```bash
docker compose -f deploy/compose.yml --env-file deploy/.env run --rm api \
  python -m core.seeds podgorica_sample --upload-files
```

This loads zones, documents, parcels and the published values (version 1) and uploads the
placeholder PDFs, so search, map clicks, the panels, the source viewer and orders work.

## 8. Publish the map layers (parcel outlines on the map)

The sample's version 1 has no tile archive, so the map shows the base map only. One publish run
builds it (the worker runs tippecanoe). In the admin console: AI review queue → **Publish**
(admins, once nothing is pending review). Or with a service token of `ADMIN_API_TOKENS`:

```bash
curl -s -X POST https://API_DOMAIN/v1/admin/publish \
  -H "Authorization: Bearer ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "first-publish"}'
curl -s https://API_DOMAIN/v1/admin/publish -H "Authorization: Bearer ADMIN_TOKEN"
curl -s https://API_DOMAIN/v1/tiles/current
```

`/v1/tiles/current` then answers `published` with an `archive_url` on `FILES_DOMAIN`, and the map
draws zones, coverage areas and parcels.

## 9. Activate AI extraction

The worker reads planning PDFs with the Anthropic API (Claude). It needs an API key with credit.

**a. In the admin console (recommended)**

1. In the Anthropic Console create an API key (https://console.anthropic.com/settings/keys) and
   add credit (https://console.anthropic.com/settings/billing).
2. Sign in to the admin console (step 6), then account menu → **AI extraction** (`/admin/ai`,
   admins only).
3. Paste the key and **Save key**. It is stored encrypted with `SECRETS_ENCRYPTION_KEY` and never
   shown again (only its last 4 characters); a connection test runs on the worker.
4. The readiness checklist must be all green: API key configured, key verified by the last
   connection test, worker listening on the extraction queue, an admin or reviewer account (e-mail
   for sign-in links is optional). A failed test says why (invalid key, no credit with the Billing
   link, model unavailable, network …); **Test connection** runs it again.

Extraction jobs pick up a saved key at once, with no restart. The page also shows the model
settings (read-only: `EXTRACTION_MODEL`, `EXTRACTION_EFFORT` … in `deploy/.env`) and the estimated
spend so far.

**b. In the server environment:** set `ANTHROPIC_API_KEY=sk-ant-…` in `deploy/.env`, then recreate
both services (`restart` keeps the old environment):

```bash
docker compose -f deploy/compose.yml --env-file deploy/.env up -d api worker
```

A key there wins over the console's; the AI extraction page shows "Server environment
(ANTHROPIC_API_KEY)" as the source. Run **Test connection** on that page to verify it.

**First rollout of this feature on a running server:** the `deploy.sh` that pulls it is still the
old one, so generate the key once by hand (or run `deploy.sh` a second time):

```bash
cd /opt/urbanview
bash deploy/ensure-secrets.sh && \
  docker compose -f deploy/compose.yml --env-file deploy/.env up -d api worker
```

## 10. Updating (after every push to GitHub)

```bash
bash /opt/urbanview/deploy/deploy.sh
```

Pulls, generates missing secrets (`ensure-secrets.sh`; an existing value is never replaced),
rebuilds what changed, applies new migrations, restarts the changed services, removes old images.
A change to a `NEXT_PUBLIC_*` value in `deploy/.env` also needs this (they are baked into the map
at build time).

## 11. Backups

```bash
crontab -e
# nightly database dump at 03:15, kept 14 days
15 3 * * * /bin/bash /opt/urbanview/deploy/backup.sh >> /var/log/urbanview-backup.log 2>&1
```

Restore a dump into the running database:

```bash
docker compose -f deploy/compose.yml --env-file deploy/.env exec -T postgres \
  pg_restore -U urbanview -d urbanview --clean --if-exists < /opt/urbanview-backups/FILE.dump
```

The bucket (PDFs, tiles, reports) lives in the `minio` volume; Hetzner's server backups or
snapshots cover it. Copy the dumps off the server now and then (or enable Hetzner backups).

Keep a copy of `deploy/.env` (at least `SECRETS_ENCRYPTION_KEY`) off the server: a restored
database cannot decrypt the API key saved in the admin console without it (paste the key again
otherwise).

## Everyday commands

```bash
cd /opt/urbanview
dc="docker compose -f deploy/compose.yml --env-file deploy/.env"
$dc ps                                  # what runs, health
$dc logs -f --tail=100 api worker       # logs
$dc up -d api worker                    # after editing deploy/.env (restart does not re-read it)
$dc exec postgres psql -U urbanview     # database shell
$dc run --rm api python -m core.staff list   # staff users
$dc exec api python -m core.staff login-link --email you@example.com   # one-time sign-in link
```

## Notes

- **Mail:** Hetzner blocks outgoing ports 25 and 465 on new accounts; use a provider on 587 (Resend,
  Brevo). The worker sends; `GET /v1/admin/email-log` shows every attempt.
- **Storage:** MinIO runs on the server's disk, built from source (`deploy/minio/Dockerfile`: MinIO
  stopped publishing free images in 2025; the first build compiles it, a few minutes). Hetzner Object Storage (or Cloudflare R2) can
  replace it: set `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY`,
  `S3_SECRET_KEY` for the api and worker, drop `minio` / `storage-init`, and give the bucket a CORS
  rule allowing `GET` with `Range` from `https://SITE_DOMAIN` (exposing `Accept-Ranges`,
  `Content-Range`, `Content-Length`).
- **Memory:** the Next.js and tippecanoe builds peak above 4 GB; the swap from `server-setup.sh`
  covers it on a 4 GB server.
- **Not deployed here:** Flower (the staff tool is the admin console, served by `web` under
  `/admin`).
