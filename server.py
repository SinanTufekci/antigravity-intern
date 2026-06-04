"""Antigravity CLI (agy) bridge — fastmcp server.

Exposes Antigravity CLI as MCP tools so Claude Code (or any MCP host) can
use it as a sub-agent. Solves the headless print-mode bug in agy 1.0.x
(verified broken through 1.0.1; -p still does not print the answer on 1.0.5)
by running `agy -p` and reading the response from agy's own transcript files
instead of relying on stdout. State-file layout and transcript schema
re-verified on agy 1.0.5.

Auth: piggybacks on whatever credential store `agy` itself uses on the host
OS (Windows Credential Manager, macOS Keychain, libsecret on Linux). User
must have logged in interactively at least once via the Antigravity IDE or
`agy -i`. Uses the same AI Pro quota. The bridge itself only does cross-
platform filesystem reads under `~/.gemini/antigravity-cli/`.

Model: effectively the model set in agy's settings.json ("model" field,
e.g. Gemini 3.5 Flash (High)). agy 1.0.5 added a --model flag (and a `models`
subcommand) that IS plumbed into print mode, but switching to a DIFFERENT
model in -p hangs the call: verified on 1.0.5 that passing the already-active
label completes in seconds while any other label hangs >60s (print mode seems
to wait on an interactive/backend step it never gets headless). So the bridge
does NOT expose a model parameter — it would hang on any real switch. Change
the model via agy's settings.json instead.

Compat (re-verified on agy 1.0.5): state-file paths, last_conversations.json,
and the transcript schema are unchanged, and -p still writes the JSONL
transcript this bridge reads. agy now ALSO dual-writes every conversation to a
SQLite store at ~/.gemini/antigravity-cli/conversations/<id>.db; the 1.0.4
changelog says SQLite "will be the CLI's conversation format", so once JSONL
stops being written the bridge breaks and _read_response will need a SQLite
reader (it already raises a clear, SQLite-aware error when the transcript is
missing). The 1.0.5 -p metadata fix also stopped agy from writing metadata to
the cwd, so last_conversations.json now updates reliably under cache/.

SECURITY — read this: `agy -p` runs the model as an autonomous agent that
auto-executes its tools (read/write files, run shell commands, reach the
network) with NO approval gate and NO opt-out. Re-verified empirically on
agy 1.0.5 / Windows that print mode runs out-of-workspace writes even WITHOUT
--dangerously-skip-permissions (that flag is a no-op for -p), and that
--sandbox does not constrain filesystem egress there. agy 1.0.5 integrated a
permission system (its logs show toolPermission=request-review), but it still
does NOT gate print-mode tool execution — -p created a file outside the
workspace with no prompt. So `workspace` is only a starting context, NOT a
security boundary:
every call effectively runs arbitrary code with your privileges. Only invoke
this bridge with trusted prompts on trusted content (untrusted input here is
the classic prompt-injection "lethal trifecta"). For real isolation, run the
whole bridge inside a container or VM.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("agy")

# Logs go to stderr (stdout is the MCP protocol channel). Quiet by default;
# set AGY_BRIDGE_DEBUG=1 for per-call diagnostics. See _configure_logging.
log = logging.getLogger("agy_bridge")

AGY_DATA = Path.home() / ".gemini" / "antigravity-cli"
LAST_CONVERSATIONS = AGY_DATA / "cache" / "last_conversations.json"
BRAIN_DIR = AGY_DATA / "brain"
CONVERSATIONS_DIR = AGY_DATA / "conversations"  # agy 1.0.4+ SQLite store

# Serializes agy invocations within this process. Concurrent runs would race
# on last_conversations.json (agy rewrites it on every call), so a second
# request could pick up the first request's conversation id.
_AGY_LOCK = threading.Lock()

# Latest agy version the bridge's state-file assumptions were verified against.
# Newer agy releases may change paths/schemas (the SQLite migration is the known
# risk), so we warn at startup if the installed agy is newer than this.
VERIFIED_AGY_VERSION = (1, 0, 5)

# Poll window for the transcript/conversation-id to appear after agy exits.
# agy has already returned 0 by the time we read, so the common case resolves
# on the first attempt; the poll just absorbs filesystem-flush lag.
_RESPONSE_POLL_DEADLINE_S = 5.0
_RESPONSE_POLL_INTERVAL_S = 0.1


def _parse_agy_version(text: str) -> Optional[tuple[int, int, int]]:
    """Extract a (major, minor, patch) tuple from `agy --version` output."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _compat_warning(version: Optional[tuple[int, int, int]]) -> Optional[str]:
    """Return a warning if the installed agy is newer than we've verified.

    None if the version is unknown, equal to, or older than VERIFIED_AGY_VERSION.
    """
    if version is None or version <= VERIFIED_AGY_VERSION:
        return None
    detected = ".".join(map(str, version))
    verified = ".".join(map(str, VERIFIED_AGY_VERSION))
    return (
        f"agy {detected} is newer than the {verified} this bridge was verified "
        "against. If responses look wrong or empty, agy may have changed its "
        "state-file layout (the SQLite conversation format is the known risk). "
        "Pin a known-good agy version if needed."
    )


