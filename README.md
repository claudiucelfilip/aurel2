# Aurel2

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-dashboard-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-deployable-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Tests](https://img.shields.io/badge/pytest-covered-0A9EDC?logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![Broker](https://img.shields.io/badge/Alpaca-paper%20trading-00A651)](https://alpaca.markets/)
[![Mode](https://img.shields.io/badge/mode-research%20software-555555)](#safety-note)

Aurel2 is a systematic equity trading research platform built around rules-first portfolio rotation, broker integration, and operational guardrails. It started as a dual-momentum allocator and grew into a broader testbed for multi-strategy decisioning, live daemon monitoring, paper-trading workflows, and explainable trade approval.

This repository is a public portfolio snapshot. It is intended to show architecture, research process, and implementation quality, not to provide financial advice or a ready-to-run trading product.

## What It Does

- Runs momentum, mean-reversion, and multi-timeframe strategy logic over a curated ETF/equity universe.
- Backtests strategy variants with repeatable research scripts and saved comparison artifacts.
- Supports paper/live broker execution through an Alpaca adapter.
- Tracks pending decisions, trade journals, daemon health, incidents, and session progress.
- Exposes a FastAPI dashboard for current state, backtest comparisons, pending decisions, and operating diagnostics.
- Includes monitor/watchdog logic for detecting stalled or unhealthy daemons.

## Design Principles

- **Deterministic core first**: strategy signals and risk checks are explicit Python code, not hidden model decisions.
- **Human-readable decisions**: non-routine actions can be routed through an approval flow instead of executed blindly.
- **Research before automation**: scripts under `scripts/` capture explored variants, out-of-sample checks, and failure analysis.
- **Operational resilience**: live processes write heartbeats, journals, and recoverable state files.
- **No secrets in code**: broker credentials, notification topics, and approval URLs are environment/config concerns.

## Repository Map

| Path | Purpose |
|---|---|
| `src/aurel2/strategies/` | Strategy implementations: dual momentum, mean reversion, multi-timeframe, robust quarterly variants. |
| `src/aurel2/engine/` | Backtesting and decision-flow analysis. |
| `src/aurel2/live/` | Live/paper daemon, checker, execution, pending decisions, and trade recording. |
| `src/aurel2/broker/` | Broker adapters and execution abstractions. |
| `src/aurel2/monitor/` | Health checks, incident tracking, auto-fix logic, and daemon monitoring. |
| `src/aurel2/dashboard/` | FastAPI dashboard and templates. |
| `scripts/` | Research, replay, validation, deployment, and backtest scripts. |
| `data/` | Checked-in research outputs and comparison artifacts. Runtime journals/caches are ignored. |
| `tests/` | Unit and integration tests for strategy, model, dashboard, execution, and monitor behavior. |

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,dashboard,broker]"
pytest
```

Run a local paper-mode check or dashboard:

```bash
aurel2 check --paper --dry-run
aurel2 dashboard --host 127.0.0.1 --port 8080
```

Broker-backed commands require credentials such as `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the environment. Start with paper mode and dry-run workflows.

## Research Workflow

The project favors executable research over loose notes. Typical loops look like:

```bash
python scripts/backtest_acceptance_protocol.py
python scripts/backtest_universe_candidate_validation.py
python scripts/replay_live_trader_vs_aurel2.py
```

The generated JSON artifacts in `data/` make historical comparisons reviewable without rerunning every experiment.

## Dashboard

The dashboard is designed for operations, not marketing:

- daemon heartbeat and connection state
- current holdings and recent actions
- pending approvals
- backtest comparison tables
- activity and diagnostic views

```bash
aurel2 dashboard --host 127.0.0.1 --port 8080
```

## Configuration And Secrets

Use environment variables or a local `.env` file that is never committed. The Docker example in `docker/.env.example` documents expected values with placeholders only.

Sensitive runtime outputs are ignored by `.gitignore`, including local env files, caches, price caches, databases, logs, and archived runtime data.

## Safety Note

This code can connect to real broker APIs if configured. Treat it as research software. Review every strategy assumption, run paper trading first, and do not use it with live capital without independent validation.
