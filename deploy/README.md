# Deploying Phase 1 (shadow — no orders)

Phase 1 only reads public market data and simulates the hedge. The network
write-guard is armed at all times; no secrets are required to run it. Goal of
this deployment: keep it running unattended for 1–2 weeks and accumulate
`shadow_rounds.jsonl` for the strategy-viability decision.

## 1. Get the code on the server

```bash
sudo mkdir -p /opt/rbh-hedge-var && sudo chown "$USER" /opt/rbh-hedge-var
git clone https://github.com/journeyanhk/rbh-hedge-var.git /opt/rbh-hedge-var
cd /opt/rbh-hedge-var
git checkout main            # or: git checkout fix/review-p0
```

## 2. Python env + deps (Python 3.11+)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 3. Prove it's safe and connected BEFORE leaving it running

```bash
export PYTHONPATH=/opt/rbh-hedge-var/src
.venv/bin/python -m rbh_hedge_var guard-check   # expect "post_blocked": true
.venv/bin/python -m rbh_hedge_var probe         # live prices + economics JSON
.venv/bin/python -m pytest -q                   # 54 passing
```

If `guard-check` does not report `post_blocked: true`, STOP — do not run.

## 4. Config (Phase 1 needs no secrets)

```bash
cp .env.example .env    # leave secrets blank; optionally fill Telegram + account index
```

- `LIGHTER_ACCOUNT_INDEX` — optional in Phase 1 (read-only account snapshot).
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional; fill to get HALT /
  single-leg / data-failure alerts. Left blank = silent no-op.
- `config.json` stays as shipped: `enabled:false`, `live_trading:false`.

## 5. Run it as a service (survives reboot, auto-restarts)

```bash
sudo cp deploy/rbh-hedge-var.service /etc/systemd/system/
sudo sed -i "s#/opt/rbh-hedge-var#$PWD#g; s/^User=rbh/User=$USER/" /etc/systemd/system/rbh-hedge-var.service
sudo systemctl daemon-reload
sudo systemctl enable --now rbh-hedge-var
sudo systemctl status rbh-hedge-var --no-pager
```

### Quick alternative (no systemd): tmux

```bash
tmux new -s rbh
export PYTHONPATH=/opt/rbh-hedge-var/src
.venv/bin/python -m rbh_hedge_var run --config config.json
# detach: Ctrl-b then d   |   reattach: tmux attach -t rbh
```

## 6. Watch it

```bash
# service logs (systemd)
journalctl -u rbh-hedge-var -f

# engine log file
tail -f logs/rbh_hedge_var.log

# the shadow ledger — the whole point of Phase 1
tail -f shadow_rounds.jsonl

# loopback dashboard (bound to 127.0.0.1 by design). From your laptop:
ssh -L 8012:127.0.0.1:8012 <user>@<server>   # then open http://localhost:8012
```

## What healthy looks like

- Every tick logs `mode=... action=...`; mostly `IDLE / no_entry:*` until a
  spread wide enough to enter appears, then `shadow_open` → `holding` →
  `shadow_close`.
- `funding_verified:false` is EXPECTED — RHC omits the funding interval, so the
  live gate stays closed. Phase 1 does not need it verified.
- Rounds land in `shadow_rounds.jsonl` with `price_pnl` and `funding_pnl` split.

## Going live is Phase 2 only

Do not set `live_trading:true` — there is no executor in this tree and the
guard blocks all writes. See `GO_LIVE_CHECKLIST.md` before any Phase 2 work.
