#!/usr/bin/env python3
"""Create disposable Aurel2 worktrees for weekly strategy research.

The weekly strategy cron should not run experiments in the protected Aurel2
checkout. This helper gives agents a throwaway worktree, captures command
results without turning every experiment failure into a cron failure, and writes
review artifacts outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPO = Path(os.environ.get("AUREL2_REPO", "/root/aurel2"))
DEFAULT_WORKTREE_ROOT = Path(
    os.environ.get("AUREL2_WEEKLY_WORKTREE_ROOT", "/private/tmp/aurel2-weekly-strategy/worktrees")
)
DEFAULT_RUNS_DIR = Path(os.environ.get("AUREL2_WEEKLY_RUNS_DIR", "/Users/claudiu/aurel2-research-runs"))

ALLOWED_REVIEW_PREFIXES = (
    "data/",
    "docs/research/",
    "docs/plans/",
    "scripts/",
    "tests/",
)

PROTECTED_PREFIXES = (
    ".env",
    "approval-endpoint/",
    "config/",
    "docker/",
    "src/aurel2/agent/",
    "src/aurel2/live/",
    "src/aurel2/strategies/",
)

INTERNAL_WORKTREE_FILES = {
    ".aurel2-weekly-workspace.json",
}


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=text,
        capture_output=capture_output,
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args], check=check)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_run_id(value: str) -> str:
    keep = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            keep.append(char)
        else:
            keep.append("-")
    return "".join(keep).strip("-")[:96] or "run"


def repo_root(repo: Path) -> Path:
    result = git(repo, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def short_status(repo: Path) -> list[str]:
    result = git(repo, "status", "--short", check=False)
    if result.returncode != 0:
        return [result.stderr.strip() or "git status failed"]
    return [line for line in result.stdout.splitlines() if line.strip()]


def status_line_path(line: str) -> str | None:
    if len(line) < 4:
        return None
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def filter_internal_status_lines(status_lines: list[str]) -> list[str]:
    return [
        line
        for line in status_lines
        if (status_line_path(line) is not None and status_line_path(line) not in INTERNAL_WORKTREE_FILES)
    ]


def parse_status_paths(status_lines: list[str]) -> list[str]:
    paths: list[str] = []
    for line in status_lines:
        path = status_line_path(line)
        if path is None or path in INTERNAL_WORKTREE_FILES:
            continue
        paths.append(path)
    return paths


def is_allowed_review_path(path: str) -> bool:
    return path.startswith(ALLOWED_REVIEW_PREFIXES)


def is_protected_path(path: str) -> bool:
    return path == ".env" or path.startswith(PROTECTED_PREFIXES)


def classify_paths(paths: list[str]) -> dict[str, list[str]]:
    protected: list[str] = []
    review_allowed: list[str] = []
    review_required: list[str] = []

    for path in paths:
        if is_protected_path(path):
            protected.append(path)
        elif is_allowed_review_path(path):
            review_allowed.append(path)
        else:
            review_required.append(path)

    return {
        "review_allowed": sorted(review_allowed),
        "review_required": sorted(review_required),
        "protected": sorted(protected),
    }


def read_meta(worktree: Path) -> dict[str, Any]:
    meta_path = worktree / ".aurel2-weekly-workspace.json"
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text())


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def ensure_git_worktree(path: Path) -> Path:
    return repo_root(path)


def command_tail(path: Path, lines: int = 80) -> list[str]:
    if not path.exists():
        return []
    content = path.read_text(errors="replace").splitlines()
    return content[-lines:]


def cmd_create(args: argparse.Namespace) -> int:
    protected_repo = repo_root(args.repo)
    base_ref = args.base_ref
    base_sha = git(protected_repo, "rev-parse", base_ref).stdout.strip()
    short_sha = base_sha[:12]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = safe_run_id(args.run_id or f"{timestamp}-{short_sha}")

    worktree_root = args.worktree_root.resolve()
    runs_dir = args.runs_dir.resolve()
    worktree = worktree_root / run_id
    run_dir = runs_dir / run_id

    if worktree.exists():
        raise SystemExit(f"worktree already exists: {worktree}")

    worktree_root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    git(protected_repo, "worktree", "add", "--detach", str(worktree), base_ref)

    protected_status = short_status(protected_repo)
    meta = {
        "created_at": utc_now(),
        "run_id": run_id,
        "protected_repo": str(protected_repo),
        "protected_repo_dirty": bool(protected_status),
        "protected_repo_status": protected_status,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "allowed_review_prefixes": list(ALLOWED_REVIEW_PREFIXES),
        "protected_prefixes": list(PROTECTED_PREFIXES),
    }
    write_json(run_dir / "workspace.json", meta)
    write_json(worktree / ".aurel2-weekly-workspace.json", meta)

    emit({"status": "ok", **meta})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    worktree = ensure_git_worktree(args.worktree.resolve())
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("run requires a command after --")

    meta = read_meta(worktree)
    run_dir = Path(args.run_dir or meta.get("run_dir") or DEFAULT_RUNS_DIR / "manual").resolve()
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"command-{int(time.time())}.log"

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{worktree / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)

    started_at = utc_now()
    started_monotonic = time.monotonic()
    status = "ok"
    exit_code: int | None = None
    timed_out = False
    error: str | None = None

    with log_path.open("w") as log_file:
        log_file.write(f"$ {' '.join(command)}\n")
        log_file.write(f"cwd={worktree}\nstarted_at={started_at}\n\n")
        log_file.flush()
        try:
            completed = subprocess.run(
                command,
                cwd=str(worktree),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=args.timeout,
                check=False,
            )
            exit_code = completed.returncode
            if completed.returncode != 0:
                status = "command_failed"
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            status = "timeout"
            error = f"command timed out after {args.timeout}s: {exc}"
            log_file.write(f"\nTIMEOUT: {error}\n")
        except OSError as exc:
            status = "runner_error"
            error = str(exc)
            log_file.write(f"\nRUNNER_ERROR: {error}\n")

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    payload = {
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "error": error,
        "command": command,
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "log_path": str(log_path),
        "duration_ms": duration_ms,
        "started_at": started_at,
        "finished_at": utc_now(),
        "log_tail": command_tail(log_path, args.tail_lines),
    }
    write_json(run_dir / "last-command.json", payload)
    emit(payload)
    return 1 if args.strict and status != "ok" else 0


def add_intent_to_add_for_untracked(worktree: Path) -> None:
    result = git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [p for p in result.stdout.split("\0") if p and p not in INTERNAL_WORKTREE_FILES]
    if paths:
        git(worktree, "add", "-N", "--", *paths)


def write_patch(worktree: Path, output: Path, paths: list[str] | None = None) -> bool:
    args = ["diff", "--binary", "HEAD"]
    if paths is not None:
        if not paths:
            output.write_text("")
            return False
        args.extend(["--", *paths])
    result = git(worktree, *args, check=False)
    output.write_text(result.stdout)
    return bool(result.stdout.strip())


def remove_worktree(protected_repo: Path, worktree: Path) -> dict[str, Any]:
    result = git(protected_repo, "worktree", "remove", "--force", str(worktree), check=False)
    if result.returncode == 0:
        return {"removed": True}
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    prune = git(protected_repo, "worktree", "prune", check=False)
    return {
        "removed": result.returncode == 0 or not worktree.exists(),
        "git_error": result.stderr.strip(),
        "prune_error": prune.stderr.strip() if prune.returncode else None,
    }


def cmd_finalize(args: argparse.Namespace) -> int:
    worktree = ensure_git_worktree(args.worktree.resolve())
    meta = read_meta(worktree)
    protected_repo = Path(args.repo or meta.get("protected_repo") or DEFAULT_REPO).resolve()
    run_dir = Path(args.run_dir or meta.get("run_dir") or DEFAULT_RUNS_DIR / "manual").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    add_intent_to_add_for_untracked(worktree)
    status_lines = filter_internal_status_lines(short_status(worktree))
    paths = parse_status_paths(status_lines)
    classified = classify_paths(paths)

    full_patch = run_dir / "full-review.patch"
    allowed_patch = run_dir / "allowed-review.patch"
    full_patch_has_content = write_patch(worktree, full_patch)
    allowed_patch_has_content = write_patch(worktree, allowed_patch, classified["review_allowed"])

    cleanup: dict[str, Any] | None = None
    if args.cleanup:
        cleanup = remove_worktree(protected_repo, worktree)

    payload = {
        "status": "ok",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "dirty": bool(status_lines),
        "status_lines": status_lines,
        "paths": classified,
        "patches": {
            "full_review_patch": str(full_patch) if full_patch_has_content else None,
            "allowed_review_patch": str(allowed_patch) if allowed_patch_has_content else None,
        },
        "cleanup": cleanup,
        "protected_repo_status": short_status(protected_repo),
        "finished_at": utc_now(),
    }
    write_json(run_dir / "finalize.json", payload)
    emit(payload)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    worktree = args.worktree.resolve()
    meta = read_meta(worktree)
    protected_repo = Path(args.repo or meta.get("protected_repo") or DEFAULT_REPO).resolve()
    payload = {
        "status": "ok",
        "worktree": str(worktree),
        "cleanup": remove_worktree(protected_repo, worktree),
        "finished_at": utc_now(),
    }
    emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    create = subparsers.add_parser("create", help="Create a disposable worktree")
    create.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    create.add_argument("--base-ref", default="HEAD")
    create.add_argument("--run-id")
    create.add_argument("--worktree-root", type=Path, default=DEFAULT_WORKTREE_ROOT)
    create.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    create.set_defaults(func=cmd_create)

    run_cmd = subparsers.add_parser("run", help="Run a command inside a disposable worktree")
    run_cmd.add_argument("--worktree", type=Path, required=True)
    run_cmd.add_argument("--run-dir", type=Path)
    run_cmd.add_argument("--timeout", type=int, default=7200)
    run_cmd.add_argument("--tail-lines", type=int, default=80)
    run_cmd.add_argument("--strict", action="store_true", help="Return non-zero when the command fails")
    run_cmd.add_argument("command", nargs=argparse.REMAINDER)
    run_cmd.set_defaults(func=cmd_run)

    finalize = subparsers.add_parser("finalize", help="Write review patches and optionally remove worktree")
    finalize.add_argument("--worktree", type=Path, required=True)
    finalize.add_argument("--repo", type=Path)
    finalize.add_argument("--run-dir", type=Path)
    finalize.add_argument("--cleanup", action="store_true")
    finalize.set_defaults(func=cmd_finalize)

    cleanup = subparsers.add_parser("cleanup", help="Remove a disposable worktree")
    cleanup.add_argument("--worktree", type=Path, required=True)
    cleanup.add_argument("--repo", type=Path)
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
