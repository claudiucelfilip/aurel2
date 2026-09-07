"""Leak test: can the overlay model date an ANONYMIZED context pack?

If it can name the year/event from relabeled, undated, relative-only market
data, historical overlay backtests are graded on memory, not judgment.
Controls are the same packs with real tickers and the as_of date left in;
they must be trivially identifiable or the test itself is broken.
"""
import glob, json, random, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aurel2.config.canonical import live_asset_registry
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.overlay.cli_backends import call_claude_cli
from aurel2.overlay.context_pack import build_context_pack_v2

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "edge_decomposition" / "overlay_leak_test.json"
RAW = ROOT / "data" / "edge_decomposition" / "overlay_leak_test_raw.jsonl"
MODEL = "claude-fable-5"

# (as_of, label, event-keyword regex or None for ordinary windows)
WINDOWS = [
    ("2008-10-10", "GFC crash",           r"2008|financial crisis|lehman|gfc|subprime"),
    ("2009-03-09", "GFC bottom",          r"2009|financial crisis|gfc|bottom"),
    ("2011-08-08", "US downgrade",        r"2011|downgrade|debt ceiling|euro"),
    ("2015-08-24", "China flash crash",   r"2015|china|yuan|flash"),
    ("2016-02-11", "oil/China low",       r"2016|oil|china"),
    ("2018-02-05", "volmageddon",         r"2018|vol|xiv"),
    ("2018-12-24", "Q4 2018 selloff",     r"2018|fed|powell|tariff|trade war"),
    ("2020-03-16", "COVID crash",         r"2020|covid|pandemic|corona"),
    ("2020-11-09", "vaccine day",         r"2020|vaccine|election|pfizer"),
    ("2022-06-16", "2022 bear",           r"2022|inflation|rate hike|fed|bear"),
    ("2022-10-12", "2022 bear low",       r"2022|inflation|rate hike|fed|bear"),
    ("2023-03-13", "SVB",                 r"2023|svb|silicon valley|bank"),
    ("2025-04-07", "tariff crash",        r"2025|tariff|liberation"),
    ("2024-08-05", "yen carry unwind",    r"2024|yen|carry|japan"),
    ("2007-05-10", "ordinary", None), ("2010-09-14", "ordinary", None), ("2012-04-17", "ordinary", None),
    ("2013-10-22", "ordinary", None), ("2014-06-09", "ordinary", None), ("2017-07-18", "ordinary", None),
    ("2019-08-26", "ordinary", None), ("2021-06-15", "ordinary", None), ("2023-09-19", "ordinary", None),
    ("2024-05-14", "ordinary", None), ("2025-01-14", "ordinary", None),
]
CONTROLS = ["2020-03-16", "2013-10-22", "2022-06-16"]

SYMBOLS = sorted({a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol})
CLASS_BY_SYMBOL = {a.yahoo_symbol: ac.value for ac, a in ASSET_REGISTRY.items() if a.yahoo_symbol}


def load_prices() -> pd.DataFrame:
    frames = [pd.read_parquet(f) for f in glob.glob(str(ROOT / "data/price_cache/*.parquet")) if "-USD" not in f]
    p = pd.concat(frames, ignore_index=True)
    p["date"] = pd.to_datetime(p["date"]).dt.date
    return p


def anonymize(pack: dict, seed: int) -> dict:
    rng = random.Random(seed)
    labels = [f"A{i:02d}" for i in range(1, len(SYMBOLS) + 1)]
    rng.shuffle(labels)
    label = dict(zip(SYMBOLS, labels))
    pack = dict(pack)
    pack.pop("as_of", None)
    pack["benchmark_20d_annualized_vol_pct"] = pack.pop("spy_20d_annualized_vol_pct", None)
    pack["deterministic_a2_signal_today"] = {"action": "hold"}
    s = json.dumps(pack, default=str)
    for sym in sorted(SYMBOLS, key=len, reverse=True):          # whole quoted tokens only
        s = re.sub(rf'"{re.escape(sym)}"', f'"{label[sym]}"', s)
        s = s.replace(CLASS_BY_SYMBOL[sym], label[sym])
    return json.loads(s)


