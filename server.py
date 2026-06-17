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
1.0.9.

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

Compat (re-verified on agy 1.0.9): state-file paths, last_conversations.json,
and the transcript schema are unchanged, and a normally-completing -p run still
writes the JSONL transcript this bridge reads. agy now ALSO dual-writes every
conversation to a SQLite store at ~/.gemini/antigravity-cli/conversations/<id>.db;
the 1.0.4
changelog says SQLite "will be the CLI's conversation format", so once JSONL
stops being written the bridge breaks and _read_response will need a SQLite
reader (it already raises a clear, SQLite-aware error when the transcript is
missing). The 1.0.5 -p metadata fix also stopped agy from writing metadata to
the cwd, so last_conversations.json now updates reliably under cache/.

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

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import anyio
from fastmcp import Context, FastMCP

mcp = FastMCP("agy")

# Logs go to stderr (stdout is the MCP protocol channel). Quiet by default;
# set AGY_BRIDGE_DEBUG=1 for per-call diagnostics. See _configure_logging.
log = logging.getLogger("agy_bridge")

# The agy executable to invoke. Defaults to "agy" (resolved via PATH); set the
# AGY_BIN env var to an explicit path when agy isn't reliably on PATH — e.g. on
# Windows where a new terminal/reboot can drop it:
#   AGY_BIN=%LOCALAPPDATA%\agy\bin\agy.exe
# Read once at import; the launching process's environment wins.
AGY_BIN = os.environ.get("AGY_BIN", "agy")

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
VERIFIED_AGY_VERSION = (1, 0, 9)

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


def _spawn_kwargs() -> dict:
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
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
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


def _entry_to_progress(entry: dict) -> Optional[str]:
    """One-line human progress string for a transcript entry, or None to skip.

    Only the model's own steps are surfaced: PLANNER_RESPONSE (the narration agy
    writes as it works — the exact text that used to leak to the host terminal)
    and RUN_COMMAND (a tool step). USER_INPUT / CONVERSATION_HISTORY / system
    rows are skipped. The final PLANNER_RESPONSE is also the answer, so callers
    get the last narration line as a 'finishing' tick — harmless.
    """
    if entry.get("source") != "MODEL":
        return None
    etype = entry.get("type")
    content = entry.get("content")
    if etype == "PLANNER_RESPONSE" and content:
        return content.strip().splitlines()[0][:160]
    if etype == "RUN_COMMAND":
        return "running a command…"
    return None


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
            f"agy_image: no image file found. Looked at target {target!r} and "
            f"scratch dir {SCRATCH_DIR}."
        )

    fmt = _detect_image_format(src)
    if fmt is None:
        raise RuntimeError(
            f"agy_image: {src!r} is not a recognized image. agy may have refused "
            "the request or returned text instead of an image."
        )

    final_path = _with_ext(target, _canonical_ext(fmt))
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(final_path):
        if os.path.exists(final_path):
            os.remove(final_path)
        shutil.move(src, final_path)
    return final_path, fmt, os.path.getsize(final_path)


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


class _ProgressStream:
    """Emits transcript steps for ONE conversation, each new step exactly once.

    For a continued conversation the id is pinned up front and the cursor starts
    past the existing history (so prior turns aren't replayed — the same problem
    agy 1.0.9 fixed for stdout). For a new conversation the id is unknown at
    launch, so the stream locks onto the first brain dir that appears after launch
    and didn't pre-exist, and never switches away from it.
    """

    def __init__(
        self,
        pinned_conv: Optional[str],
        start: float,
        on_progress: Callable[[int, str], None],
    ) -> None:
        self._start = start
        self._on_progress = on_progress
        self._pre_existing = set() if pinned_conv else _existing_conv_names()
        self._conv = pinned_conv
        self._cursor = len(_transcript_entries(pinned_conv)) if pinned_conv else 0

    def poll(self) -> None:
        """Read the locked conversation and emit any steps past the cursor."""
        if self._conv is None:
            self._conv = _newest_new_conv(self._start, self._pre_existing)
            if self._conv is None:
                return
            self._cursor = 0
        entries = _transcript_entries(self._conv)
        for idx, entry in enumerate(entries[self._cursor :], start=self._cursor):
            msg = _entry_to_progress(entry)
            if msg:
                self._on_progress(idx + 1, msg)
        self._cursor = max(self._cursor, len(entries))


