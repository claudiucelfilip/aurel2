# Aurel2 real-money switch: retire live-trader, run Aurel2 `--real` (runbook)

Investigated 2026-10-23 (read-only) on Dumbo and the Mac checkout. Nothing was changed except writing this file.
Code links point at [`474c182`](https://github.com/claudiucelfilip/aurel2/commit/474c1825977af77e52e916d6ebc6ab455b3ffd62), the commit that is deployed (see Finding 1).

## Findings

### 1. Git and deploy state

- Mac `/Users/claudiu/Sites/aurel2`: branch `autonomous-trading` at `474c182`, same as `origin/autonomous-trading` (`git ls-remote` returns `474c1825…`). Dirty: `.claude/settings.local.json`, `data/edge_decomposition/overlay_replay_cache.jsonl` and two untracked JSON files. None of these affect trading.
- Dumbo `/Users/claudiu/vps-root/aurel2`: `autonomous-trading` at `474c182`, clean. `/root` is a symlink to `/Users/claudiu/vps-root`, so `/root/aurel2` is the same checkout (same inode).
- `/opt/aurel2` is not a git repo. `diff -rq` against the Dumbo checkout shows `src/`, `config/` and `docker/` identical (ignoring `.env` and `claude-config`). `scripts/` differs only in watchdog scripts that don't trade (`model_freshness_watchdog.py` is missing from /opt, `sherlock_gateway_watchdog.sh` differs). The deployed code is `474c182`.
- Note: origin's default branch is `main`, but deploys come from `autonomous-trading`.
- Running containers: `aurel2-trading-aurel2-1` (paper, healthy), `-monitor-1`, `-dashboard-1`, `docker-aurel2-crypto-1`, `docker-dashboard-1`, `ana-site`. No real-mode Aurel2 container exists.

### 2. How live-trader trades

- It is not launchd and not crontab. It is a **Hermes cron job in the `claw` profile**, stored in `~/.hermes/profiles/claw/cron/jobs.json`:
  - id **`06b611ad4472`**, name **`claw-live-market-check`**, schedule `45 10 * * 1-5` (**10:45 ET, weekdays**), deliver `slack`, workdir `/Users/claudiu/.openclaw/workspace`, enabled, 20 runs completed, last run 2026-10-22 10:47 ET ok, next run **2026-10-23 10:45 ET**.
  - The prompt tells Claw to run `market_check.py`, then decide, then call `trader.rotate/buy/sell`, then `reconcile_journal.py`. Orders go through `trader.py` → `TradingClient(config["api_key"], config["api_secret"], paper=config["paper"])`, using `live-trader/config.json` (`paper: false`, account `live-challenge`).
- The other 6 `claw` jobs place no orders (`aurel2-daily-ops-check`, `aurel2-crypto-daily-check`, `aurel2-pnl-divergence-check`, `weekly-annoyance-check`, `aurel2-crypto-monday-rebalance-report`, `apple-tv-romania-…`). The `sherlock` profile has no `jobs.json`. The root `~/.hermes/cron` has no `jobs.json`.
- No crontab entry or launchd job places live-trader orders. The 17:00 Friday crontab entry points at `/Users/claudiu/Sites/aurel/runner_7days.sh`, which no longer exists. `daily_runner.py` / `execute_trade.py` are only ever run by hand, and the policy forbids that.
- **How to disable it, scoped to that one job** (other bots and the gateway keep running): `hermes -p claw cron pause 06b611ad4472`. This sets `enabled:false, state:paused` ([`~/.hermes/hermes-agent/cron/jobs.py:2067-2077`]). To undo: `hermes -p claw cron resume 06b611ad4472`. ⚠ On resume, a slot missed during the pause **fires once as a catch-up** (`jobs.py:2080-2099`, `cron.catch_up_missed` defaults to true at `jobs.py:3040`). Resume only after Aurel2-real is stopped.
- One thing the pause does not cover: Claw can still run `trader.py` if someone asks it to in chat, because `live-trader/config.json` still holds valid real-account keys. Only a key rotation closes that (Runbook step 2b).

### 3. Aurel2 real mode

- Flag: `aurel2 live --real` (`--paper/--real`, default paper) ([cli.py#L790-L792](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/cli.py#L790-L792)). On a TTY it asks for confirmation, otherwise it skips the prompt ([cli.py#L844-L852](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/cli.py#L844-L852)). `aurel2 check --real` **always** prompts ([cli.py#L902-L909](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/cli.py#L902-L909)).
- Account selection: paper and real both read **`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`** ([connection.py#L31-L32](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/connection.py#L31-L32)). The only thing that changes the endpoint is the `--real` flag (`paper=False`). Real mode therefore needs a **different env file holding the live key pair**. `TRADING_MODE` is not read by `live` (it is only used by the dashboard and the cron profiles). The journal and pending files are partitioned by mode: `data/live/trade_journal.json`, `data/live/pending_decisions.json`.
- `/opt/aurel2/docker/.env`: defines `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. These are the **paper** keys (prefix `PK`). It has **no** real-account keys. `TRADING_MODE=paper`, `MARKET_TIMEZONE=America/New_York`, `CHECK_TIME=10:30`, `RUN_START=2026-08-10`.
- `/opt/aurel2/live-runner.env` (Jun 12, left over from the old runner): has `APCA_API_KEY_ID` (prefix `AK` = live) and `APCA_API_SECRET_KEY`. **Both are byte-identical to `live-trader/config.json`** (checked by equality, not printed), and they currently work (see Finding 5). Other names: `CHECK_TIME=16:00`, `DRY_RUN=false`, `TRADING_MODE=live`, `NTFY_TOPIC=aurel2-live`, `AI_MODEL`, `AI_LOOKBACK_YEARS`, `POLL_INTERVAL`, plus image-dump variables (`PATH`, `LANG`, `GPG_KEY`, `PYTHON_VERSION`, `PYTHON_SHA256`, `PYTHONPATH`, `PYTHONUNBUFFERED`). Those leftovers must **not** be reused as an env_file. Copy only the two key lines. It has no `MARKET_TIMEZONE`.
- The decommissioning commit [`9429c2c`](https://github.com/claudiucelfilip/aurel2/commit/9429c2ccc497e4a9d37e0979c3b07e158ff7cd1a) only edited `CLAUDE.md` (added a "DECOMMISSIONED … Do not recreate it" note) and removed `aurel2-live-runner` from the [host watchdog list](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/scripts/aurel2_host_watchdog.sh#L101-L111). There was **never a compose service** for it. It was a plain `docker run` container named `aurel2-live-runner` running `live --real`, with mounts of `/root/aurel2/{src,config}` (old CLAUDE.md, commit `3d4d594`) and its own data volume. That volume still exists as **`aurel2-live-data`**: its `heartbeat.json` shows `paper:false`, `last_check 2026-07-10`. `aurel2-live-ib-gateway-settings` is also left over.
- Constraints on the new service:
  - It needs a separate `/root/.aurel2` volume. The heartbeat file at `/root/.aurel2/heartbeat.json` ([daemon.py#L22](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/daemon.py#L22)) stores `last_check`, which prevents a re-run after restart. Sharing the paper volume would let each daemon block the other's daily check.
  - It must sit **outside `docker/docker-compose.yml`**: [deploy.sh#L44-L47](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/scripts/deploy.sh#L44-L47) overwrites that file on every deploy, and the paper `.env` in the same directory would be interpolated into `${APCA_…}`. A separate file with `env_file:` and no `${APCA_…}` interpolation avoids both.
  - deploy.sh's code-only path restarts only `aurel2-trading-aurel2-1` ([deploy.sh#L64-L70](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/scripts/deploy.sh#L64-L70)). The real container keeps running old code until it is restarted.
  - The overlay is disabled (`config/canonical.py:68 enabled: False`) and the AI advisor is off by default (`--ai` / `USE_AI` unset in paper too). The real daemon therefore needs neither Claude auth nor overlay files. Paper today is effectively bare A2, which matches what real would run.

### 4. Reconcile behaviour with a position Aurel2 didn't open

- Startup does no reconciliation. `LiveDaemon.start()` connects, loads pending decisions, sends an ntfy and enters the loop ([daemon.py#L176-L211](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/daemon.py#L176-L211)). `Executor.reconcile_positions()` ([executor.py#L458](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/executor.py#L458)) has **no callers**.
- At each daily check, the checker reads broker positions and **adopts the largest position as `current_holding` if its market value is > $100**, whoever opened it ([checker.py#L257-L262](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/checker.py#L257-L262)). The decision then treats it as Aurel2's own holding. HOLD keeps it. BUY of another symbol triggers `_execute_switch` (sell it, buy the target) ([executor.py#L144-L147](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/executor.py#L144-L147)). SELL sells it. Nothing raises. Nothing sells it just for being foreign.
- If the position is **≤ $100** it is **ignored**: `current_holding=None`, so Aurel2 thinks it is in cash. A BUY then goes to `_execute_buy` with `cash_balance` only, which means a tiny order next to the foreign position ([executor.py#L158-L200](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/executor.py#L158-L200)).
- `_broker_snapshot_is_inconsistent` only matters when the *live journal* has a previous holding ([checker.py#L33-L52](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/checker.py#L33-L52)). `/opt/aurel2/data/live/` is empty, so it is inert.
- **The XLK premise is out of date: the real account holds no position today** (Finding 5). There is nothing to adopt.
- ⚠ **The $100 threshold is a real risk for this account (~$109).** Once Aurel2 buys, the position is about $106.8 (2% settlement headroom, [executor.py#L42-L56](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/executor.py#L42-L56)). A drawdown of about 6.4% puts it under $100, and from then on Aurel2 sees "cash". It would stop switching and could add ~$2 fractional buys of other symbols on top of the stranded position. The same `> 100` test also exists in `get_current_holding` ([executor.py#L442-L456](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/executor.py#L442-L456)), which `trade_recorder.py:83` uses. **Fix this (for example, a relative threshold such as > 50% of equity) before going live with DRY_RUN=false.**

### 5. Current real-account state (read-only GETs via live-trader's own client, run on the host; no secrets printed)

`trader.status()` plus `get_orders(OPEN)`, run 2026-10-23 06:51 ET:
- Portfolio value **$108.98**, cash $108.98, equity 108.98, status ACTIVE.
- **Positions: none (all cash).** **Open orders: none.**
- The journal agrees. The last three entries (Oct 20–22) are `HOLD CASH`.

### 6. Other consumers of the real account / live-trader

| Consumer | What it reads | Effect after switch |
|---|---|---|
| `org.clawdiu.live-trader-dashboard` (port 8083, `/Users/claudiu/vps-root/live-trader-dashboard/app.py`) | `live-trader/trades.jsonl` only, read-only | Doesn't break, but freezes at the last Claw entry. It shows no Aurel2-real trades, which is misleading |
| Weekly scorecard (crontab Fri 23:30 EEST, `weekly_scorecard.py`, `LIVE_TRADER_TRADES_PATH`) | Paper journal plus live-trader `trades.jsonl` | The live-trader arm already reports **`DATA GAP`** in `/opt/aurel2/logs/cron-scorecard.log`. After the switch, no arm tracks Aurel2-real (`--mode paper` only) |
| `scripts/edge_decomposition/lt_actual_audit.py` | live-trader `config.json`, real account | Would attribute Aurel2-real trades to "LT-actual" (this is the July contamination again) |
| Claw jobs `aurel2-daily-ops-check`, `aurel2-pnl-divergence-check` | Paper only (`aurel2_daily_ops_snapshot.sh` contains no alpaca/real/live-trader references) | Unaffected and blind to real |
| `aurel2-trading-monitor-1` (`monitor --paper`) | Paper heartbeat volume | Doesn't watch the real daemon |
| `org.clawdiu.aurel2-host-watchdog` | Fixed container list | Doesn't watch the real container until the list is changed (repo change; deploy rsync overwrites `/opt` copies) |
| Pending approvals ([pending.py#L110](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/pending.py#L110)) | Polls `http://46.225.75.110:8080/decision` (the **deprecated VPS**) and the disk file `data/live/pending_decisions.json` | NON_ROUTINE decisions (3-way strategy split; first entry from cash was NON_ROUTINE on 2026-05-07 paper) time out after 10 min unless approved by editing `/opt/aurel2/data/live/pending_decisions.json`. The dashboard runs `TRADING_MODE=paper`, so its approval UI writes `data/paper` |
| Aurel3 (`/root/aurel3/config.json`) | `alpaca.paper: true`, keys from env names | Not the real account |
| Crontab Dec 5 reminder (`REMIND-AUREL2-DEC`) | — | Says "6-month checkpoint … decide real money". This switch comes before that checkpoint |
| `CLAUDE.md` line 31 | — | Says "aurel2-live-runner is DECOMMISSIONED … Do not recreate it". Needs updating, or future agents will tear the service down |

## Runbook

Run on Dumbo (`ssh claudiu@100.122.64.94`). Dumbo is EEST (ET+7 until 25 Oct, ET+6 from 25 Oct to 1 Nov). The Claw job fires at 10:45 ET and the Aurel2 check at 10:30 ET. Do steps 1–2 before 10:45 ET on a trading day, or over the weekend.

0. **Preflight (read-only).** Confirm the account is flat and has no open orders.
   ```bash
   cd /Users/claudiu/.openclaw/workspace/live-trader && /usr/bin/arch -x86_64 ./.venv/bin/python -c "
   import trader; from alpaca.trading.requests import GetOrdersRequest; from alpaca.trading.enums import QueryOrderStatus
   trader.status(); c=trader.get_client(trader.load_config())
   print('open orders:', [(o.symbol,o.side,o.qty) for o in c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))])"
   ```
   Expected: `No positions (all cash)`, `open orders: []`. Revert: n/a.

1. **Pause the only live-trader order job.**
   ```bash
   HERMES_HOME=/Users/claudiu/.hermes ~/.local/bin/hermes -p claw cron pause 06b611ad4472
   HERMES_HOME=/Users/claudiu/.hermes ~/.local/bin/hermes -p claw cron list | grep -A1 06b611ad4472   # expect [paused]
   ```
   Revert (only after step 4's container is down; a catch-up run fires if a slot was missed):
   `HERMES_HOME=/Users/claudiu/.hermes ~/.local/bin/hermes -p claw cron resume 06b611ad4472`

2. **Make sure only one bot can use the account.**
   - 2a (soft): tell Claw in chat that the live-trader account is retired and not to run `trader.py`. Revert: tell it the opposite.
   - 2b (hard, recommended): in the Alpaca live dashboard, **regenerate the live API key pair**. This revokes the shared keys. Put the new pair only into `real.env` in step 3, not into `live-trader/config.json`. Revert: paste the new pair into `live-trader/config.json` (`api_key`, `api_secret`) and resume step 1. If you skip 2b, step 3 copies the existing pair from `live-runner.env`.

3. **Create the real env file.** It holds the keys plus clean settings, with no image-dump variables.
   ```bash
   cd /opt/aurel2/docker && umask 077
   { grep -E '^APCA_API_(KEY_ID|SECRET_KEY)=' ../live-runner.env     # or paste the regenerated pair from 2b
     printf '%s\n' CHECK_TIME=10:30 MARKET_TIMEZONE=America/New_York POLL_INTERVAL=5 \
       NTFY_TOPIC=aurel2-live DRY_RUN=true AI_MODEL=sonnet AI_LOOKBACK_YEARS=3 TRADING_MODE=live
   } > real.env
   grep -c '^APCA_API_' real.env    # expect 2; do not cat the file
   ```
   Revert: `rm /opt/aurel2/docker/real.env`

4. **Create the real compose file.** It is a separate project, so deploy.sh never overwrites it and the paper `.env` is never interpolated into it.
   ```bash
   cat > /opt/aurel2/docker/docker-compose.real.yml <<'YAML'
   name: aurel2-real
   services:
     aurel2-real:
       image: aurel2-trading-aurel2:latest   # same image the paper daemon runs
       container_name: aurel2-real
       restart: unless-stopped
       env_file: ./real.env                  # real keys; never ${APCA_*} here
       volumes:
         - aurel2-real-data:/root/.aurel2    # own heartbeat/last_check
         - ../data/live:/app/data/live       # live journal + pending approvals
         - ../src:/app/src:ro
         - ../config:/app/config:ro
       command: ["live", "--real"]
   volumes:
     aurel2-real-data:
       name: aurel2-real-data
   YAML
   cd /opt/aurel2/docker && docker compose -f docker-compose.real.yml config --services   # expect aurel2-real
   ```
   Revert: `rm /opt/aurel2/docker/docker-compose.real.yml`

5. **Fix the $100 holding threshold first** (Finding 4). Change `checker.py:261` and `executor.py:453` in the repo, commit, and deploy with `./scripts/deploy.sh` from `/root/aurel2`. Revert: `git revert <sha>` and redeploy. *(Code change: not done here.)*

6. **Start in dry-run on the real account.**
   ```bash
   cd /opt/aurel2/docker && docker compose -f docker-compose.real.yml up -d
   docker exec aurel2-real python -c "import os;print(os.environ['APCA_API_KEY_ID'][:2], os.environ['DRY_RUN'])"   # expect: AK true
   docker logs --tail 40 aurel2-real    # expect "Mode: LIVE", "Dry run: True", alpaca_connected paper=False
   docker ps --format '{{.Names}}\t{{.Status}}' | grep aurel2   # paper aurel2-trading-aurel2-1 still Up
   ```
   Let it run through one 10:30 ET check. Then check `docker logs aurel2-real | grep -E "checker_positions_synced|checker_dry_run"` and `/opt/aurel2/data/live/trade_journal.json`. Dry-run still journals the decision with `executed:false`.
   Revert: `docker compose -f /opt/aurel2/docker/docker-compose.real.yml down` (keeps the `aurel2-real-data` volume; add `-v` to drop it).

7. **Go live (real orders).**
   ```bash
   cd /opt/aurel2/docker && sed -i '' 's/^DRY_RUN=true$/DRY_RUN=false/' real.env \
     && docker compose -f docker-compose.real.yml up -d --force-recreate
   docker exec aurel2-real python -c "import os;print(os.environ['DRY_RUN'])"   # expect false
   ```
   Revert: `sed -i '' 's/^DRY_RUN=false$/DRY_RUN=true/' real.env && docker compose -f docker-compose.real.yml up -d --force-recreate`. For a full stop, use `down` (step 6 revert). If Aurel2 holds a position when you roll back to Claw, Claw's `trader.py` will see it as its own holding.

8. **Approvals.** For a NON_ROUTINE decision (ntfy on topic `aurel2-live`), approve within 10 minutes by setting that decision's `status` to `approved` in `/opt/aurel2/data/live/pending_decisions.json`. The daemon picks up disk edits on the next poll ([pending.py#L258-L280](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/pending.py#L258-L280)). Revert: n/a.

9. **Monitoring and deploys (repo changes, commit and push):**
   - Add `aurel2-real` to the watchdog list in `scripts/aurel2_host_watchdog.sh`.
   - Add `docker restart aurel2-real 2>/dev/null || true` after the paper restart in `scripts/deploy.sh`.
   - Replace the "DECOMMISSIONED … Do not recreate it" block in `CLAUDE.md` with the new service, file and env names.
   Revert: `git revert`. Note: once `aurel2-real` is in the watchdog, a plain `docker stop` gets undone within minutes. Use `compose down` (the watchdog then logs "missing"), or remove it from the list first.

10. **Optional.** Stop the stale dashboard with `launchctl bootout gui/$(id -u)/org.clawdiu.live-trader-dashboard`. Revert: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.clawdiu.live-trader-dashboard.plist`.

11. **Optional cleanup, after a week of stable running:** `docker volume rm aurel2-live-data aurel2-live-ib-gateway-settings` and `rm /opt/aurel2/live-runner.env`. This can't be undone. The keys live on in `real.env` / `config.json`.

## Open questions

1. Rotate the live keys (2b) or rely on the paused cron? Without rotating, Claw can still trade through chat.
2. `$100` threshold fix (step 5): which rule? A relative share of equity is suggested.
3. ntfy topic for real: reuse `aurel2-live` (the old runner's topic, if you are still subscribed) or `aurel2`?
4. Approvals: is editing the JSON by hand acceptable, or should a second dashboard run with `TRADING_MODE=live`, or should the poll URL drop the dead VPS ([pending.py#L110](https://github.com/claudiucelfilip/aurel2/blob/474c1825977af77e52e916d6ebc6ab455b3ffd62/src/aurel2/live/pending.py#L110))?
5. Scorecard: add an Aurel2-real arm and a new `RUN_START`, or retire the live-trader arm (already `DATA GAP`)? Should `lt_actual_audit.py` be marked as covering live-trader only up to the switch date?
6. This comes before the Dec 5 "6-month checkpoint" in the graduation rule (`docs/plans/2026-07-10-graduation-rule.md`). Is that intentional, and should the reminder be edited?
7. Entry timing: Aurel2 starts in cash, while paper holds XLE (since 2026-08-11). The first real check will likely try to enter XLE (or whatever A2 prefers that day). Is entering at that point acceptable?