PROMPT = """You are shown market data for a fixed universe of 16 US-listed ETFs (broad equities, sectors, bonds, TIPS, REITs, gold, commodities), {mode}.
All figures are RELATIVE (percent returns, RSI, z-scores); no absolute prices.

Task: estimate WHEN this data ends. Give your single best guess even if unsure.
Respond with JSON only, no prose:
{{"year": <int>, "month": <int or null>, "event": "<named market event if you recognize one, else null>", "confidence": <0.0-1.0>, "clues": "<one sentence: what you keyed on>"}}

DATA:
{data}"""


def build_case(prices, as_of_iso, control, seed):
    as_of = date.fromisoformat(as_of_iso)
    pack = build_context_pack_v2(prices, as_of, live_asset_registry(), None, 0, {"action": "hold"})
    if control:
        mode = "with real tickers and the as_of date included (CONTROL)"
        data = json.dumps(pack, indent=1, default=str)
    else:
        mode = "relabeled A01-A16 in random order, with all dates removed"
        data = json.dumps(anonymize(pack, seed), indent=1)
    return PROMPT.format(mode=mode, data=data)


def run_case(args):
    as_of, label, kw, control, seed, prompt = args
    parsed, latency, raw = call_claude_cli(prompt, model=MODEL)
    rec = {"as_of": as_of, "label": label, "control": control, "latency_s": round(latency, 1),
           "parsed": parsed, "raw": raw[:1500]}
    with RAW.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    true_year = int(as_of[:4])
    yr = (parsed or {}).get("year")
    try: yr = int(yr)
    except Exception: yr = None
    ev = str((parsed or {}).get("event") or "") + " " + str((parsed or {}).get("clues") or "")
    rec.update(true_year=true_year, guess_year=yr,
               year_exact=(yr == true_year), year_pm1=(yr is not None and abs(yr - true_year) <= 1),
               event_hit=(bool(re.search(kw, ev, re.I)) if kw else None),
               confidence=(parsed or {}).get("confidence"))
    print(f"  {'CTRL' if control else 'anon'} {as_of} {label:20s} -> year={yr} ({'HIT' if rec['year_exact'] else 'miss'}) "
          f"event_hit={rec['event_hit']} conf={rec['confidence']} [{latency:.0f}s]", flush=True)
    return rec


def main():
    prices = load_prices()
    RAW.unlink(missing_ok=True)
    cases = [(w[0], w[1], w[2], False, i, build_case(prices, w[0], False, i)) for i, w in enumerate(WINDOWS)]
    for j, c in enumerate(CONTROLS):
        kw = next(w[2] for w in WINDOWS if w[0] == c)
        cases.append((c, "control", kw, True, 1000 + j, build_case(prices, c, True, 1000 + j)))
    random.Random(7).shuffle(cases)
    print(f"{len(cases)} cases ({len(CONTROLS)} controls), model={MODEL}, 4 parallel")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(run_case, cases))
    anon = [r for r in results if not r["control"]]
    ctrl = [r for r in results if r["control"]]
    famous = [r for r in anon if r["event_hit"] is not None]
    def rate(rs, k): return sum(1 for r in rs if r[k]) / len(rs) if rs else float("nan")
    n_years = 2025 - 2007 + 1
    summary = {
        "model": MODEL, "n_anon": len(anon), "n_control": len(ctrl), "chance_year_exact": round(1 / n_years, 3),
        "anon_year_exact": round(rate(anon, "year_exact"), 3), "anon_year_pm1": round(rate(anon, "year_pm1"), 3),
        "anon_famous_event_hit": round(rate(famous, "event_hit"), 3),
        "anon_ordinary_year_exact": round(rate([r for r in anon if r["event_hit"] is None], "year_exact"), 3),
        "control_year_exact": round(rate(ctrl, "year_exact"), 3), "control_event_hit": round(rate(ctrl, "event_hit"), 3),
        "mean_conf_hits": round(sum(r["confidence"] or 0 for r in anon if r["year_exact"]) / max(1, sum(1 for r in anon if r["year_exact"])), 2),
        "mean_conf_misses": round(sum(r["confidence"] or 0 for r in anon if not r["year_exact"]) / max(1, sum(1 for r in anon if not r["year_exact"])), 2),
        "elapsed_s": round(time.time() - t0),
    }
    OUT.write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print("\nSUMMARY:", json.dumps(summary, indent=1))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
