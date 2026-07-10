"""CLI callers for the overlay operator (Fable 5) and Claw shadow-scorer (gpt-5.5).

NEVER the Anthropic SDK / ANTHROPIC_API_KEY — always shell out to the `claude`
CLI. Pattern lifted verbatim from scripts/edge_decomposition/ai_tilt_harness.py
(call_claude_cli / call_codex_cli), which is the working, evidence-tested caller.
"""

import json
import subprocess
import time
from typing import Optional

CLI_TIMEOUT_S = 150
DEFAULT_CLAUDE_MODEL = "claude-fable-5"
DEFAULT_CODEX_MODEL = "gpt-5.5"


def extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def call_claude_cli(prompt: str, model: str = DEFAULT_CLAUDE_MODEL) -> tuple[Optional[dict], float, str]:
    """Shell out to `claude -p ... --output-format json`. Returns
    (parsed_result_or_None, latency_s, raw_text)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, "TIMEOUT"

    latency = time.monotonic() - start
    if proc.returncode != 0:
        return None, latency, f"CLI_ERROR: {proc.stderr[:500]}"

    try:
        envelope = json.loads(proc.stdout)
        result_text = envelope.get("result", "")
    except json.JSONDecodeError:
        return None, latency, f"ENVELOPE_PARSE_FAIL: {proc.stdout[:500]}"

    parsed = extract_json(result_text)
    return parsed, latency, result_text


def call_codex_cli(prompt: str, model: str = DEFAULT_CODEX_MODEL) -> tuple[Optional[dict], float, str]:
    """Shell out to `codex exec --json ...`. Returns (parsed_result_or_None,
    latency_s, raw_text). stdin redirected from /dev/null — codex exec
    otherwise blocks reading additional input."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["codex", "exec", "--model", model, "--json", prompt],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, "TIMEOUT"

    latency = time.monotonic() - start
    if proc.returncode != 0:
        return None, latency, f"CLI_ERROR: {proc.stderr[:500]}"

    result_text = ""
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            result_text = event["item"].get("text", "")

    if not result_text:
        return None, latency, f"NO_AGENT_MESSAGE: {proc.stdout[:500]}"

    parsed = extract_json(result_text)
    return parsed, latency, result_text
