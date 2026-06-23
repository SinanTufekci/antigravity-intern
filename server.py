"""Antigravity CLI (agy) bridge — fastmcp server.

Exposes Antigravity CLI as MCP tools so Claude Code (or any MCP host) can
use it as a sub-agent. Solves the headless print-mode "stdout bug" in agy
1.0.x (verified broken through 1.0.9): `agy -p` writes its progress/answer to
the controlling terminal (TTY/console) directly, NOT to its stdout file
descriptor — so a captured-stdout read gets nothing. The bridge runs `agy -p`
and reads the real response from agy's own transcript files instead. It also
detaches agy from the host's controlling terminal when spawning it (see
_spawn_kwargs), so that direct-to-terminal output can't leak into the host TUI
— e.g. straight into Claude Code's prompt input (observed empirically on 1.0.9
before the fix). State-file layout and transcript schema re-verified on agy
1.0.10.

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

Compat (re-verified on agy 1.0.10): state-file paths, last_conversations.json,
and the transcript schema are unchanged, and a normally-completing -p run still
writes the JSONL transcript this bridge reads. (Nothing in agy 1.0.10 — bash-mode
stdout escaping, PowerShell default shell, the new alert message type, permission/
settings fixes, or rundll32 browser sign-in — touches the paths, schema, or the
print-mode TTY-leak this bridge depends on.) agy now ALSO dual-writes every
conversation to a SQLite store at ~/.gemini/antigravity-cli/conversations/<id>.db;
the 1.0.4
changelog says SQLite "will be the CLI's conversation format", so JSONL is on its
way out. _read_response handles this: it reads the JSONL transcript when present
and falls back to the SQLite store (_read_response_db) when it isn't — already the
case for --sandbox runs — so the bridge keeps working once JSONL goes away. The
1.0.5 -p metadata fix also stopped agy from writing metadata to the cwd, so
last_conversations.json now updates reliably under cache/.

SECURITY — read this: `agy -p` runs the model as an autonomous agent that
auto-executes its tools (read/write files, run shell commands, reach the
network) with NO approval gate and NO opt-out. Re-verified empirically on
agy 1.0.9 / Windows that print mode runs out-of-workspace writes even WITHOUT
--dangerously-skip-permissions (that flag is a no-op for -p). agy 1.0.5
integrated a permission system (its logs show toolPermission=request-review),
but it still does NOT gate print-mode tool execution — -p created a file
outside the workspace with no prompt.

--sandbox is NOT a usable safety knob for this bridge. agy 1.0.6 fixed
--sandbox flag propagation into -p (its 1.0.6 changelog calls this "sandbox
isolation correctly enforced"), and verified here it now DOES block terminal/
shell command execution in print mode. But that "isolation" is partial and
misleadingly named: re-verified on 1.0.9 that under --sandbox the model still
wrote a file OUTSIDE its workspace via the write_to_file tool — so --sandbox
does NOT constrain filesystem writes or network egress, only the terminal.
(agy 1.0.9 hardened the sandbox's command path — stricter exact-match command
checks, .git added to its dangerous-paths list — but none of that closes the
out-of-workspace write_to_file hole.) Worse for us, a --sandbox run that hits
a blocked terminal command writes NO JSONL transcript (only the SQLite .db, as
re-confirmed on 1.0.9), so the bridge would fail to read a response.
For both reasons the bridge deliberately does NOT pass --sandbox; there is
still no agy flag that makes print mode safe.

So `workspace` is only a starting context, NOT a security boundary:
every call effectively runs arbitrary code with your privileges. Only invoke
this bridge with trusted prompts on trusted content (untrusted input here is
the classic prompt-injection "lethal trifecta"). For real isolation, run the
whole bridge inside a container or VM.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from fastmcp import Context, FastMCP

import codex_bridge

mcp = FastMCP("agent-intern")

# The running bridge's version — the source of truth is THIS file (not the
# installed package metadata, which goes stale on editable installs). Keep in
# sync with pyproject.toml's version. Compared at startup against the latest
# tag on GitHub so a long-lived clone learns when to `git pull`.
__version__ = "0.14.1"

# Logs go to stderr (stdout is the MCP protocol channel). Quiet by default;
# set AGY_BRIDGE_DEBUG=1 for per-call diagnostics. See _configure_logging.
log = logging.getLogger("agy_bridge")

# The agy executable to invoke. Defaults to "agy" (resolved via PATH); set the
# AGY_BIN env var to an explicit path when agy isn't reliably on PATH — e.g. on
# Windows where a new terminal/reboot can drop it:
#   AGY_BIN=%LOCALAPPDATA%\agy\bin\agy.exe
# Read once at import; the launching process's environment wins.
AGY_BIN = os.environ.get("AGY_BIN", "agy")

# GitHub repo polled at startup for a newer release tag. Override AGY_BRIDGE_REPO
# if you run a fork; set AGY_BRIDGE_NO_UPDATE_CHECK=1 to skip the check entirely.
GITHUB_REPO = os.environ.get("AGY_BRIDGE_REPO", "SinanTufekci/agent-intern")

AGY_DATA = Path.home() / ".gemini" / "antigravity-cli"
LAST_CONVERSATIONS = AGY_DATA / "cache" / "last_conversations.json"
BRAIN_DIR = AGY_DATA / "brain"
CONVERSATIONS_DIR = AGY_DATA / "conversations"  # agy 1.0.4+ SQLite store
# agy saves generated images here when not given an explicit absolute save path
SCRATCH_DIR = AGY_DATA / "scratch"

# Serializes agy invocations within this process. Concurrent runs would race
# on last_conversations.json (agy rewrites it on every call), so a second
# request could pick up the first request's conversation id.
_AGY_LOCK = threading.Lock()

# Latest agy version the bridge's state-file assumptions were verified against.
# Newer agy releases may change paths/schemas (the SQLite migration is the known
# risk), so we warn at startup if the installed agy is newer than this.
VERIFIED_AGY_VERSION = (1, 0, 10)

# Poll window for the transcript/conversation-id to appear after agy exits.
# agy has already returned 0 by the time we read, so the common case resolves
# on the first attempt; the poll just absorbs filesystem-flush lag.
_RESPONSE_POLL_DEADLINE_S = 5.0
_RESPONSE_POLL_INTERVAL_S = 0.1

# How often the streaming runner re-reads the transcript to emit progress while
# agy is still working. agy flushes the transcript in coarse chunks (verified on
# 1.0.9: it can stay empty for ~15 s then append several entries at once), so
# progress is deliberately coarse — a handful of ticks per run, not token-level.
_PROGRESS_POLL_INTERVAL_S = 0.4

# How often to emit an MCP progress notification while a blocking agy run is in
# flight (see _run_with_progress). agy reports no real percentage, so progress is
# a coarse time bar (elapsed / timeout); ~1 s keeps clients' bars moving without
# spamming notifications.
_PROGRESS_NOTIFY_INTERVAL_S = 1.0


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


def _env_truthy(name: str) -> bool:
    """True if env var `name` is set to a truthy value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_enabled() -> bool:
    """True if AGY_BRIDGE_DEBUG is set to a truthy value (1/true/yes/on)."""
    return _env_truthy("AGY_BRIDGE_DEBUG")


