#!/usr/bin/python3
"""Model freshness watchdog for the Hermes / Codex / OpenClaw stack.

Runs daily on each machine (launchd). It keeps every GPT pin on the newest
ChatGPT-Codex-entitled model and makes sure every agent profile falls back to
the local qwen server.

Why: OpenAI retires model slugs for ChatGPT-account Codex without notice
(gpt-5.4 and gpt-5.5 both died in Oct 2026). Pinned profiles then fail every
request until someone edits config by hand.

What it does, in order:
  1. Upgrades the Codex CLI if a newer npm release exists (the backend gates
     new models on client version).
  2. Reads candidate slugs from ~/.codex/models_cache.json, keeps the `-sol`
     line (plus plain gpt-N.N), sorts by version.
  3. Probes the newest candidates with `codex exec` until one answers.
  4. Rewrites every known pin to that model: Hermes profiles, ~/.codex/config.toml,
     ~/.openclaw/openclaw.json, Aurel3 config.json.
  5. Ensures each Hermes profile and OpenClaw agent falls back to local qwen.
  6. Restarts the Hermes gateway when something changed, and pings ntfy.

Usage:
  model_freshness_watchdog.py            # check + apply (what launchd runs)
  model_freshness_watchdog.py --check    # report only, write nothing
  model_freshness_watchdog.py --target gpt-6.1-sol   # skip probing, pin this
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

HOME = Path.home()
HERMES_PROFILES = HOME / ".hermes" / "profiles"
CODEX_CONFIG = HOME / ".codex" / "config.toml"
CODEX_MODELS_CACHE = HOME / ".codex" / "models_cache.json"
OPENCLAW_CONFIG = HOME / ".openclaw" / "openclaw.json"
AUREL3_CONFIG = HOME / "vps-root" / "aurel3" / "config.json"
STATE_DIR = HOME / ".hermes" / "state"
STATE_FILE = STATE_DIR / "model-freshness.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "aurel2")
HERMES_SERVICE = "ai.hermes.gateway"

# Local qwen endpoints, first reachable wins. (host, port, api_path)
# (base_url, fixed model name or None to take the server's first qwen id)
QWEN_ENDPOINTS = [
    ("http://127.0.0.1:18090/v1", "qwen-local"),  # mlx-serve on Dumbo accepts any name
    ("http://127.0.0.1:11434/v1", None),  # ollama needs the real tag
]

GPT_SLUG = re.compile(r"^gpt-(\d+(?:\.\d+)*)(?:-([a-z]+))?$")
GPT_TOKEN = re.compile(r"gpt-\d+(?:\.\d+)*(?:-[a-z]+)?")
EXCLUDED_SUFFIXES = {"mini", "nano", "pro", "codex", "spark", "chat", "terra", "luna", "astra"}
PREFERRED_SUFFIX = "sol"
MAX_PROBES = 4
PROBE_TIMEOUT_S = 180

changes: list[str] = []
notes: list[str] = []


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} model-freshness: {msg}", flush=True)


def ntfy(title: str, body: str) -> None:
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode(), headers={"Title": title}
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        log(f"ntfy failed: {exc}")


def version_key(slug: str) -> tuple:
    m = GPT_SLUG.match(slug)
    nums = tuple(int(x) for x in m.group(1).split("."))
    return nums + (0,) * (3 - len(nums))


# ---------------------------------------------------------------- codex cli

def codex_env(codex_path: str) -> dict:
    """PATH with the codex binary's own bin dir first, so `#!/usr/bin/env node`
    resolves to the node that codex was installed under (launchd has no nvm)."""
    env = dict(os.environ)
    env["PATH"] = str(Path(codex_path).parent) + ":" + env.get("PATH", "/usr/bin:/bin")
    return env


def run_codex(codex_path: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [codex_path, *args], capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, cwd="/tmp", env=codex_env(codex_path),
    )


def codex_version(codex_path: str) -> str:
    try:
        out = run_codex(codex_path, ["--version"], 30).stdout
    except Exception:  # noqa: BLE001
        return "0.0.0"
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else "0.0.0"


def codex_bin() -> str | None:
    """Newest working codex among homebrew, /usr/local, PATH and nvm installs."""
    paths = ["/opt/homebrew/bin/codex", "/usr/local/bin/codex", shutil.which("codex") or ""]
    paths += glob.glob(str(HOME / ".nvm/versions/node/*/bin/codex"))
    best, best_ver = None, (0, 0, 0)
    for p in dict.fromkeys(x for x in paths if x and os.access(x, os.X_OK)):
        ver = tuple(int(x) for x in codex_version(p).split("."))
        if ver > best_ver:
            best, best_ver = p, ver
    return best


def npm_for(codex_path: str) -> str | None:
    sib = Path(codex_path).parent / "npm"
    return str(sib) if sib.exists() else shutil.which("npm")


def upgrade_codex_if_stale(codex_path: str, apply: bool) -> None:
    npm = npm_for(codex_path)
    if not npm:
        notes.append("codex: no npm found to upgrade with")
        return
    try:
        latest = subprocess.run(
            [npm, "view", "@openai/codex", "version"], capture_output=True, text=True, timeout=60,
            env=codex_env(codex_path),
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"codex: npm view failed: {exc}")
        return
    current = codex_version(codex_path)
    if latest and latest != current:
        if apply:
            log(f"upgrading codex {current} -> {latest}")
            r = subprocess.run([npm, "install", "-g", f"@openai/codex@{latest}"], capture_output=True, text=True, timeout=600, env=codex_env(codex_path))
            if r.returncode == 0:
                changes.append(f"codex CLI {current} -> {latest}")
            else:
                notes.append(f"codex upgrade failed: {r.stderr[-300:]}")
        else:
            notes.append(f"codex CLI {current} is behind {latest}")


def probe(codex_path: str, model: str) -> tuple[bool, str]:
    """Return (entitled, detail). Costs one tiny request on the subscription."""
    try:
        r = run_codex(codex_path, ["exec", "--skip-git-repo-check", "-m", model, "Reply with exactly: OK"], PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    text = (r.stdout + r.stderr)
    if "not supported" in text or "requires a newer version" in text or '"status":400' in text:
        m = re.search(r'"message":"([^"]+)"', text)
        return False, m.group(1) if m else "rejected"
    ok = r.returncode == 0 and re.search(r"^OK\s*$", r.stdout, re.M) is not None
    return ok, "ok" if ok else f"rc={r.returncode}: {text[-200:]}"


def candidates() -> list[str]:
    try:
        data = json.loads(CODEX_MODELS_CACHE.read_text())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"models cache unreadable: {exc}")
        return []
    models = data.get("models", data) if isinstance(data, dict) else data
    slugs = {m.get("slug") for m in models if isinstance(m, dict)} if isinstance(models, list) else set()
    out = []
    for s in slugs:
        m = GPT_SLUG.match(s or "")
        if not m:
            continue
        suffix = m.group(2)
        if suffix in EXCLUDED_SUFFIXES:
            continue
        if suffix not in (None, PREFERRED_SUFFIX):
            continue
        out.append(s)
    # newest first; within a version prefer the -sol variant
    return sorted(out, key=lambda s: (version_key(s), 1 if s.endswith("-" + PREFERRED_SUFFIX) else 0), reverse=True)


def pick_target(codex_path: str, current: str | None) -> str | None:
    cands = candidates()
    if not cands:
        return current
    # newest first, so the first entitled slug is the freshest one; the current
    # pin is probed in its natural position and only kept if nothing newer works
    for slug in cands[:MAX_PROBES]:
        ok, detail = probe(codex_path, slug)
        log(f"probe {slug}: {detail}")
        if ok:
            return slug
        if slug == current:
            notes.append(f"current model {current} rejected: {detail}")
    return None


# ---------------------------------------------------------------- qwen

def detect_qwen() -> tuple[str, str] | None:
    for base, fixed in QWEN_ENDPOINTS:
        try:
            with urllib.request.urlopen(base + "/models", timeout=5) as resp:
                ids = [m.get("id") for m in json.load(resp).get("data", [])]
        except Exception:  # noqa: BLE001
            continue
        if not ids:
            continue
        qwen = [i for i in ids if "qwen" in i.lower()] or ids
        return base, fixed or qwen[0]
    return None


# ---------------------------------------------------------------- targets

def replace_gpt_token(value: str, target: str) -> str:
    return GPT_TOKEN.sub(target, value)


def current_pin() -> str | None:
    """The model Hermes profiles are on right now (first openai-codex profile)."""
    for cfg in sorted(HERMES_PROFILES.glob("*/config.yaml")):
        try:
            d = yaml.safe_load(cfg.read_text()) or {}
        except Exception:  # noqa: BLE001
            continue
        default = str((d.get("model") or {}).get("default") or "")
        m = GPT_TOKEN.search(default)
        if m:
            return m.group(0)
    if CODEX_CONFIG.exists():
        m = re.search(r'^model\s*=\s*"(gpt-[^"]+)"', CODEX_CONFIG.read_text(), re.M)
        if m:
            return m.group(1)
    return None


def backup(path: Path) -> None:
    stamp = dt.date.today().isoformat()
    dst = path.with_name(path.name + f".bak-{stamp}-model-freshness")
    if not dst.exists():
        shutil.copy2(path, dst)


def update_hermes_profiles(target: str, qwen: tuple[str, str] | None, apply: bool) -> bool:
    restart = False
    for cfg in sorted(HERMES_PROFILES.glob("*/config.yaml")):
        text = cfg.read_text()
        d = yaml.safe_load(text) or {}
        model = d.setdefault("model", {})
        default = str(model.get("default") or "")
        if not GPT_TOKEN.search(default):
            continue  # profile on another provider, leave alone
        changed = []
        new_default = replace_gpt_token(default, target)
        if new_default != default:
            model["default"] = new_default
            changed.append(f"model {default} -> {new_default}")
        if model.get("provider") != "openai-codex":
            model["provider"] = "openai-codex"
            changed.append("provider pinned to openai-codex")
        if qwen:
            base, qid = qwen
            fb = d.get("fallback_providers") or []
            if not any(isinstance(f, dict) and f.get("base_url") == base for f in fb):
                fb.append({"provider": "custom", "model": qid, "base_url": base})
                d["fallback_providers"] = fb
                changed.append(f"fallback -> local qwen ({qid})")
        if changed:
            label = f"hermes/{cfg.parent.name}"
            changes.append(f"{label}: " + "; ".join(changed))
            if apply:
                backup(cfg)
                header = "%YAML 1.1\n---\n" if text.startswith("%YAML") else ""
                cfg.write_text(header + yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
                restart = True
    return restart


def update_codex_config(target: str, apply: bool) -> None:
    if not CODEX_CONFIG.exists():
        return
    text = CODEX_CONFIG.read_text()
    new = re.sub(r'^(model\s*=\s*")(gpt-[^"]+)(")', lambda m: m.group(1) + target + m.group(3), text, count=1, flags=re.M)
    if new != text:
        old = re.search(r'^model\s*=\s*"([^"]+)"', text, re.M).group(1)
        changes.append(f"codex config.toml: model {old} -> {target}")
        if apply:
            backup(CODEX_CONFIG)
            CODEX_CONFIG.write_text(new)


def update_openclaw(target: str, qwen_id: str | None, apply: bool) -> None:
    if not OPENCLAW_CONFIG.exists():
        return
    d = json.loads(OPENCLAW_CONFIG.read_text())
    changed: list[str] = []

    def fix_model_block(m: dict, where: str) -> None:
        p = m.get("primary")
        if isinstance(p, str) and GPT_TOKEN.search(p):
            np = replace_gpt_token(p, target)
            if np != p:
                m["primary"] = np
                changed.append(f"{where} primary {p} -> {np}")
        fb = m.get("fallbacks")
        if isinstance(fb, list):
            nfb = []
            for x in fb:
                nx = replace_gpt_token(x, target) if isinstance(x, str) and GPT_TOKEN.search(x) else x
                if nx == m.get("primary"):
                    continue  # never fall back to the primary itself
                if nx not in nfb:
                    nfb.append(nx)
            if qwen_id and not any(isinstance(x, str) and x.startswith("qwen/") for x in nfb):
                nfb.append(f"qwen/{qwen_id}")
            if nfb != fb:
                m["fallbacks"] = nfb
                changed.append(f"{where} fallbacks -> {nfb}")

    def fix_models(ms: dict, where: str) -> None:
        for key in list(ms):
            if GPT_TOKEN.search(key):
                nk = replace_gpt_token(key, target)
                if nk != key:
                    val = ms.pop(key)
                    if nk not in ms:
                        ms[nk] = val
                    changed.append(f"{where} models {key} -> {nk}")

    def walk(o, where="defaults") -> None:
        if isinstance(o, dict):
            if isinstance(o.get("id"), str):
                where = o["id"]
            if isinstance(o.get("model"), dict):
                fix_model_block(o["model"], where)
            if isinstance(o.get("models"), dict):
                fix_models(o["models"], where)
            for k, v in o.items():
                if k == "providers":
                    continue  # provider catalogs list real upstream ids; leave them
                walk(v, where)
        elif isinstance(o, list):
            for v in o:
                walk(v, where)

    walk(d.get("agents", {}))
    if changed:
        changes.append("openclaw.json: " + "; ".join(changed))
        if apply:
            backup(OPENCLAW_CONFIG)
            OPENCLAW_CONFIG.write_text(json.dumps(d, indent=2) + "\n")


def openclaw_qwen_id() -> str | None:
    if not OPENCLAW_CONFIG.exists():
        return None
    try:
        d = json.loads(OPENCLAW_CONFIG.read_text())
        models = d["models"]["providers"]["qwen"]["models"]
        return models[0]["id"]
    except Exception:  # noqa: BLE001
        return None


def update_aurel3(target: str, apply: bool) -> None:
    if not AUREL3_CONFIG.exists():
        return
    text = AUREL3_CONFIG.read_text()
    new = re.sub(r'("model"\s*:\s*"openai-codex/)(gpt-[^"]+)(")', lambda m: m.group(1) + target + m.group(3), text)
    if new != text:
        changes.append(f"aurel3 config.json: model -> openai-codex/{target}")
        if apply:
            backup(AUREL3_CONFIG)
            AUREL3_CONFIG.write_text(new)


def restart_hermes_gateway() -> None:
    uid = os.getuid()
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{HERMES_SERVICE}"], capture_output=True, text=True)
    if r.returncode == 0:
        log("hermes gateway restarted")
    else:
        notes.append(f"gateway restart failed: {r.stderr.strip()}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--target", help="pin this model without probing")
    args = ap.parse_args()
    apply = not args.check
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    codex = codex_bin()
    if not codex:
        log("codex CLI not found; nothing to do")
        return 1
    upgrade_codex_if_stale(codex, apply)
    codex = codex_bin() or codex
    log(f"codex {codex_version(codex)} at {codex}")
    current = current_pin()
    target = args.target or pick_target(codex, current)
    if not target:
        msg = f"no entitled model found (current {current}); " + "; ".join(notes)
        log(msg)
        ntfy("Model freshness: no entitled GPT model", msg)
        return 2
    log(f"current={current} target={target}")

    qwen = detect_qwen()
    if qwen:
        log(f"local qwen: {qwen[1]} at {qwen[0]}")
    else:
        notes.append("no local qwen endpoint reachable; fallbacks left as they are")

    restart = update_hermes_profiles(target, qwen, apply)
    update_codex_config(target, apply)
    update_openclaw(target, openclaw_qwen_id(), apply)
    update_aurel3(target, apply)

    for c in changes:
        log(("would change: " if not apply else "changed: ") + c)
    for n in notes:
        log("note: " + n)

    if apply and restart:
        restart_hermes_gateway()

    state = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "current": current,
        "target": target,
        "changes": changes,
        "notes": notes,
        "mode": "check" if args.check else "apply",
    }
    if apply:
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
        if changes:
            ntfy(f"Model freshness: now on {target}", "\n".join(changes + notes)[:1800])
        elif notes:
            ntfy("Model freshness: attention", "\n".join(notes)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