def _run_agy_streamed(
    prompt: str,
    workspace: str,
    continue_conv: bool,
    timeout_s: int,
    on_progress: Callable[[int, str], None],
) -> str:
    """Like _run_agy, but spawn agy non-blocking and stream transcript progress.

    EXPERIMENTAL. Polls the transcript while agy works and calls
    `on_progress(step, message)` for each new model step. Progress is coarse —
    agy flushes the transcript in chunks (see _PROGRESS_POLL_INTERVAL_S) — so
    expect a handful of ticks per run, with an initial blind window, not
    token-level streaming. The final answer is still read from the transcript
    exactly as _run_agy does.
    """
    args, pinned_conv = _build_agy_args(prompt, workspace, continue_conv, timeout_s)

    with _AGY_LOCK:
        start = time.time()
        stream = _ProgressStream(pinned_conv, start, on_progress)
        log.debug("streaming agy: pinned=%s", pinned_conv)
        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_spawn_kwargs(),  # keep agy's TTY writes out of the host terminal
        )
        hard_deadline = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard_deadline:
                proc.kill()
                raise RuntimeError(f"agy timed out after {timeout_s + 30}s (streaming)")
            stream.poll()
            time.sleep(_PROGRESS_POLL_INTERVAL_S)

        # Drain any entries flushed between the last poll and exit.
        stream.poll()
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"agy exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}")

        deadline = time.time() + _RESPONSE_POLL_DEADLINE_S
        while True:
            try:
                return _resolve_and_read(pinned_conv, workspace, start)
            except RuntimeError:
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
def agy_ask_stream(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    ctx: Context = None,
) -> str:
    """EXPERIMENTAL: like agy_ask, but stream agy's progress while it works.

    Starts a NEW conversation and returns the same final text as agy_ask, but as
    agy works it reports each intermediate step — its planner narration (the same
    text that, pre-fix, leaked into the host terminal) and its tool runs — as MCP
    progress notifications, read live from agy's transcript.

    Progress is intentionally coarse: agy flushes its transcript in chunks, so
    expect a few ticks per run with an initial blind window, not token-level
    streaming. Whether you SEE the ticks depends on your MCP client surfacing
    progress/log notifications; the final return value is identical to agy_ask.

    Args:
        prompt: Question or instruction for Antigravity.
        workspace: Working directory for the conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
    """
    ws = _normalize_workspace(workspace)

    def emit(step: int, message: str) -> None:
        if ctx is None:
            return
        # Bridge from this worker thread into the event loop to fire the async
        # notifications. Best-effort: a progress hiccup must never fail the call.
        try:
            anyio.from_thread.run(ctx.report_progress, float(step), None, message)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass
        try:
            anyio.from_thread.run(ctx.info, f"agy step {step}: {message}")
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    return _run_agy_streamed(prompt, ws, continue_conv=False, timeout_s=timeout_s, on_progress=emit)


@mcp.tool()
def agy_image(
    prompt: str,
    output_path: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout_s: int = 240,
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
    """
    ws = _normalize_workspace(workspace)
    target = _resolve_output_path(output_path, ws)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    wrapped = _wrap_image_prompt(prompt, target)

    start = time.time()
    agy_text: Optional[str] = None
    agy_error: Optional[Exception] = None
    try:
        agy_text = _run_agy(wrapped, ws, continue_conv=False, timeout_s=timeout_s)
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
