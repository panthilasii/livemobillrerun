# Deploying NP Create — License & Admin Server

Target: a $6/month DigitalOcean / Vultr / Hetzner droplet running
Ubuntu 24.04. Estimated install time: **15 minutes** end-to-end.

## What you need

* A VPS with a public IPv4 (≥ 1 GB RAM, 25 GB disk).
* A domain name (or subdomain) like `admin.np-create.com`.
* SSH access as root or a sudoer.

## Step 1 — point DNS at the VPS

In your registrar, add an `A` record:

```
admin.np-create.com    A    <VPS-IPv4>
```

Wait until `dig admin.np-create.com` answers with the right IP
before proceeding (usually < 5 minutes for fresh records).

## Step 2 — install Docker on the VPS

```bash
ssh root@<VPS-IP>
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

## Step 3 — clone the project

```bash
mkdir -p /opt && cd /opt
git clone <your-git-remote> npcreate
cd npcreate/vcam-server
```

If you don't have a git remote yet, `scp` the `vcam-server/` folder
from your laptop:

```bash
# On your laptop:
scp -r vcam-server root@<VPS-IP>:/opt/npcreate/
```

## Step 4 — configure

```bash
cd /opt/npcreate/vcam-server
cp .env.example .env
nano .env
# Set DOMAIN=admin.np-create.com (your real subdomain)
# Set SESSION_SECRET=$(openssl rand -hex 32)
```

## Step 5 — bootstrap (one-time)

These commands create the SQLite DB, generate the Ed25519 signing
seed, and create your first admin user.

```bash
mkdir -p data
docker compose run --rm app python -m app.cli init-db
docker compose run --rm app python -m app.cli init-keys
docker compose run --rm app python -m app.cli create-admin \
    --email you@np.local \
    --password "$(openssl rand -base64 24)" \
    --display-name "You"
```

**Save the generated password** — it's printed once.

The `init-keys` command also prints a public-key hex line. Copy
it; you'll paste it into `vcam-pc/src/_pubkey.py` so customer
builds verify keys issued by THIS server.

## Step 6 — start

```bash
docker compose up -d
docker compose logs -f app    # watch for "Application startup complete"
```

Caddy obtains a cert from Let's Encrypt within ~30 s of the first
HTTPS request. Open `https://admin.np-create.com/admin` in your
browser and log in.

## Step 7 — point customer apps at this server

In `vcam-pc/src/_pubkey.py`, paste the hex you copied at Step 5:

```python
PUBLIC_KEY_HEX = "<the-64-hex-chars-from-init-keys>"
```

Then in `vcam-pc/src/branding.py` (or wherever the activation
endpoint URL lives), set:

```python
license_server_url = "https://admin.np-create.com"
```

Rebuild the customer bundle (`python tools/build_release.py`) and
ship the new installer to customers via your existing update
channel.

## Backups

```bash
# On the VPS, daily cron:
tar czf /backups/npc-$(date +%F).tar.gz /opt/npcreate/vcam-server/data
```

The `data/` dir contains:
* `npcreate.sqlite3`  — all customers, licenses, payments.
* `.private_key`       — your Ed25519 signing seed (CRITICAL).
* `uploads/`           — support log zips.

**The signing seed is irreplaceable.** Lose it and you can't issue
new keys that match shipped customer builds. Burn a copy onto a
USB stick AND a password manager AND email it to yourself
encrypted with `gpg -c`.

## Maintenance

```bash
# View logs
docker compose logs -f app

# Restart after a code update
git pull && docker compose up -d --build

# Prune old support uploads (older than 90 days)
docker compose exec app python -m app.cli prune-uploads --days 90

# Reset an admin password
docker compose exec app python -m app.cli set-password \
    --email you@np.local --password "<new>"

# Show the public key (for re-checking)
docker compose exec app python -m app.cli show-pubkey
```

## Updating the server

```bash
cd /opt/npcreate
git pull
cd vcam-server
docker compose up -d --build app
# Caddy doesn't usually need to restart; if Caddyfile changed:
docker compose restart caddy
```

Migrations run automatically at startup (the `init-db` SQL is
idempotent — `CREATE TABLE IF NOT EXISTS …`).

## Estimated monthly cost

| Item                             | Cost         |
|----------------------------------|--------------|
| Vultr Cloud Compute 1 vCPU/1 GB  | $6.00/month  |
| Domain (`.com`)                  | ~$1.00/month |
| **Total**                        | **~$7/mo**   |

For ≤ 500 customers and < 100 phone-home requests/sec, this
single-VPS setup is enough. Above that, switch to a 2 GB instance
and split the SQLite to PostgreSQL — schema is unchanged.
