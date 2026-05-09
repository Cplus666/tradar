# Dashboard regression — needs fix

## Symptom
Dashboard at `/tradar/` shows blank account / zero PnL / empty position cards on tradar2 (port 7551).

`GET /api/dashboard` returns:

```json
{
  "account": {
    "account_value": null,
    "all_time_pnl": 0.0,
    "open_count": 0,
    "open_value": 0.0,
    "starting_capital": 200.29,
    "today_losses": 0,
    "today_realized": 0.0,
    "today_total": 0.0,
    "today_unrealized": 0.0,
    "today_wins": 0,
    "today_win_rate": null,
    "usdt_free": null
  },
  "position_cards": [],
  "stale": false,
  "static": false
}
```

## What is verified
- Database `/app/data/app.db` (bind-mount of `/home/18566299940/tradar2/data/`) **does** contain user data:
  - `crypto_trades`: **101 rows** (all `is_paper=1`, including 5 trades from today)
  - `crypto_runs`: 466 rows
  - `crypto_daily_snapshots`: 31 rows
  - `settings`: 24 rows
  - `crypto_holdings`: **0 rows** ← empty
- API endpoints respond 200 (not 500). Flask reads the right DB.
- Trade rows look complete: id, symbol, side, qty, price, quote_amount, executed_at, status, is_paper, strategy, notes — all present.
- Sample today: BUY UTKUSDT @ 09:29, SELL VANAUSDT/PENGUUSDT/LTCUSDT/DASHUSDT through the day. Multiple realized round-trips exist.

## Suspect commit
`999a870 "update"` (May 9 21:03) — refactored `webapp/crypto/routes.py` (+459 / -506 lines, almost a full rewrite of dashboard / account-summary code paths).

The earlier passing build was the prior-day commit before this rewrite.

## Likely cause (hypothesis)
`_account_summary()` in `webapp/crypto/routes.py` (line ~107 in current file vs line ~152 pre-rewrite) is the function that populates `account_value`, `usdt_free`, `today_realized`, etc.

`account_value` and `usdt_free` returning `null` is the giveaway:
1. The new `_account_summary` likely **requires a live Binance API call** (balances) before it computes anything — and bails to nulls/zeros on failure or paper mode.
2. With keys missing/invalid OR with `is_paper_mode=true`, the function returns the zero-skeleton early and **never falls back to computing realized PnL from the local `crypto_trades` table**.
3. `position_cards` and `open_count` come from `_open_position_cards()` which probably builds from live Binance balances merged with `crypto_trades`. With no live balances → no cards.

The pre-rewrite version (`git show 999a870^:webapp/crypto/routes.py`) had paper-mode fallbacks that synthesized account state from `crypto_trades` directly. The rewrite removed or broke that path.

## What the next agent should do

1. **Diff the two functions** between `999a870^` and `HEAD`:
   ```bash
   cd /home/18566299940/tradar
   git log --oneline -5
   git show 999a870^:webapp/crypto/routes.py > /tmp/old_routes.py
   diff /tmp/old_routes.py webapp/crypto/routes.py | less
   # focus on the two suspect functions:
   git diff 999a870^ HEAD -- webapp/crypto/routes.py
   ```

2. **Check paper-mode fallback path** in current `_account_summary()`. Confirm whether it returns early when:
   - `_has_binance_keys()` is false, or
   - paper mode is on, or
   - the Binance API call raises.
   The fix is to compute `today_realized`, `all_time_pnl`, `open_count`, `position_cards` from `crypto_trades` (FIFO) when Binance is unavailable or in paper mode — restoring the pre-rewrite behaviour.

3. **`crypto_holdings` is empty** but should not need to be populated for paper mode. Open positions in paper mode should be derivable purely from `crypto_trades` — sum of unmatched BUY qty per symbol with avg entry price from FIFO matching.

4. **Verify settings**: `/tradar/settings` page — confirm paper-mode setting and Binance key state. Almost certainly paper-mode is on (every trade row in DB has `is_paper=1`).

## Useful diagnostic commands

```bash
# row counts
sqlite3 /home/18566299940/tradar2/data/app.db "
SELECT 'trades', COUNT(*) FROM crypto_trades
UNION ALL SELECT 'runs', COUNT(*) FROM crypto_runs
UNION ALL SELECT 'holdings', COUNT(*) FROM crypto_holdings
UNION ALL SELECT 'settings', COUNT(*) FROM settings;
"

# today's trades
sqlite3 /home/18566299940/tradar2/data/app.db "
SELECT id, symbol, side, qty, price, quote_amount, executed_at, is_paper
FROM crypto_trades
WHERE date(executed_at) = date('now')
ORDER BY id DESC;
"

# api response (use IPv4, IPv6 has docker-proxy issue inside SSH session)
curl -s -4 http://localhost:7551/tradar/api/dashboard | python3 -m json.tool

# rebuild and restart tradar2 after edits
cd /home/18566299940/tradar
sudo docker compose build
sudo docker stop tradar2 && sudo docker rm tradar2
sudo docker run -d --name tradar2 -p 7551:7550 \
  -v /home/18566299940/tradar2/data:/app/data \
  -v /home/18566299940/tradar2/logs:/app/logs \
  --restart unless-stopped \
  -e TZ=Asia/Kuala_Lumpur -e HOST=0.0.0.0 -e PORT=7550 -e STOCK_RUN_SCHEDULER=1 \
  tradar:latest
```

## Don't-touch list
- Bind mount: `/home/18566299940/tradar2/data:/app/data` — never replace with anonymous volume; data was almost lost once already.
- The DB itself: do not `DROP TABLE` or run destructive migrations. Backup first if you need to mutate schema:
  ```bash
  sudo cp /home/18566299940/tradar2/data/app.db /home/18566299940/tradar2/data/app.db.bak_$(date +%s)
  ```