def _debug_enabled() -> bool:
    """True if AGY_BRIDGE_DEBUG is set to a truthy value (1/true/yes/on)."""
    return os.environ.get("AGY_BRIDGE_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_agy_version() -> Optional[str]:
    """Return `agy --version` output, or None if agy can't be run."""
    try:
        proc = subprocess.run(
            ["agy", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def _startup_checks() -> None:
    """Warn (once, at startup) if the installed agy is newer than verified."""
    warning = _compat_warning(_parse_agy_version(_get_agy_version() or ""))
    if warning:
        log.warning(warning)


def _configure_logging() -> None:
    """Route bridge logs to stderr; DEBUG when AGY_BRIDGE_DEBUG is set."""
    handler = logging.StreamHandler()  # defaults to stderr
    handler.setFormatter(logging.Formatter("[agy-bridge] %(levelname)s: %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if _debug_enabled() else logging.WARNING)
    log.propagate = False


def _normalize_workspace(ws: Optional[str]) -> str:
    return os.path.abspath(ws) if ws else os.getcwd()


def _read_last_conv_id(workspace: str) -> Optional[str]:
    if not LAST_CONVERSATIONS.exists():
        return None
    try:
        data = json.loads(LAST_CONVERSATIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if workspace in data:
        return data[workspace]
    for k, v in data.items():
        if k.lower() == workspace.lower():
            return v
    return None


def _find_newest_conv_after(start_time: float) -> Optional[str]:
    if not BRAIN_DIR.exists():
        return None
    best = None
    best_mtime = start_time - 2
    for child in BRAIN_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best = child.name
            best_mtime = mtime
    return best


def _read_response(conv_id: str) -> str:
    transcript = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.exists():
        # agy 1.0.4 added a SQLite (.db) conversation format and announced it
        # "will be the CLI's conversation format". This is where the bridge
        # breaks first if a future release stops writing JSONL transcripts.
        conv_dir = BRAIN_DIR / conv_id
        db_files = sorted(str(p) for p in conv_dir.glob("**/*.db")) if conv_dir.exists() else []
        if db_files:
            hint = (
                f" Found SQLite store(s) instead: {db_files}. agy appears to have "
                "migrated this conversation to its 1.0.4 SQLite format; the bridge's "
                "JSONL transcript reader needs updating to read from the .db file."
            )
        else:
            hint = (
                " No JSONL transcript in the conversation dir. If you upgraded agy, it "
                "may have switched to the SQLite (.db) conversation format added in 1.0.4."
            )
        raise RuntimeError(f"Transcript not found: {transcript}.{hint}")

    chunks: list[str] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            entry.get("source") == "MODEL"
            and entry.get("status") == "DONE"
            and entry.get("type") == "PLANNER_RESPONSE"
            and entry.get("content")
        ):
            chunks.append(entry["content"])

    if not chunks:
        raise RuntimeError(
            f"No completed MODEL response in transcript {transcript}. "
            "agy may have failed silently or timed out."
        )
    # Last completed planner response is the final answer (tool steps come earlier).
    return chunks[-1]


def _collect_status() -> list[tuple[str, bool, str]]:
    """Gather offline setup diagnostics as (label, ok, detail) rows.

    Spends no quota: only runs `agy --version` and inspects local state files.
    """
    rows: list[tuple[str, bool, str]] = []

    version = _parse_agy_version(_get_agy_version() or "")
    if version is None:
        rows.append(("agy CLI", False, "not found on PATH (or --version unparseable)"))
    else:
        vstr = ".".join(map(str, version))
        ok_compat = _compat_warning(version) is None
        detail = f"v{vstr} - " + ("compat OK" if ok_compat else "newer than verified")
        rows.append(("agy CLI", True, detail))

    rows.append(("base dir", AGY_DATA.exists(), str(AGY_DATA)))

    if BRAIN_DIR.is_dir():
        n = sum(1 for c in BRAIN_DIR.iterdir() if c.is_dir())
        rows.append(("brain dir", True, f"{n} conversations"))
    else:
        rows.append(("brain dir", False, str(BRAIN_DIR)))

    rows.append(("last_conversations.json", LAST_CONVERSATIONS.exists(), str(LAST_CONVERSATIONS)))

    newest = _find_newest_conv_after(0.0)
    if newest is None:
        rows.append(("newest transcript", True, "no conversations yet"))
    else:
        try:
            _read_response(newest)
            rows.append(("newest transcript", True, "readable"))
        except RuntimeError as e:
            rows.append(("newest transcript", False, str(e)[:80]))

    if CONVERSATIONS_DIR.exists():
        n = sum(1 for _ in CONVERSATIONS_DIR.glob("*.db"))
        rows.append(("SQLite store", True, f"present - {n} .db (JSONL still primary)"))
    else:
        rows.append(("SQLite store", True, "absent"))

    return rows


def _resolve_and_read(pinned_conv: Optional[str], workspace: str, start: float) -> str:
    """Resolve the conversation id for this run and return its final response.

    Resolution order: the pinned id (continue), then the workspace's recorded
    id, then the newest brain dir touched since `start`. Raises if none resolve.
    """
    conv_id = pinned_conv or _read_last_conv_id(workspace) or _find_newest_conv_after(start)
    log.debug("resolved conv_id=%s", conv_id)
    if conv_id is None:
        raise RuntimeError(
            f"No conversation found after agy run (workspace={workspace}). "
            f"Check {LAST_CONVERSATIONS} and {BRAIN_DIR}."
        )
    return _read_response(conv_id)


def _run_agy(prompt: str, workspace: str, continue_conv: bool, timeout_s: int) -> str:
    # Note: agy's `-p` mode auto-executes all tools/commands with no approval
    # gate, so we deliberately do NOT pass --dangerously-skip-permissions (it is
    # a no-op for -p) or --sandbox (verified not to constrain FS/network on
    # Windows). There is no agy flag that makes print mode safe; see the module
    # docstring's SECURITY note.
    args = ["agy", "--print-timeout", f"{timeout_s}s"]

    pinned_conv: Optional[str] = None
    if continue_conv:
        # Pin to the exact conversation rooted at this workspace instead of `-c`
        # ("most recent"), which could resume a conversation started elsewhere in
        # between. Fall back to -c only when we have no id on record yet.
        pinned_conv = _read_last_conv_id(workspace)
        if pinned_conv:
            args.extend(["--conversation", pinned_conv])
        else:
            args.append("-c")
    args.extend(["-p", prompt])

    with _AGY_LOCK:
        start = time.time()
        log.debug(
            "running agy: continue=%s pinned=%s workspace=%s timeout=%ss prompt_chars=%d",
            continue_conv,
            pinned_conv,
            workspace,
            timeout_s,
            len(prompt),
        )
        proc = subprocess.run(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
        )
        log.debug("agy exited %s in %.1fs", proc.returncode, time.time() - start)
        if proc.returncode != 0:
            raise RuntimeError(
                f"agy exited {proc.returncode}\n"
                f"stderr: {proc.stderr[-1000:]}\n"
                f"stdout: {proc.stdout[-500:]}"
            )

        # agy has already exited 0, so the transcript is usually ready at once;
        # poll briefly to absorb filesystem-flush lag instead of a fixed sleep.
        deadline = time.time() + _RESPONSE_POLL_DEADLINE_S
        while True:
            try:
                return _resolve_and_read(pinned_conv, workspace, start)
            except RuntimeError:
                # Retries transient resolution/flush lag. A persistent failure
                # (e.g. the SQLite-migration "transcript not found" from
                # _read_response) is caught here too and surfaces only after the
                # deadline; that small delay is an accepted tradeoff for keeping
                # this loop simple.
                if time.time() >= deadline:
                    raise
                time.sleep(_RESPONSE_POLL_INTERVAL_S)


@mcp.tool()
def agy_ask(prompt: str, workspace: Optional[str] = None, timeout_s: int = 180) -> str:
    """Ask Antigravity (Gemini 3.5 Flash High via agy CLI) a question in a NEW conversation.

    Uses your existing AI Pro authentication (silent-auth via Windows Credential
    Manager). Returns the model's final response as text.

    Model is fixed to Gemini 3.5 Flash (High) — agy print-mode hardcodes it.
    Good for fast tool-calling and short tasks; for heavier reasoning prefer
    the host model directly.

    Args:
        prompt: Question or instruction for Antigravity.
        workspace: Working directory for the conversation. Defaults to cwd.
                   Choose an existing project dir for context-aware responses.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
    """
    ws = _normalize_workspace(workspace)
    return _run_agy(prompt, ws, continue_conv=False, timeout_s=timeout_s)


@mcp.tool()
def agy_continue(prompt: str, workspace: Optional[str] = None, timeout_s: int = 180) -> str:
    """Continue the Antigravity conversation rooted at this workspace.

    Resumes the exact conversation id recorded for `workspace` (via agy's
    --conversation flag), not agy's global "most recent", so it stays correct
    even if agy was used elsewhere in between.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
    """
    ws = _normalize_workspace(workspace)
    return _run_agy(prompt, ws, continue_conv=True, timeout_s=timeout_s)


@mcp.tool()
def agy_status() -> str:
    """Report offline diagnostics for the agy bridge setup (spends no quota).

    Checks whether agy is on PATH (and its version/compat), whether agy's state
    directories exist, whether the newest conversation transcript is readable,
    and whether the SQLite conversation store is present. Use this to debug
    empty or failed responses before spending quota.
    """
    rows = _collect_status()
    width = max(len(label) for label, _, _ in rows)
    lines = ["agy bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


if __name__ == "__main__":
    _configure_logging()
    _startup_checks()
    mcp.run()