def _fetch_latest_release_version() -> Optional[tuple[int, int, int]]:
    """Best-effort: the highest semver tag published on GITHUB_REPO, or None.

    Hits GitHub's public tags API (no auth) with a short timeout. ANY failure —
    offline, DNS, rate-limit, HTTP error, unexpected JSON — returns None so the
    server never blocks or errors on the network at startup.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-intern-bridge",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(tags, list):  # e.g. a {"message": "rate limit"} error body
        return None
    versions = [
        v
        for v in (_parse_agy_version(t.get("name", "")) for t in tags if isinstance(t, dict))
        if v is not None
    ]
    return max(versions) if versions else None


def _update_warning(latest: Optional[tuple[int, int, int]]) -> Optional[str]:
    """Return a warning if `latest` is a newer bridge version than this file.

    None if no newer release is known, or if either version can't be parsed.
    """
    current = _parse_agy_version(__version__)
    if latest is None or current is None or latest <= current:
        return None
    newest = ".".join(map(str, latest))
    return (
        f"A newer Agent Intern bridge is available: v{newest} "
        f"(you are running v{__version__}). Update with `git pull` in the repo, "
        "then restart Claude Code. Set AGY_BRIDGE_NO_UPDATE_CHECK=1 to silence this."
    )


def _spawn_kwargs(name: str = "") -> dict:
    """Extra subprocess kwargs that detach agy from the host's controlling terminal.

    agy -p writes its progress/answer to the controlling terminal (TTY/console)
    directly, NOT to its stdout file descriptor — which is both why capturing
    stdout yields nothing AND why, when run under an interactive terminal, agy's
    text leaks into the host (e.g. straight into Claude Code's TUI prompt input).
    Detaching gives agy no terminal to write to; the bridge still reads the real
    answer from the transcript file. Verified on agy 1.0.9 / Windows that this
    does not change what the bridge captures (the response is read from the
    transcript regardless). Windows: CREATE_NO_WINDOW. POSIX: a new session
    (no controlling tty).

    `name` overrides the platform (defaults to os.name) so both branches stay
    unit-testable without globally mutating os.name — which would break pathlib
    (and pytest's own bookkeeping) on non-Windows hosts.
    """
    if (name or os.name) == "nt":
        # CREATE_NO_WINDOW is Windows-only; the literal fallback lets the "nt"
        # branch be exercised on any OS (the value is only ever used on Windows).
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _get_agy_version() -> Optional[str]:
    """Return `agy --version` output, or None if agy can't be run."""
    try:
        proc = subprocess.run(
            [AGY_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def _startup_checks() -> None:
    """Warn (once, at startup) about a stale agy or a newer bridge release.

    Both checks are best-effort and non-fatal: the agy check runs `agy --version`
    locally; the update check polls GitHub (skipped via AGY_BRIDGE_NO_UPDATE_CHECK,
    silent on any network failure).
    """
    agy_warning = _compat_warning(_parse_agy_version(_get_agy_version() or ""))
    if agy_warning:
        log.warning(agy_warning)
    if not _env_truthy("AGY_BRIDGE_NO_UPDATE_CHECK"):
        update_warning = _update_warning(_fetch_latest_release_version())
        if update_warning:
            log.warning(update_warning)


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


# --- minimal protobuf wire reader, for agy's SQLite `steps.step_payload` blobs ---
# agy 1.0.4 added a SQLite conversation store and the changelog says it "will be
# the CLI's conversation format". When agy stops writing the JSONL transcript the
# bridge falls back to reading the .db (see _read_response_db). The schema is
# undocumented; these helpers walk the protobuf wire format well enough to pull
# the final answer — verified against the JSONL transcript across 114 local
# conversations (104 byte-identical, 10 a longer superset, 0 wrong).
def _pb_varint(buf: bytes, i: int):
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _pb_fields(buf: bytes) -> list:
    """(field_number, wire_type, value) for each field in a protobuf message.
    value is raw bytes for length-delimited fields and the int for varints; other
    wire types are skipped. Best-effort — stops on malformed input, never raises."""
    out: list = []
    i, n = 0, len(buf)
    while i < n:
        try:
            tag, i = _pb_varint(buf, i)
            field, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _pb_varint(buf, i)
                out.append((field, 0, v))
            elif wt == 2:
                ln, i = _pb_varint(buf, i)
                out.append((field, 2, buf[i : i + ln]))
                i += ln
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
        except IndexError:
            break
    return out


def _pb_bytes(fields: list, num: int) -> list:
    """The length-delimited values of field `num` (bytes / string / sub-message)."""
    return [v for f, wt, v in fields if f == num and wt == 2]


# step_type / status codes in the SQLite `steps` table, reverse-engineered to
# mirror the JSONL transcript's type=PLANNER_RESPONSE / status=DONE filter.
_DB_PLANNER_RESPONSE = 15
_DB_STATUS_DONE = 3


def _read_response_db(conv_id: str) -> Optional[str]:
    """Final planner answer from agy's SQLite store (`conversations/<id>.db`).

    Mirrors _read_response's JSONL logic — the last completed planner-response
    step's text — read from the `steps` table (step_payload protobuf: the
    sub-message at field 20, its string at field 1). Returns None if the .db is
    missing/unreadable or has no such step, so the caller can fall through to a
    clear error. Best-effort: agy's schema is undocumented and may change."""
    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT step_payload FROM steps WHERE step_type=? AND status=? ORDER BY idx",
                (_DB_PLANNER_RESPONSE, _DB_STATUS_DONE),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    answer: Optional[str] = None
    for (payload,) in rows:
        if not payload:
            continue
        for sub in _pb_bytes(_pb_fields(payload), 20):
            for text in _pb_bytes(_pb_fields(sub), 1):
                try:
                    decoded = text.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if decoded.strip():
                    answer = decoded
    return answer


def _read_response(conv_id: str) -> str:
    """Final model answer for a conversation: the last completed planner response.

    Reads agy's JSONL transcript (the fast path) and falls back to its SQLite
    conversation store when the JSONL is missing or empty. That fallback matters
    today (a --sandbox run writes no JSONL, only the .db) and is the migration agy
    has announced — so the bridge keeps working once JSONL goes away entirely."""
    transcript = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    chunks: list[str] = []
    if transcript.exists():
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
    if chunks:
        # Last completed planner response is the final answer (tool steps come earlier).
        return chunks[-1]

    # JSONL absent or empty — fall back to the SQLite (.db) store.
    db_answer = _read_response_db(conv_id)
    if db_answer is not None:
        return db_answer

    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not transcript.exists():
        raise RuntimeError(
            f"No transcript for conversation {conv_id}: neither the JSONL ({transcript}) "
            f"nor a readable SQLite store ({db_path}) yielded a completed planner response. "
            "If you upgraded agy, its conversation format may have changed in a way the "
            "bridge can't yet parse."
        )
    raise RuntimeError(
        f"No completed MODEL response in transcript {transcript} (and no usable SQLite "
        f"fallback at {db_path}). agy may have failed silently or timed out."
    )


def _transcript_entries(conv_id: str) -> list[dict]:
    """All parsed JSONL entries for a conversation, or [] if no transcript yet.

    Unlike _read_response this is non-raising and returns every entry (not just
    the final answer) — it's the live feed the streaming runner polls for
    progress. Re-reads the whole file each call; transcripts are small (a handful
    of entries per turn), so that's cheap enough for the poll loop.
    """
    transcript = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.exists():
        return []
    out: list[dict] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _clean_tool_arg(value) -> str:
    """Unwrap a tool-call arg. agy stores them JSON-encoded (a quoted/escaped
    string inside the string), so one json.loads turns e.g. CommandLine into the
    real command. Falls back to the raw value if it isn't double-encoded."""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    try:
        decoded = json.loads(value)
        if isinstance(decoded, str):
            return decoded.strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return value.strip()


def _entry_to_watch_lines(entry: dict) -> list[tuple[str, str]]:
    """Richer per-entry breakdown for the watch window: the model's narration,
    the ACTUAL command it runs (from tool_calls), and a command-finished marker.

    Returns a list of (kind, text) where kind is 'narration' | 'command' |
    'result'; the viewer maps kind to colour/symbol. A single planner step can
    yield two lines (its narration + the actual command it runs).
    """
    if entry.get("source") != "MODEL":
        return []
    etype = entry.get("type")
    lines: list[tuple[str, str]] = []
    if etype == "PLANNER_RESPONSE":
        content = entry.get("content")
        if content:
            lines.append(("narration", content.strip().splitlines()[0][:200]))
        for call in entry.get("tool_calls") or []:
            args = (call or {}).get("args") or {}
            cmd = _clean_tool_arg(args.get("CommandLine"))
            if not cmd:
                cmd = _clean_tool_arg(args.get("toolSummary") or args.get("toolAction"))
            if cmd:
                lines.append(("command", cmd[:200]))
    elif etype == "RUN_COMMAND":
        lines.append(("result", "command finished"))
    return lines


# Canonical extension per detected image format. Drives extension-correction:
# agy's image model picks the format itself (JPEG for photos, PNG for flat
# graphics), regardless of the requested filename's extension.
_IMAGE_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}


def _detect_image_format(path: str) -> Optional[str]:
    """Sniff an image format from a file's magic bytes, or None if not an image."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:4] == b"GIF8":
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


def _canonical_ext(fmt: str) -> str:
    """Canonical file extension (with dot) for a detected image format."""
    return _IMAGE_EXT[fmt]


def _with_ext(path: str, ext: str) -> str:
    """Return `path` with its extension replaced by `ext` (e.g. '.jpg')."""
    return os.path.splitext(path)[0] + ext


def _resolve_output_path(output_path: Optional[str], workspace: str) -> str:
    """Resolve the absolute target path for a generated image.

    Omitted -> a timestamped default under `workspace`; relative -> joined to
    `workspace`; absolute -> used as-is. The extension may still be corrected
    after generation (agy picks JPEG or PNG itself, regardless of the name).
    """
    if not output_path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return os.path.join(workspace, f"agy-image-{stamp}Z.png")
    if os.path.isabs(output_path):
        return os.path.abspath(output_path)
    return os.path.abspath(os.path.join(workspace, output_path))


def _newest_scratch_image_after(start: float) -> Optional[str]:
    """Newest recognized image in agy's scratch dir, modified at/after `start`
    (with a ~2 s buffer to absorb filesystem timestamp lag).

    agy falls back to ~/.gemini/antigravity-cli/scratch/ when not given an
    explicit absolute save path. Returns an absolute path string, or None.
    """
    if not SCRATCH_DIR.exists():
        return None
    best: Optional[str] = None
    best_mtime = start - 2
    for child in SCRATCH_DIR.iterdir():
        if not child.is_file():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime and _detect_image_format(str(child)):
            best = str(child)
            best_mtime = mtime
    return best


def _wrap_image_prompt(prompt: str, target: str) -> str:
    """Wrap a user image prompt with an explicit save path + path-only reply.

    agy honours an explicit absolute path; without one it falls back to its own
    scratch dir. Asking it to reply with only the path gives a reliable hint for
    locating the file.
    """
    base = prompt.rstrip()
    sep = "" if base.endswith(".") else "."
    return (
        f"{base}{sep} Save the generated image to this exact absolute path: "
        f"{target} . After saving, reply with ONLY the absolute file path where "
        f"you actually saved the image, nothing else."
    )


def _finalize_image(target: str, agy_text: Optional[str], start: float) -> tuple[str, str, int]:
    """Locate the generated image, move it to `target` (with its extension
    corrected to the real magic-byte format), and return path + format + size.

    Candidate order: the resolved `target`, then an absolute path agy reported in
    `agy_text`, then the newest image in the scratch dir created at/after `start`.
    Renames to the canonical extension for the real (magic-byte) format, so the
    returned path never lies about its bytes.

    Returns (final_path, format, size_bytes). Raises RuntimeError if no image
    file is found, or if the located file is not a recognized image.
    """
    candidates = [target]
    if agy_text and agy_text.strip():
        # agy may add prose after the path; take the first non-empty line.
        candidates.append(agy_text.strip().splitlines()[0].strip().strip('"'))
    scratch = _newest_scratch_image_after(start)
    if scratch:
        candidates.append(scratch)

    src = next((c for c in candidates if c and os.path.isfile(c)), None)
    if src is None:
        raise RuntimeError(
            f"antigravity_image: no image file found. Looked at target {target!r} and "
            f"scratch dir {SCRATCH_DIR}."
        )

    fmt = _detect_image_format(src)
    if fmt is None:
        raise RuntimeError(
            f"antigravity_image: {src!r} is not a recognized image. agy may have refused "
            "the request or returned text instead of an image."
        )

    final_path = _with_ext(target, _canonical_ext(fmt))
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(final_path):
        if os.path.exists(final_path):
            os.remove(final_path)
        shutil.move(src, final_path)
    return final_path, fmt, os.path.getsize(final_path)


def _bridge_version_status() -> tuple[str, bool, str]:
    """Status row for the bridge's own version and whether a newer release exists.

    Always reports ok=True — an available update is informational, not a fault, so
    it must not flip the overall status to PROBLEMS FOUND. Honors
    AGY_BRIDGE_NO_UPDATE_CHECK and stays ok (just uninformative) when GitHub is
    unreachable. This is what surfaces the update notice in an MCP client's chat
    (the startup stderr warning only lands in the host's logs).
    """
    label = "bridge version"
    if _env_truthy("AGY_BRIDGE_NO_UPDATE_CHECK"):
        return (label, True, f"v{__version__} (update check disabled)")
    latest = _fetch_latest_release_version()
    if latest is None:
        return (label, True, f"v{__version__} (update check unavailable — offline?)")
    current = _parse_agy_version(__version__)
    if current is not None and latest > current:
        newest = ".".join(map(str, latest))
        return (
            label,
            True,
            f"v{__version__} -> v{newest} available; upgrade: uvx agent-intern@latest",
        )
    return (label, True, f"v{__version__} (latest)")


def _collect_status() -> list[tuple[str, bool, str]]:
    """Gather setup diagnostics as (label, ok, detail) rows.

    Spends no AI Pro quota: runs `agy --version`, inspects local state files, and
    (unless AGY_BRIDGE_NO_UPDATE_CHECK is set) makes one best-effort GitHub call to
    report whether a newer bridge release exists.
    """
    rows: list[tuple[str, bool, str]] = [_bridge_version_status()]

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


def _build_agy_args(
    prompt: str, workspace: str, continue_conv: bool, timeout_s: int
) -> tuple[list[str], Optional[str]]:
    """Build agy's argv and resolve the pinned conversation id for continue mode.

    Note: agy's `-p` mode auto-executes all tools/commands with no approval gate,
    so we deliberately do NOT pass --dangerously-skip-permissions (a no-op for -p)
    or --sandbox. On 1.0.6+ --sandbox blocks only terminal/shell commands, not
    write_to_file/FS or network egress, so it is no real boundary; and a
    sandbox-blocked terminal run writes no JSONL transcript for us to read. There
    is no agy flag that makes print mode safe; see the module docstring's SECURITY
    note.
    """
    args = [AGY_BIN, "--print-timeout", f"{timeout_s}s"]
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
    return args, pinned_conv


def _run_agy(prompt: str, workspace: str, continue_conv: bool, timeout_s: int) -> str:
    args, pinned_conv = _build_agy_args(prompt, workspace, continue_conv, timeout_s)

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
            **_spawn_kwargs(),  # keep agy's TTY writes out of the host terminal
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


async def _run_with_progress(
    run_fn, args: tuple, ctx: "Optional[Context]", timeout_s: int, label: str = "agy"
) -> str:
    """Run a blocking CLI call off the event loop, emitting MCP progress while it works.

    `run_fn(*args)` is the synchronous runner (e.g. _run_agy or
    codex_bridge.run_codex); it executes in a worker thread so the event loop stays
    free to send progress. When `ctx` is None — direct/test calls, or a client that
    sent no progressToken — this is just a threaded call with no notifications.
    Progress is a coarse time bar (elapsed / timeout_s): neither CLI exposes a real
    percentage, so a smooth elapsed fraction is the honest approximation. `label`
    names the backend in the progress message ("agy" or "codex"). Progress
    reporting is best-effort and never fails the run.
    """
    if ctx is None:
        return await asyncio.to_thread(run_fn, *args)

    task = asyncio.ensure_future(asyncio.to_thread(run_fn, *args))
    start = time.monotonic()
    while not task.done():
        await asyncio.sleep(_PROGRESS_NOTIFY_INTERVAL_S)
        elapsed = time.monotonic() - start
        try:
            await ctx.report_progress(
                progress=min(elapsed, float(timeout_s)),
                total=float(timeout_s),
                message=f"{label} running ({int(elapsed)}s)",
            )
        except Exception:  # noqa: BLE001 — progress is cosmetic; never break the run
            pass
    return await task  # re-raises any error from the worker thread


def _existing_conv_names() -> set[str]:
    """Names of brain conversation dirs that exist right now (snapshot)."""
    if not BRAIN_DIR.exists():
        return set()
    return {c.name for c in BRAIN_DIR.iterdir() if c.is_dir()}


def _newest_new_conv(start: float, exclude: set[str]) -> Optional[str]:
    """Newest brain dir touched since `start` whose name is NOT in `exclude`.

    Used to lock streaming onto *this* run's brand-new conversation, ignoring any
    other recently-finished one — without this, agy's initial blind window (the
    transcript can stay empty ~15 s) would resolve to a prior conversation and
    emit its steps as if they were ours.
    """
    if not BRAIN_DIR.exists():
        return None
    best, best_mtime = None, start - 2
    for child in BRAIN_DIR.iterdir():
        if not child.is_dir() or child.name in exclude:
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = child.name, mtime
    return best


# Live "watch" viewer state, served over a localhost HTTP server to a browser
# page. One server + one shared state per bridge process (agy runs are serialized
# by _AGY_LOCK, so only one run writes at a time). The browser polls /events.
_WATCH_STATE: dict = {
    "title": "",
    "status": "idle",  # idle | working | done | error
    "started": 0.0,
    "elapsed": 0.0,
    "timeout": 0.0,  # this run's timeout_s, so the page can draw a time progress bar
    "answer": "",
    "image": "",  # absolute path to a generated image to show, or ""
    "events": [],  # list of {kind, text, t}
    "backend": "agy",  # which CLI this run drives: "agy" | "codex" (shown in the header)
}
_WATCH_STATE_LOCK = threading.Lock()
_WATCH_SERVER: Optional[tuple] = None  # (httpd, port, thread) singleton

# An open viewer polls /events a few times a second; we record the last poll so a
# new run can REUSE an already-open window instead of stacking a fresh one each
# time watch mode is used (the page resets itself when `started` changes).
_WATCH_LAST_POLL = 0.0
_VIEWER_ALIVE_S = 4.0  # a poll within this window means a viewer is still open


def _watch_reset(title: str, start: float, timeout: float = 0.0, backend: str = "agy") -> None:
    with _WATCH_STATE_LOCK:
        _WATCH_STATE.update(
            title=title,
            status="working",
            started=start,
            elapsed=0.0,
            timeout=timeout,
            answer="",
            image="",
            events=[],
            backend=backend,
        )


def _watch_set_image(path: str) -> None:
    with _WATCH_STATE_LOCK:
        _WATCH_STATE["image"] = path


def _watch_append(events: list[dict]) -> None:
    with _WATCH_STATE_LOCK:
        _WATCH_STATE["events"].extend(events)
        _WATCH_STATE["elapsed"] = round(time.time() - _WATCH_STATE["started"], 1)


def _watch_finish(status: str, answer: str, elapsed: float) -> None:
    with _WATCH_STATE_LOCK:
        _WATCH_STATE["status"] = status
        _WATCH_STATE["answer"] = answer
        _WATCH_STATE["elapsed"] = round(elapsed, 1)


def _watch_snapshot() -> dict:
    with _WATCH_STATE_LOCK:
        snap = dict(_WATCH_STATE)
        snap["events"] = list(_WATCH_STATE["events"])
        return snap


class _WatchFeed:
    """Locks onto this run's conversation and turns new transcript entries into
    rich step events (narration / command / result) appended to the shared watch
    state. For a new conversation it locks onto the first brain dir that appears
    after launch and didn't pre-exist, and never switches away from it."""

    def __init__(self, pinned_conv: Optional[str], start: float) -> None:
        self._start = start
        self._pre = set() if pinned_conv else _existing_conv_names()
        self._conv = pinned_conv
        self._cursor = len(_transcript_entries(pinned_conv)) if pinned_conv else 0

    @property
    def conv(self) -> Optional[str]:
        return self._conv

    def pump(self) -> None:
        if self._conv is None:
            self._conv = _newest_new_conv(self._start, self._pre)
            if self._conv is None:
                return
            self._cursor = 0
        entries = _transcript_entries(self._conv)
        new_events = []
        for entry in entries[self._cursor :]:
            for kind, text in _entry_to_watch_lines(entry):
                t = round(time.time() - self._start, 1)
                new_events.append({"kind": kind, "text": text, "t": t})
        self._cursor = max(self._cursor, len(entries))
        if new_events:
            _watch_append(new_events)


# Self-contained dark-theme page: polls /events and renders steps live, with a
# spinner while working and the final answer card on completion. Resets its view
# when `started` changes, so one browser tab can be reused across runs.
# Terminal-styled page with typewriter step reveal and a Markdown-rendered answer.
# __WIN_W__/__WIN_H__ are substituted per request (see _watch_html).
_WATCH_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="google" content="notranslate">
<title>Agent Intern — watching agy</title>
<style>
:root{
 --bg:#0a0c10;--fg:#d6d6d6;--dim:#677;--green:#3fdf7f;--cyan:#5cd6e6;
 --bd:#191c22;--code:#06080b;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg)}
body{
 color:var(--fg);padding-bottom:30px;
 font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace;
}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d}
.top{position:sticky;top:0;z-index:3;background:var(--bg)}
header{
 display:flex;align-items:center;gap:8px;
 padding:8px 13px;background:#0d0f14;border-bottom:1px solid var(--bd);
 font-size:12px;color:var(--dim);
}
.name{color:var(--green);font-weight:700;text-shadow:0 0 10px rgba(63,223,127,.4)}
.wlabel{color:var(--dim)}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px}
.dot{
 width:7px;height:7px;border-radius:50%;background:var(--green);
 box-shadow:0 0 9px var(--green);
}
.spin{
 color:var(--green);display:inline-block;width:9px;text-align:center;
 text-shadow:0 0 8px rgba(63,223,127,.6);
}
#elapsed{color:#556}
main{padding:11px 14px}
.sub{
 position:relative;color:var(--dim);font-size:12px;padding:8px 14px 9px;
 cursor:pointer;border-bottom:1px solid var(--bd);
 max-height:3.6em;overflow:hidden;transition:max-height .2s ease;
}
.sub:hover{background:#0f1116}
.sub .tag{
 color:var(--green);font-weight:700;font-size:9.5px;letter-spacing:1.6px;
 margin-right:8px;
}
.sub .chev{float:right;color:var(--green);opacity:.8;margin-left:8px}
.subtext{white-space:pre-wrap;word-break:break-word}
.sub::after{
 content:"";position:absolute;left:0;right:0;bottom:0;height:15px;
 background:linear-gradient(transparent,var(--bg));pointer-events:none;
}
.sub.expanded{max-height:46vh;overflow:auto}
.sub.expanded::after{display:none}
.row{
 display:flex;gap:9px;align-items:baseline;padding:2px 0;
 animation:slide .22s ease both;
}
@keyframes slide{from{opacity:0;transform:translateX(-6px)}}
.t{color:#4a4f57;min-width:50px;text-align:right;font-size:11px}
.sym{width:10px;flex:none}
.txt{white-space:pre-wrap;word-break:break-word}
.command .sym{color:var(--green)}
.command .txt{color:#f2f2f2}
.narration .sym,.narration .txt{color:var(--cyan)}
.result .sym,.result .txt{color:var(--green);opacity:.5}
.cur{
 display:inline-block;width:7px;height:14px;background:var(--green);
 vertical-align:-2px;box-shadow:0 0 8px var(--green);
 animation:blink 1.05s steps(1) infinite;
}
@keyframes blink{50%{opacity:0}}
.sep{
 display:flex;align-items:center;gap:10px;margin:22px 0 12px;
 animation:slide .3s ease both;
}
.sep::before,.sep::after{content:"";height:1px;background:var(--bd);flex:1}
.seplabel{
 font-size:10px;letter-spacing:2.5px;color:var(--green);font-weight:700;opacity:.9;
}
.answer{
 animation:fade .4s ease both;background:#0c0e13;
 border:1px solid var(--bd);border-radius:8px;padding:13px 15px;
}
@keyframes fade{from{opacity:0}}
.answer .h{font-weight:700;margin:13px 0 5px;color:#cdd9e5}
.answer .h1{font-size:16px;color:#fff}
.answer .h2{font-size:14px}
.answer .h3{font-size:12.5px;color:var(--green)}
.answer .p{margin:3px 0;white-space:pre-wrap;word-break:break-word}
.answer .li{display:flex;gap:8px;margin:2px 0}
.answer .bul{color:var(--green);flex:none;min-width:14px;text-align:right}
.answer .lit{white-space:pre-wrap;word-break:break-word}
.answer pre.code{
 background:var(--code);border-left:2px solid var(--green);border-radius:4px;
 padding:9px 11px;margin:7px 0;overflow:auto;white-space:pre;color:#e9efe9;
}
.answer code{background:#16191f;padding:1px 5px;border-radius:4px;color:#9fe6ad}
.answer .lnk{color:var(--cyan);border-bottom:1px dotted #2a6b73}
.answer strong{color:#fff}
.shot{
 max-width:100%;border:1px solid var(--bd);border-radius:8px;
 margin:10px 0 4px;display:block;animation:fade .4s ease both;
}
.hint{
 position:fixed;bottom:7px;right:12px;color:#3b414a;font-size:10.5px;
 pointer-events:none;user-select:none;
}
.gbar{height:2px;background:#11141a}
.gfill{
 height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));
 box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .5s linear;
}
.dot{animation:pop .45s ease}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.answer{position:relative}
.copy{
 position:absolute;top:8px;right:9px;background:#11151c;border:1px solid var(--bd);
 color:var(--dim);font:inherit;font-size:10.5px;padding:2px 9px;border-radius:5px;
 cursor:pointer;opacity:.55;transition:opacity .15s,color .15s,border-color .15s;
}
.copy:hover{opacity:1;color:var(--green);border-color:#2a3340}
.jump{
 position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#12161d;
 border:1px solid #2a3340;color:var(--cyan);font-size:11.5px;padding:5px 13px;
 border-radius:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.5);
 animation:fade .3s;z-index:4;
}
</style></head><body>
<div class="top">
 <header>
  <span class="name">Agent Intern</span><span class="wlabel" id="wlabel">— watching agy</span>
  <span class="pill" id="pill">
   <span class="dot" id="dot" style="display:none"></span>
   <span class="spin" id="spin"></span>
   <span id="status">working</span><span id="elapsed"></span>
  </span>
 </header>
 <div class="gbar"><div class="gfill" id="gfill"></div></div>
 <div class="sub" id="sub" title="click to expand / collapse">
  <span class="chev" id="chev">▾</span><span class="tag">PROMPT</span><span
   class="subtext" id="subtext"></span>
 </div>
</div>
<main>
 <div id="steps"></div>
 <div id="live"><span class="cur"></span></div>
 <div id="answerWrap"></div>
</main>
<div class="jump" id="jump" style="display:none">↓ jump to latest</div>
<div class="hint">⏎ / esc · close</div>
<script>
try{window.resizeTo(__WIN_W__,__WIN_H__);}catch(e){}
document.addEventListener("keydown",e=>{
 if(e.key==="Enter"||e.key==="Escape"){try{window.close();}catch(_){}}
});
const SYM={narration:"▸",command:"$",result:"✓"};
let seen=0,started=null,finished=false,tq=[],typing=false,follow=true;
const $=id=>document.getElementById(id);
$("sub").addEventListener("click",()=>{
 const ex=$("sub").classList.toggle("expanded");$("chev").textContent=ex?"▴":"▾";
});
function toBottom(){window.scrollTo(0,document.body.scrollHeight);}
function maybeBottom(){if(follow)toBottom();}
window.addEventListener("scroll",()=>{
 follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-44;
 $("jump").style.display=follow?"none":"";
});
$("jump").addEventListener("click",()=>{follow=true;$("jump").style.display="none";toBottom();});
function copyText(txt,btn){
 navigator.clipboard.writeText(txt).then(()=>{
  const o=btn.textContent;btn.textContent="copied ✓";setTimeout(()=>btn.textContent=o,1200);
 }).catch(()=>{});
}
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0,spinT=null;
function startSpin(){
 if(spinT)return;
 spinT=setInterval(()=>{$("spin").textContent=FR[fi=(fi+1)%FR.length];},80);
}
function stopSpin(){if(spinT){clearInterval(spinT);spinT=null;}$("spin").textContent="";}
function reset(){
 $("steps").innerHTML="";$("answerWrap").innerHTML="";
 $("live").style.display="";$("dot").style.display="none";$("dot").className="dot";
 $("gfill").style.width="0";$("jump").style.display="none";follow=true;
 $("status").textContent="working";seen=0;finished=false;tq=[];typing=false;startSpin();
}
function drain(){
 if(!tq.length){typing=false;return;}
 typing=true;const[el,text]=tq.shift();let i=0;
 (function step(){
  el.textContent=text.slice(0,i++);maybeBottom();
  if(i<=text.length)setTimeout(step,text.length>90?3:9);else drain();
 })();
}
function type(el,text){tq.push([el,text]);if(!typing)drain();}
function addStep(e){
 const row=document.createElement("div");row.className="row "+e.kind;
 const t=document.createElement("span");t.className="t";
 t.textContent="["+e.t.toFixed(1)+"s]";
 const sy=document.createElement("span");sy.className="sym";
 sy.textContent=SYM[e.kind]||"·";
 const tx=document.createElement("span");tx.className="txt";
 row.append(t,sy,tx);$("steps").appendChild(row);type(tx,e.text);
}
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function inl(s){
 return s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,"<span class='lnk'>$1</span>")
         .replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>")
         .replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>");
}
function md(src){
 const lines=esc(src).split("\\n"),out=[];let inC=false,code="";
 for(const ln of lines){
  const f=ln.match(/^```(\\w*)\\s*$/);
  if(f){if(!inC){inC=true;code="";}else{inC=false;
   out.push("<pre class='code'>"+code.replace(/\\n$/,"")+"</pre>");}continue;}
  if(inC){code+=ln+"\\n";continue;}
  const h=ln.match(/^(#{1,6})\\s+(.*)$/);
  if(h){out.push("<div class='h h"+h[1].length+"'>"+inl(h[2])+"</div>");continue;}
  const b=ln.match(/^\\s*[-*]\\s+(.*)$/);
  if(b){out.push("<div class='li'><span class='bul'>•</span>"+
   "<span class='lit'>"+inl(b[1])+"</span></div>");continue;}
  const n=ln.match(/^\\s*(\\d+)\\.\\s+(.*)$/);
  if(n){out.push("<div class='li'><span class='bul'>"+n[1]+".</span>"+
   "<span class='lit'>"+inl(n[2])+"</span></div>");continue;}
  if(ln.trim()==="")continue;
  out.push("<div class='p'>"+inl(ln)+"</div>");
 }
 if(inC)out.push("<pre class='code'>"+code+"</pre>");
 return out.join("");
}
function finish(s){
 finished=true;stopSpin();$("live").style.display="none";$("dot").style.display="";
 if(s.status==="error"){$("dot").classList.add("err");
  $("gfill").style.background="var(--red)";}
 $("gfill").style.width="100%";
 const verb=s.status==="error"?"failed":"done";
 $("status").textContent=verb+" in "+(s.elapsed||0).toFixed(1)+"s";
 const w=$("answerWrap");
 if(s.answer||s.image){
  const sep=document.createElement("div");sep.className="sep";
  sep.innerHTML="<span class='seplabel'>OUTPUT</span>";w.appendChild(sep);
 }
 if(s.image){
  const im=document.createElement("img");im.className="shot";
  im.onload=maybeBottom;im.src="/image?"+encodeURIComponent(s.image);w.appendChild(im);
 }
 if(s.answer){
  const a=document.createElement("div");a.className="answer";
  a.innerHTML=md(s.answer);
  const cp=document.createElement("button");cp.className="copy";cp.textContent="copy";
  cp.onclick=()=>copyText(s.answer,cp);a.appendChild(cp);
  w.appendChild(a);
 }
 maybeBottom();
}
async function tick(){
 try{
  const s=await (await fetch("/events",{cache:"no-store"})).json();
  if(s.started!==started){started=s.started;reset();}
  $("subtext").textContent=s.title||"";
 $("wlabel").textContent="— watching "+(s.backend||"agy");
 document.title="Agent Intern — watching "+(s.backend||"agy");
  $("elapsed").textContent=s.elapsed?s.elapsed.toFixed(1)+"s":"";
  if(!finished){const to=s.timeout||0;
   const fr=to>0?Math.min((s.elapsed||0)/to,.98):0.05;
   $("gfill").style.width=Math.round(fr*100)+"%";}
  for(let i=seen;i<s.events.length;i++)addStep(s.events[i]);
  seen=s.events.length;
  if((s.status==="done"||s.status==="error")&&!finished)finish(s);
 }catch(e){}
 setTimeout(tick,finished?1500:400);
}
startSpin();tick();
</script></body></html>"""


def _watch_html() -> str:
    """The watch page with the configured window size substituted for resizeTo."""
    w, h = 600, 820
    try:
        parts = [int(x) for x in _WATCH_WINDOW_SIZE.split(",")]
        if len(parts) == 2:
            w, h = parts
    except ValueError:
        pass
    return _WATCH_HTML.replace("__WIN_W__", str(w)).replace("__WIN_H__", str(h))


def _ensure_watch_server() -> int:
    """Lazily start the localhost watch server (once per process); return its port.

    Binds 127.0.0.1 only — the page and events never leave the local machine.
    """
    global _WATCH_SERVER
    if _WATCH_SERVER is not None:
        return _WATCH_SERVER[1]

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr request logging
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.startswith("/events"):
                global _WATCH_LAST_POLL
                _WATCH_LAST_POLL = time.time()
                self._send(json.dumps(_watch_snapshot()).encode("utf-8"), "application/json")
            elif self.path.startswith("/image"):
                path = _watch_snapshot().get("image") or ""
                fmt = _detect_image_format(path) if path and os.path.isfile(path) else None
                if fmt:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "GIF": "image/gif",
                        "WEBP": "image/webp",
                    }[fmt]
                    with open(path, "rb") as fh:
                        self._send(fh.read(), mime)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self._send(_watch_html().encode("utf-8"), "text/html; charset=utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _WATCH_SERVER = (httpd, port, thread)
    log.debug("watch server on http://127.0.0.1:%d", port)
    return port


# Small dedicated viewer window. Override "WIDTH,HEIGHT" via AGY_WATCH_WINDOW_SIZE.
_WATCH_WINDOW_SIZE = os.environ.get("AGY_WATCH_WINDOW_SIZE", "560,760")


def _chromium_app_browsers() -> list[str]:
    """Paths to Chromium-based browsers that support `--app` windowed mode, so the
    viewer can open as a small chromeless window instead of a tab. Best-effort."""
    found: list[str] = []
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
    ):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


def _viewer_is_live() -> bool:
    """True if a watch window polled /events within _VIEWER_ALIVE_S — i.e. a viewer
    is already open, so a new run should reuse it instead of stacking another window."""
    return (time.time() - _WATCH_LAST_POLL) < _VIEWER_ALIVE_S


def _open_watch_window(url: str) -> None:
    """Open the watch page in a small, dedicated window. Prefers a Chromium browser
    in `--app` mode (a sized, chromeless window — not a tab); falls back to a normal
    new browser window/tab. Best-effort — never raises.

    Reuses an already-open viewer (detected via recent /events polls) so repeated
    watch calls don't pile up browser windows; the open page picks up the new run
    on its own. Set AGY_WATCH_ALWAYS_NEW=1 to force a fresh window every time."""
    if _viewer_is_live() and not _env_truthy("AGY_WATCH_ALWAYS_NEW"):
        log.debug("watch viewer already open; reusing it instead of opening a new window")
        return
    for exe in _chromium_app_browsers():
        try:
            subprocess.Popen(
                [exe, f"--app={url}", f"--window-size={_WATCH_WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_spawn_kwargs(),
            )
            return
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1)  # request a new window (clients may still tab)
    except Exception:  # noqa: BLE001 - viewer is best-effort
        pass


def _run_agy_watched(prompt: str, workspace: str, continue_conv: bool, timeout_s: int) -> str:
    """Like _run_agy, but open a live browser "watch" view. EXPERIMENTAL.

    agy runs headless (console-detached, no leak); alongside it, the bridge serves
    a small localhost page and opens your browser to it, live-streaming agy's steps
    (narration + the real commands it runs) read from the transcript. The return
    value is identical to antigravity_ask. The viewer is best-effort and cross-platform
    (any browser); if it can't open, the run still completes normally.
    """
    args, pinned_conv = _build_agy_args(prompt, workspace, continue_conv, timeout_s)

    with _AGY_LOCK:
        start = time.time()
        feed = _WatchFeed(pinned_conv, start)
        title = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        _watch_reset(title, start, timeout_s)
        try:
            port = _ensure_watch_server()
            _open_watch_window(f"http://127.0.0.1:{port}/")
        except Exception:  # noqa: BLE001 - the viewer is best-effort, never fatal
            pass

        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_spawn_kwargs(),  # agy stays headless; the browser is the viewer
        )
        hard_deadline = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard_deadline:
                proc.kill()
                _watch_finish("error", "(timed out)", time.time() - start)
                raise RuntimeError(f"agy timed out after {timeout_s + 30}s (watched)")
            feed.pump()
            time.sleep(_PROGRESS_POLL_INTERVAL_S)
        feed.pump()  # drain entries flushed right before exit

        _, stderr = proc.communicate()
        if proc.returncode != 0:
            _watch_finish("error", f"(agy exited {proc.returncode})", time.time() - start)
            raise RuntimeError(f"agy exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}")

        deadline = time.time() + _RESPONSE_POLL_DEADLINE_S
        while True:
            try:
                answer = _resolve_and_read(pinned_conv or feed.conv, workspace, start)
                break
            except RuntimeError:
                if time.time() >= deadline:
                    _watch_finish("error", "(no answer found)", time.time() - start)
                    raise
                time.sleep(_RESPONSE_POLL_INTERVAL_S)
        _watch_finish("done", answer, time.time() - start)
        return answer


def _run_agy_image_watched(
    wrapped_prompt: str, target: str, workspace: str, timeout_s: int, display_prompt: str
) -> str:
    """Generate an image with a live watch window that also displays the result.

    EXPERIMENTAL. Runs agy headless, streams its steps to the Agent Intern window,
    finalises the generated image (extension corrected to the real bytes), shows
    it in the window, and returns the same string as antigravity_image. `display_prompt`
    is the user's original prompt, shown as the window title (not the wrapped
    save-path instructions that actually go to agy).
    """
    args, _ = _build_agy_args(wrapped_prompt, workspace, False, timeout_s)

    with _AGY_LOCK:
        start = time.time()
        feed = _WatchFeed(None, start)
        title = display_prompt.strip().splitlines()[0] if display_prompt.strip() else "image"
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        _watch_reset(title, start, timeout_s)
        try:
            port = _ensure_watch_server()
            _open_watch_window(f"http://127.0.0.1:{port}/")
        except Exception:  # noqa: BLE001 - viewer is best-effort
            pass

        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_spawn_kwargs(),
        )
        hard_deadline = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard_deadline:
                proc.kill()
                _watch_finish("error", "(timed out)", time.time() - start)
                raise RuntimeError(f"agy timed out after {timeout_s + 30}s (image/watch)")
            feed.pump()
            time.sleep(_PROGRESS_POLL_INTERVAL_S)
        feed.pump()
        proc.communicate()

        # The transcript read may fail even though the image was written; don't lose
        # a produced image to a transcript hiccup (mirrors antigravity_image).
        agy_text = None
        agy_error = None
        try:
            agy_text = _resolve_and_read(feed.conv, workspace, start)
        except RuntimeError as e:
            agy_error = e

        try:
            final_path, fmt, size = _finalize_image(target, agy_text, start)
        except RuntimeError as fin_err:
            _watch_finish("error", f"no image produced: {fin_err}", time.time() - start)
            if agy_error is not None:
                raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
            raise

        _watch_set_image(final_path)
        caption = f"Saved to {final_path}\nformat={fmt} · {size} bytes"
        _watch_finish("done", caption, time.time() - start)
        return f"{final_path}\nformat={fmt}  size={size} bytes"


@mcp.tool(
    annotations={
        "title": "Ask Antigravity (new conversation)",
        "readOnlyHint": False,  # agy runs unsandboxed: may write files / run commands
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Antigravity service
    }
)
async def antigravity_ask(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
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
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps (narration + the real commands it runs) as it works.
               agy still runs headless; the same final text is returned. Best-
               effort and cross-platform — if the browser can't open, the run
               completes normally. Default false.
    """
    ws = _normalize_workspace(workspace)
    if watch:
        return await asyncio.to_thread(_run_agy_watched, prompt, ws, False, timeout_s)
    return await _run_with_progress(_run_agy, (prompt, ws, False, timeout_s), ctx, timeout_s)


@mcp.tool(
    annotations={
        "title": "Continue Antigravity conversation",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Antigravity conversation rooted at this workspace.

    Resumes the exact conversation id recorded for `workspace` (via agy's
    --conversation flag), not agy's global "most recent", so it stays correct
    even if agy was used elsewhere in between.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps as it works (same return value, best-effort). Default false.
    """
    ws = _normalize_workspace(workspace)
    if watch:
        return await asyncio.to_thread(_run_agy_watched, prompt, ws, True, timeout_s)
    return await _run_with_progress(_run_agy, (prompt, ws, True, timeout_s), ctx, timeout_s)


@mcp.tool(
    annotations={
        "title": "Generate an image with Antigravity",
        "readOnlyHint": False,  # writes the generated image file to disk
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_image(
    prompt: str,
    output_path: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout_s: int = 240,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Generate an image with Antigravity (Gemini image model via agy CLI).

    Drives agy to produce a raster image on your existing AI Pro quota, saves it,
    and returns the absolute file path plus its real format and byte size. The
    host can then read the path to view the image.

    agy picks the image format itself (JPEG for photo-like images, PNG for flat
    graphics), so the returned path's extension is corrected to match the actual
    bytes (a requested out.png may come back as out.jpg). Runs a normal,
    unsandboxed agy session — same privileges/caveats as the other tools (see the
    module SECURITY note).

    Args:
        prompt: Description of the image to generate.
        output_path: Where to save. Absolute, or relative to `workspace`. If
                     omitted, a timestamped name under `workspace` is used.
        workspace: Working directory for the conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 240
                   (image generation is slower than text).
        watch: If true, open the live "watch" window that streams agy's steps and
               shows the finished image inline (same return value, best-effort).
               Default false.
    """
    ws = _normalize_workspace(workspace)
    target = _resolve_output_path(output_path, ws)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    wrapped = _wrap_image_prompt(prompt, target)
    if watch:
        return await asyncio.to_thread(
            _run_agy_image_watched, wrapped, target, ws, timeout_s, prompt
        )

    start = time.time()
    agy_text: Optional[str] = None
    agy_error: Optional[Exception] = None
    try:
        agy_text = await _run_with_progress(
            _run_agy, (wrapped, ws, False, timeout_s), ctx, timeout_s
        )
    except RuntimeError as e:
        # The transcript read may fail even though agy wrote the image. Don't
        # lose a successfully generated file to a transcript hiccup — try to
        # locate it anyway, and only surface this error if nothing was produced.
        agy_error = e

    try:
        final_path, fmt, size = _finalize_image(target, agy_text, start)
    except RuntimeError as fin_err:
        if agy_error is not None:
            raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
        raise
    return f"{final_path}\nformat={fmt}  size={size} bytes"


def _broadcast_workspaces(workspaces: Optional[list], n: int):
    """Map the MCP `workspaces` arg to swarm's None|str|list contract.

    None -> server cwd for all; a 1-item list -> that dir for all N; an N-item
    list -> one workspace per prompt. (MCP can't pass a bare str for a list field,
    so a 1-item list is the "same dir for everyone" shorthand.)
    """
    if not workspaces:
        return None
    if len(workspaces) == 1:
        return workspaces[0]
    return workspaces


@mcp.tool(
    annotations={
        "title": "Agent swarm (mixed Antigravity + Codex, parallel)",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def agent_swarm(
    tasks: list[dict],
    max_concurrency: int = 4,
    timeout_s: int = 180,
    watch: bool = False,
) -> str:
    """Run SEVERAL tasks IN PARALLEL across BOTH backends in a single swarm.

    Each task is its own worker and names the backend to run on, so one swarm can
    mix Antigravity (Gemini) and Codex workers — they run truly concurrently
    (capped at `max_concurrency`) and every answer comes back in one labelled
    block. A worker that fails is reported in place; the others still return.

    SECURITY: this launches N unsandboxed agents at once — N times the
    prompt-injection surface of a single call (see the module SECURITY note). Only
    use it with trusted prompts on trusted content.

    Args:
        tasks: One object per parallel worker:
               - backend: "antigravity" (alias "agy"/"gemini") or "codex" (required)
               - prompt:  the question or instruction (required)
               - workspace: working dir for that worker (default: server cwd)
               - sandbox: Codex only — "read-only" (default), "workspace-write",
                          or "danger-full-access". Ignored for Antigravity.
               - model:   Codex only — model override (`-m`). Ignored for Antigravity.
        max_concurrency: Max workers running at once (default 4). Higher = faster
                         but more quota/rate-limit pressure and more agents at once.
        timeout_s: Per-worker timeout in seconds. Default 180.
        watch: If true, open the live "Agent Swarm" dashboard window (one row per
               worker, with a backend badge; click a row for its full step log).
    """
    import swarm

    results = swarm.swarm_agents(tasks, max_concurrency, timeout_s, watch)
    return swarm.format_agent_results(results)


@mcp.tool(
    annotations={
        "title": "Generate several images in parallel",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def antigravity_image_swarm(
    prompts: list[str],
    output_paths: Optional[list[str]] = None,
    workspaces: Optional[list[str]] = None,
    max_concurrency: int = 4,
    timeout_s: int = 240,
    watch: bool = False,
) -> str:
    """Generate several images IN PARALLEL with Antigravity (one worker per prompt).

    Like antigravity_image, but runs N image generations concurrently in isolated
    workers (capped at `max_concurrency`). Returns one block listing each image's
    final path/format/size (or its error). Extensions are corrected to the real
    bytes, exactly like antigravity_image. Same unsandboxed privileges/caveats as
    antigravity_swarm.

    Args:
        prompts: One image description per parallel worker.
        output_paths: Where to save each image (aligned to prompts). Omit to write
                      timestamped files in the first workspace (or server cwd).
        workspaces: Working directory per worker (same shorthand as antigravity_swarm).
        max_concurrency: Max workers running at once (default 4).
        timeout_s: Per-worker timeout in seconds. Default 240 (images are slower).
        watch: If true, open the live dashboard; each finished image shows in its
               pane, and clicking a row opens that agent's window beside the dashboard.
    """
    import swarm

    n = len(prompts)
    if output_paths is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = workspaces[0] if workspaces else os.getcwd()
        output_paths = [os.path.join(base, f"agy-swarm-image-{stamp}-{i}.png") for i in range(n)]
    results = swarm.swarm_image(
        prompts,
        output_paths,
        workspaces=_broadcast_workspaces(workspaces, n),
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        watch=watch,
    )
    return swarm.format_image_results(results)


@mcp.tool(
    annotations={
        "title": "agy bridge diagnostics",
        "readOnlyHint": True,  # only reads local state + runs `agy --version`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def antigravity_status() -> str:
    """Report diagnostics for the agy bridge setup (spends no AI Pro quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK), then checks
    whether agy is on PATH (and its version/compat), whether agy's state
    directories exist, whether the newest conversation transcript is readable,
    and whether the SQLite conversation store is present. Use this to debug empty
    or failed responses — or to see if the bridge itself is out of date — before
    spending quota.
    """
    rows = _collect_status()
    width = max(len(label) for label, _, _ in rows)
    lines = ["agy bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


@mcp.tool(
    annotations={
        "title": "Ask Codex (new session)",
        "readOnlyHint": False,  # codex may edit files when sandbox != read-only
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external OpenAI/Codex service
    }
)
async def codex_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = codex_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask OpenAI Codex (`codex exec`) a question or task in a NEW session.

    Uses your existing Codex login (ChatGPT or API key — see `codex login status`).
    Returns the agent's final message as text, read from codex's
    --output-last-message file (no stdout scraping). Codex is a capable coding
    agent, so this suits heavier reasoning and real code work, not just cheap
    tool-calling. Point `workspace` at a real project dir for context-aware answers.

    Args:
        prompt: Question or instruction for Codex.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Filesystem policy — "read-only" (default: reads and answers but
                 writes nothing), "workspace-write" (may edit files under the
                 workspace), or "danger-full-access" (no sandbox — avoid). `codex
                 exec` has no interactive approval gate, so this is the real safety
                 boundary; opt into write access deliberately.
        model: Optional model override (`-m`); omit to use codex's configured default.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               codex's steps (reasoning, the commands it runs, file changes) from
               its `--json` event stream. codex still runs headless; the same final
               text is returned. Best-effort — if the browser can't open, the run
               completes normally. Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    codex_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@mcp.tool(
    annotations={
        "title": "Continue Codex session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def codex_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Codex session rooted at this workspace (`codex exec resume`).

    Resumes the exact session id captured from the last codex_ask in this
    workspace, falling back to the newest on-disk session whose recorded cwd
    matches (so it still works after a server restart). The resumed session keeps
    its original sandbox and model — those are chosen when you start it with
    codex_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open the live "watch" view streaming codex's steps as it
               works (same viewer as codex_ask). Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched,
            prompt,
            ws,
            codex_bridge.DEFAULT_SANDBOX,
            None,
            True,
            timeout_s,
        )
    return await _run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, codex_bridge.DEFAULT_SANDBOX, None, True, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@mcp.tool(
    annotations={
        "title": "Codex bridge diagnostics",
        "readOnlyHint": True,  # only runs `codex --version` / `codex login status`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def codex_status() -> str:
    """Report diagnostics for the Codex bridge setup (spends no quota).

    Checks whether codex is on PATH (and its version), whether you're logged in
    (`codex login status` — no model call, no quota), where codex stores its
    sessions, and how many workspace sessions are pinned this run. Use this to
    debug "codex not found" or auth errors before spending quota.
    """
    rows = codex_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["codex bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _codex_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one codex --json event to (kind, text) watch lines (kind is
    'narration' | 'command' | 'result'), mirroring _entry_to_watch_lines for agy.
    Returns [] for events with nothing worth showing in the viewer.
    """
    etype = ev.get("type")
    if etype == "item.completed":
        item = ev.get("item") or {}
        itype = item.get("type")
        if itype in ("agent_message", "reasoning"):
            txt = (item.get("text") or item.get("summary") or "").strip()
            return [("narration", txt.splitlines()[0][:200])] if txt else []
        if itype == "command_execution":
            cmd = (item.get("command") or "").strip()
            return [("command", cmd[:200])] if cmd else []
        if itype == "file_change":
            changes = item.get("changes") or item.get("files") or []
            n = len(changes) if isinstance(changes, list) else 0
            return [("result", f"file change ({n} file(s))" if n else "file change")]
        if itype == "mcp_tool_call":
            return [("command", f"mcp: {item.get('tool') or item.get('name') or ''}"[:200])]
        if itype == "web_search":
            return [("command", f"search: {item.get('query') or ''}"[:200])]
        return []
    if etype == "turn.started":
        return [("narration", "thinking…")]
    if etype == "error":
        return [("result", f"error: {ev.get('message') or ''}"[:200])]
    return []


def _run_codex_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like codex_bridge.run_codex, but stream codex's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy watch tools;
    the return value is identical to codex_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    _watch_reset(title, start, timeout_s, backend="codex")
    try:
        port = _ensure_watch_server()
        _open_watch_window(f"http://127.0.0.1:{port}/")
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _codex_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append([{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = codex_bridge.run_codex_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish("error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish("done", answer, time.time() - start)
    return answer


def main() -> None:
    """Console entry point (also `python server.py`).

    Exposed as the `agent-intern` script so the bridge can be launched with
    `uvx agent-intern` (isolated, always-latest) instead of a hardcoded path.
    """
    _configure_logging()
    _startup_checks()
    mcp.run()


if __name__ == "__main__":
    main()
