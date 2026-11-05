# Discord Community Bot – Railway Deployment

Giveaways · Tickets · Moderation · Auto-Mod · Honeypot · Admin Dashboard

---

## 1. Discord Bot Setup

1. Open [Discord Developer Portal](https://discord.com/developers/applications)
2. **New Application** → name it
3. **Bot** tab → **Reset Token** → copy the token
4. Enable **Privileged Gateway Intents**:
   - ✅ Server Members Intent  
   - ✅ Message Content Intent  
5. **OAuth2 → URL Generator**
   - Scopes: `bot` + `applications.commands`
   - Permissions: **Administrator** (or: Manage Channels, Kick, Ban, Moderate Members, Manage Messages, Manage Roles)
6. Open the invite link and add the bot to your server
7. Move the bot role **above** the roles you want to moderate

---

## 2. Deploy on Railway

### Option A – GitHub (recommended)

1. Create a GitHub repo and upload these files:
   - `main.py`
   - `requirements.txt`
   - `Procfile`
   - `runtime.txt`
   - `railway.toml`
   - `.gitignore`
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Select the repo

### Option B – Railway CLI

```bash
npm i -g @railway/cli
railway login
railway init
railway up
```

---

## 3. Environment Variables

In Railway → your service → **Variables**:

| Variable         | Value                      | Required |
|------------------|----------------------------|----------|
| `DISCORD_TOKEN`  | Your bot token             | ✅ Yes   |
| `DATA_FILE`      | `/data/data.json`          | ✅ Yes (with volume) |

---

## 4. Persistent Storage (Volume)

Without a volume, all data is lost on every redeploy.

1. Service → **Settings** → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. Make sure `DATA_FILE=/data/data.json` is set

---

## 5. Start Command

Already set via `Procfile` / `railway.toml`:

```
python main.py
```

The service runs as a **worker** (no public HTTP port needed).

---

## 6. After Deploy

1. Wait until logs show: `Logged in as YourBot#1234`
2. In Discord try:
   - `/dashboard` – Admin panel
   - `/giveaway` – Giveaway dashboard
   - `/honeypot` – Honeypot panel
   - `/ticket_setup` – Ticket system
3. Slash commands can take up to a few minutes to appear (or restart the bot)

---

## Commands Overview

| Command | Description |
|---------|-------------|
| `/dashboard` | Main admin dashboard (buttons) |
| `/giveaway` | Giveaway dashboard |
| `/honeypot` | Honeypot channel setup |
| `/ticket_setup` | Create ticket panel |
| `/kick` `/ban` `/unban` | Moderation |
| `/timeout` `/untimeout` | Timeouts |
| `/warn` `/warns` `/clearwarns` | Warnings |
| `/purge` | Delete messages |
| `/filter add/remove/list` | Banned words |
| `/automod status/antilink/anticaps/...` | Auto-Mod settings |

---

## Features

- **Giveaways** – Dashboard + modal form, enter button, auto-end, reroll
- **Tickets** – Private channels, transcript on close
- **Moderation** – Kick, ban, timeout, warns, purge
- **Auto-Mod** – Word filter (leet/homoglyph), anti-link, anti-caps, anti-spam
- **Honeypot** – Select channels; anyone who posts gets banned/kicked/timed out
- **Dashboard** – Control everything with buttons

---

## Local testing

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # put your token in .env
python main.py
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot offline | Check `DISCORD_TOKEN` variable |
| Commands missing | Wait 1–5 min or re-invite with `applications.commands` |
| Can't ban/timeout | Bot role must be higher than target role |
| Data resets | Add Volume at `/data` + set `DATA_FILE` |
| Intents error | Enable Members + Message Content in Developer Portal |

---

## Files in this project

```
main.py           → Bot code
requirements.txt  → Python dependencies
Procfile          → Railway start command
runtime.txt       → Python version
railway.toml      → Railway config
.env.example      → Example env vars
.gitignore        → Ignore secrets & cache
README.md         → This file
```
