# Round 2 — paper-mode account valuation is wrong (after `3a39142`)

The fix in `3a39142 "Fix paper-mode dashboard regression"` partly worked — dashboard now renders, position cards show correct per-position PnL, today_realized matches the journal. But the account-level numbers are wrong.

## Observed (paper mode, 101 trades, 3 open positions)

```json
{
  "account_value": 3720.25,
  "usdt_free": 0.0,
  "open_value": 3720.25,
  "starting_capital": 200.29,
  "all_time_pnl": 3519.96,
  "today_realized": -262.46,
  "today_unrealized": -6199.79,
  "today_total": -6462.24,
  "day_start_value": 10182.49,
  "drawdown_pct": 63.46
}
```

Position cards (sum is the **truth** for unrealized):

| Symbol | qty | entry | current | pnl_usd |
|---|---|---|---|---|
| VANAUSDT | 462.10 | 1.623 | 1.697 | +32.66 |
| UTKUSDT | 188679.24 | 0.00795 | 0.00795 | -3.00 |
| ICPUSDT | 399.68 | 3.753 | 3.593 | -66.88 |
| **Sum** | | | | **-37.22** |

## Two bugs

### Bug A — `usdt_free` stuck at 0 in paper mode
In paper mode every closed SELL should credit virtual USDT back to a synthesized cash balance. The current code returns `usdt_free: 0.0`, so `account_value = open_value` only. That's why `account_value` ($3,720.25) looks small even though all-time PnL is +$3,519 from a $200 base.

The correct paper cash balance is derivable purely from `crypto_trades`:
```
usdt_free = starting_capital
          + Σ (sell_qty * sell_price - sell_fee)        ; cash in
          - Σ (buy_qty  * buy_price  + buy_fee)         ; cash out
          (over all rows where is_paper=1 and status='filled')
```
Or equivalently: `starting_capital + all_realized_pnl - cost_basis_of_currently_open_positions`.

### Bug B — `today_unrealized` is computed via subtraction from a stale `day_start_value`
Right now (suspected): `today_unrealized = today_total - today_realized`, where `today_total = account_value - day_start_value`. But `day_start_value` is `10,182.49` — that's not yesterday's end-of-day account value, it's wrong.

`today_unrealized` should simply be the **sum of per-position `pnl_usd` from `_open_position_cards()`** (or recomputed the same way at account level). It must equal the sum across position cards (-$37.22 in this snapshot).

`today_total` should then be `today_realized + today_unrealized = -$262.46 + (-$37.22) = -$299.68`, not -$6,462.

`day_start_value` itself: in paper mode it should be the account_value as of `start_of_today` (00:00 local) — i.e. the equity curve point from yesterday's last snapshot, *not* a sum of trade volumes or anything else.

## Repro

```bash
curl -s -4 http://localhost:7551/tradar/api/dashboard | python3 -m json.tool
```

Compare `account.today_unrealized` to `sum(card.pnl_usd for card in position_cards)`.
They should match. Currently they're off by a factor of ~167.

## Fix direction

In `_account_summary()` (current `webapp/crypto/routes.py` line ~107):
1. Compute `usdt_free` from trades (formula above) instead of returning `0.0` in paper mode.
2. Compute `today_unrealized` by **summing position-card pnl_usd**, not by subtracting from `day_start_value`.
3. Reconcile `day_start_value` from `crypto_daily_snapshots` (yesterday's row) — it has 31 rows already.

The `crypto_daily_snapshots` table is presumably what the equity curve / day_start should use. Schema unchecked — `sqlite3 .schema crypto_daily_snapshots` first.

## Don't break round 1

The fix from `3a39142` for paper-mode (FIFO reconstruction of positions) is correct — keep it. The bugs are downstream of it: the position cards are right, the account aggregation on top of them is wrong.
