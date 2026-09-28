#!/usr/bin/env bash
# First-time preparation of a fresh Hetzner Cloud server (Ubuntu 24.04), run once as root:
#   bash deploy/server-setup.sh
# Installs Docker Engine + the compose plugin (Docker's own apt repository), adds swap (the image
# builds need more than 4 GB at peak), opens only SSH / HTTP / HTTPS, turns on automatic security
# updates and creates the backup folder. Safe to run again.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl git ufw unattended-upgrades

# Docker Engine and the compose plugin
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# shellcheck disable=SC1091
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# 4 GB swap
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# firewall: only Caddy publishes ports (80 / 443); the databases stay on Docker's internal network
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# automatic security updates
dpkg-reconfigure -f noninteractive unattended-upgrades

mkdir -p /opt/urbanview-backups
docker --version
docker compose version
echo "server ready: clone the repository to /opt/urbanview next (deploy/README.md, step 4)"
