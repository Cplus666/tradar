# Tradar

Autonomous Binance trading bot. Runs locally on your hardware (NAS / Pi / mini PC). Your API keys, your data, your machine.

## What it does

- Watches the top ~30 hot coins on Binance (refreshed each loop)
- Runs 5 strategies: 4h breakout, 1h breakout, momentum surge, pullback in uptrend, oversold mean reversion
- Places real Binance orders when an A+ setup fires
- Auto-exits via stop / target / time-stop / regime change
- Shows everything in a web dashboard

## Quick deploy on a NAS (Synology / QNAP / Z-Space / Unraid / TrueNAS)

```bash
# 1. Copy this folder to your NAS docker dir
#    e.g.  /volume1/docker/tradar/

# 2. SSH into the NAS, into that folder
cd /volume1/docker/tradar

# 3. Start
docker compose up -d --build

# 4. Open the dashboard
#    http://<your-nas-ip>:5050/
```

## First-time setup (5 minutes)

1. Open `http://<nas-ip>:5050/`
2. Click **Settings** in the nav
3. Paste your **Binance API key + secret** (under "Binance API credentials")
4. Click **Test connection** — should show "✓ Connection OK"
5. Reveal IP and add it to your Binance API key whitelist on Binance.com (defense-in-depth)
6. Adjust **guardrails** if you want (defaults are conservative — $10/trade, 2 max concurrent)
7. Leave **kill switch ON** + **mode = paper** for the first few days
8. After validating: turn kill switch OFF, optionally switch to LIVE mode

## Backup

The only thing that matters: `./data/app.db` (SQLite — settings, trades, journal).

Set up your NAS's snapshot/backup task to capture `./data/` daily. That's all you need.

## Update

```bash
cd /volume1/docker/tradar
git pull
docker compose up -d --build
```

Data persists across rebuilds (volume-mounted).

## Defaults for new install

| Setting | Default | Why |
|---|---|---|
| Kill switch | **ON** | Won't trade until you opt in |
| Mode | **paper** | Simulated trades only — no real money |
| Max position size | $10 | Tiny — safe even if accidentally enabled live |
| Max concurrent | 2 | Conservative |
| Drawdown halt | 10% | Tighter than usual |
| Loop interval | 15 min | Sweet spot for 4h candle strategies |
| Fast exit check | 60 sec | Stops/targets react within 1 min |

Adjust via `/crypto/settings` after API keys are configured.

## Required: Binance API key permissions

When creating the API key on Binance:

| Permission | Required? |
|---|---|
| Enable Reading | ✅ Yes |
| Enable Spot & Margin Trading | ✅ Yes |
| Enable Withdrawals | ❌ **NEVER** |
| Enable Margin/Futures/Universal Transfer | ❌ No |

IP whitelist: **strongly recommended**. Whitelist your home/NAS public IP. The Settings page has an "Reveal IP" button that shows what to whitelist.

## Disclaimer

This is a personal trading tool. Markets carry risk. Strategies that work historically may stop working. Test in paper mode for at least 2 weeks before trading real money. The author is not responsible for any losses.
