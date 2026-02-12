# Aurel2 Cloud Deployment Guide

This guide walks you through deploying Aurel2 to a cloud VPS.

## Cloud Provider Options

| Provider | Cheapest Option | Monthly Cost | Notes |
|----------|----------------|--------------|-------|
| **Hetzner** | CX22 (2 vCPU, 4GB RAM) | ~€5 | Best value, EU-based |
| **DigitalOcean** | Basic Droplet (2GB RAM) | $12 | Simple, good docs |
| **Vultr** | Cloud Compute (2GB RAM) | $12 | Global locations |
| **Linode** | Nanode (1GB RAM) | $5 | May be tight on RAM |

**Recommended**: Hetzner CX22 - best price/performance for EU.

## Step 1: Create Cloud Server

### Hetzner (Recommended)

1. Go to https://console.hetzner.cloud
2. Create account and project
3. Add Server:
   - **Location**: Nuremberg or Falkenstein (closest to Romania)
   - **Image**: Ubuntu 24.04
   - **Type**: CX22 (2 vCPU, 4GB RAM, 40GB SSD)
   - **SSH Key**: Add your public key (`cat ~/.ssh/id_rsa.pub`)
   - **Name**: `aurel2-trading`
4. Create server and note the IP address

### DigitalOcean

1. Go to https://cloud.digitalocean.com
2. Create Droplet:
   - **Region**: Frankfurt (closest to Romania)
   - **Image**: Ubuntu 24.04
   - **Size**: Basic, $12/mo (2GB RAM)
   - **SSH Key**: Add your public key
   - **Hostname**: `aurel2-trading`

## Step 2: Initial Server Setup

SSH into your server:

```bash
ssh root@YOUR_SERVER_IP
```

Run initial setup:

```bash
# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose
apt install docker-compose-plugin -y

# Create non-root user (optional but recommended)
adduser aurel2
usermod -aG docker aurel2

# Reboot to apply updates
reboot
```

## Step 3: Prepare Deployment Files (Local Machine)

On your local machine:

```bash
cd ~/Sites/aurel2/docker

# 1. Setup Claude authentication
./setup-claude-auth.sh

# 2. Create .env file
cp .env.example .env

# 3. Edit .env with your credentials
nano .env  # or use your preferred editor
```

**Important .env settings:**
- `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`: Your Alpaca API credentials
- `TRADING_MODE=paper`: Start with paper trading!
- `NTFY_TOPIC`: Your notification topic
- `AUREL2_SETTLEMENT_HEADROOM_PCT` (optional, default `0.02`): reserve percentage before BUY sizing to reduce settlement-limit rejects
- `AUREL2_SETTLEMENT_MIN_CASH_BUFFER` (optional, default `0`): fixed cash to keep unspent before BUY sizing

## Step 4: Deploy to Server

```bash
# From your local machine, in the aurel2 directory
cd ~/Sites/aurel2

# Copy project to server
rsync -avz --exclude='.git' --exclude='data/' --exclude='*.pyc' \
  . root@YOUR_SERVER_IP:/opt/aurel2/

# SSH to server
ssh root@YOUR_SERVER_IP

# Navigate to docker directory
cd /opt/aurel2/docker

# Start the services
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs -f aurel2
```

## Step 5: Verify Deployment

```bash
# Check Aurel2 daemon
docker compose logs aurel2

# Check heartbeat file
docker compose exec aurel2 cat /root/.aurel2/heartbeat.json
```

## Day-to-Day Operations

### View Logs
```bash
docker compose logs -f aurel2      # Trading daemon
```

### Restart Services
```bash
docker compose restart aurel2      # Restart trading daemon
docker compose restart             # Restart all
```

### Stop Everything
```bash
docker compose down
```

### Update Aurel2 Code
```bash
# If running on the VPS directly (recommended):
cd /root/aurel2
./scripts/deploy.sh
# This runs tests, syncs to /opt/aurel2/, and rebuilds containers.

# From a remote machine:
rsync -avz --exclude='.git' --exclude='data/' . root@YOUR_SERVER_IP:/opt/aurel2/
ssh root@YOUR_SERVER_IP "cd /opt/aurel2/docker && docker compose build aurel2 && docker compose up -d aurel2"
```

### Re-authenticate Claude

If Claude auth expires (you'll get a notification):

```bash
# On server
ssh root@YOUR_SERVER_IP
cd /opt/aurel2/docker

# Run Claude login interactively
docker compose exec aurel2 claude login

# Or from local machine, re-run setup and copy:
./setup-claude-auth.sh
rsync -avz claude-config/ root@YOUR_SERVER_IP:/opt/aurel2/docker/claude-config/
docker compose restart aurel2
```

## Optional: Enable Dashboard

```bash
# Start with dashboard
docker compose --profile dashboard up -d

# Access at http://YOUR_SERVER_IP:8080
# (Consider adding nginx + SSL for production)
```

## Optional: Enable Monitoring

```bash
# Start with AI-powered monitoring
docker compose --profile monitoring up -d
```

## Troubleshooting

### Alpaca API connection issues
- Check credentials in `.env` (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`)
- Verify paper/live mode matches your API keys
- View logs: `docker compose logs aurel2`

### Aurel2 daemon not starting
- Check containers: `docker compose ps`
- View logs: `docker compose logs aurel2`
- Check heartbeat: `docker compose exec aurel2 cat /root/.aurel2/heartbeat.json`

### Claude auth not working
- Re-run `setup-claude-auth.sh` locally
- Copy fresh config to server
- Restart aurel2 container

### Out of memory
- Check: `docker stats`
- Upgrade server or reduce services

## Security Notes

1. **Never commit `.env`** - it contains credentials
2. **Use SSH keys**, not passwords
3. **Consider a firewall**: Only allow SSH (22) and optionally dashboard port
4. **VNC should be localhost only** - use SSH tunnel if needed

```bash
# Basic firewall setup
ufw allow ssh
ufw allow 8080/tcp  # Only if using dashboard
ufw enable
```
