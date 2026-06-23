"""Codex CLI bridge: run OpenAI's `codex exec` headless and return its answer.

Companion to the agy bridge in server.py. Where `agy -p` is broken — it never
writes its answer to stdout, so server.py must scrape transcript files — `codex
exec` is well-behaved: `-o/--output-last-message` writes the final agent message
to a file we pick, so we read the answer straight from there. No stdout/JSONL
scraping on the happy path.

CONTINUE / RESUME. codex persists every session to

    $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl

with the session id both in the filename and in the first line's
`session_meta.payload.id`, and the originating workspace in
`session_meta.payload.cwd`. After a fresh `codex exec` we capture that id (the
rollout file that appeared during the run) and pin it to the workspace; a later
`codex_continue` resumes the exact session with `codex exec resume <id>`. If the
in-memory pin is gone (server restarted) we fall back to the newest rollout whose
recorded cwd matches the workspace — the codex analogue of agy's
last_conversations.json lookup. This is verified against codex-cli 0.141.0.

SECURITY. `codex exec` runs the model as an autonomous agent with no interactive
approval gate. Unlike agy's no-op `--sandbox`, codex's `-s/--sandbox` is a REAL
boundary: the default `read-only` lets the agent read and answer but change
nothing on disk; callers must opt into `workspace-write` (edit files under the
workspace) or `danger-full-access` (no sandbox — avoid). Even so, only run it
with trusted prompts on trusted content.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# The codex executable. Defaults to "codex" (resolved via PATH); set CODEX_BIN to
# an explicit path when codex isn't reliably on PATH — e.g. on Windows, where the
# native installer drops it at
#   %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
# and a fresh terminal/reboot can leave it off a non-login shell's PATH. Mirrors
# AGY_BIN in server.py. Read once at import; the launching process's env wins.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")

# codex's state home (config + sessions). codex honors CODEX_HOME; default ~/.codex.
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"

# codex exec -s/--sandbox accepts exactly these. Default read-only = safe: the
# agent can read and answer but cannot modify the filesystem. Callers opt into
# write access explicitly per call.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# A session id is a UUID; it appears in the rollout filename
# (rollout-<ts>-<uuid>.jsonl) and in the first line's session_meta.payload.id.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Strips the ANSI color codes codex wraps some output in (e.g. `login status`
# prints "\x1b[31;1mLogged in using ChatGPT\x1b[0m").
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Poll window for the rollout file to appear after codex exits. codex has already
# returned by the time we look, so this usually resolves at once; the poll just
# absorbs filesystem-flush lag (mirrors server.py's response poll).
_SESSION_POLL_DEADLINE_S = 5.0
_SESSION_POLL_INTERVAL_S = 0.1

# workspace -> session id, captured after each fresh ask so codex_continue resumes
# the exact session rooted at that workspace. Guarded by a lock because MCP tools
# may run on different threads. Lives only for the process; the on-disk rollout
# cwd lookup (_resume_target_for) is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """Keep codex from popping a console window on Windows; new session elsewhere.

    Cosmetic only: codex writes its answer to the -o file regardless of the
    controlling terminal (it has no agy-style stdout bug). Windows uses
    CREATE_NO_WINDOW so a child console doesn't flash; POSIX starts a new session.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values."""
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


def _iter_rollouts() -> list[Path]:
    """All rollout JSONL files under the sessions dir (date-bucketed), or []."""
    if not SESSIONS_DIR.exists():
        return []
    return list(SESSIONS_DIR.glob("**/rollout-*.jsonl"))


def _rollout_names() -> set[str]:
    """Snapshot of rollout filenames that exist right now."""
    return {p.name for p in _iter_rollouts()}


def _session_id_from_name(path: Path) -> Optional[str]:
    """Session id embedded in a rollout filename, or None."""
    m = _UUID_RE.search(path.name)
    return m.group(0) if m else None


def _session_meta(path: Path) -> Optional[dict]:
    """Parsed `session_meta` payload from a rollout's first line, or None."""
    try:
        with path.open("r", encoding="utf-8") as f:
            rec = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if isinstance(rec, dict) and rec.get("type") == "session_meta":
        payload = rec.get("payload")
        return payload if isinstance(payload, dict) else None
    return None


def _session_id_of(path: Path) -> Optional[str]:
    """Session id of a rollout: filename first, then the first-line meta."""
    sid = _session_id_from_name(path)
    if sid:
        return sid
    meta = _session_meta(path)
    sid = meta.get("id") if meta else None
    return sid if isinstance(sid, str) else None


def _capture_new_session(before: set[str]) -> Optional[str]:
    """Session id of the rollout that appeared during this run (not in `before`).

    Polls briefly: codex has already exited, but the rollout file may lag a moment
    behind on disk. Returns None if no new rollout shows up within the window.
    """
    deadline = time.time() + _SESSION_POLL_DEADLINE_S
    while True:
        fresh = [p for p in _iter_rollouts() if p.name not in before]
        if fresh:
            newest = max(fresh, key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
            sid = _session_id_of(newest)
            if sid:
                return sid
        if time.time() >= deadline:
            return None
        time.sleep(_SESSION_POLL_INTERVAL_S)


def _resume_target_for(workspace: str) -> Optional[str]:
    """Newest rollout's session id whose recorded cwd matches `workspace`, or None.

    Restart-proof fallback for codex_continue when the in-memory pin is gone:
    scans rollouts newest-first by mtime and returns the first whose
    session_meta.cwd equals the workspace. Reads only first lines, and stops at
    the first match, so it stays cheap in the common case.
    """
    target = os.path.normcase(os.path.abspath(workspace))
    dated: list[tuple[float, Path]] = []
    for p in _iter_rollouts():
        try:
            dated.append((p.stat().st_mtime, p))
        except OSError:
            continue
    for _, p in sorted(dated, key=lambda t: t[0], reverse=True):
        meta = _session_meta(p)
        cwd = meta.get("cwd") if meta else None
        if isinstance(cwd, str) and os.path.normcase(os.path.abspath(cwd)) == target:
            return _session_id_of(p)
    return None


# ----------------------------------------------------------------- running codex
def _resolve_resume_session(workspace: str, continue_conv: bool) -> Optional[str]:
    """The session id to resume for `workspace`, or None for a fresh run.

    Prefers the in-memory pin (set after the last codex_ask in this workspace),
    then the newest on-disk rollout whose recorded cwd matches. Raises if continue
    is requested but no prior session exists.
    """
    if not continue_conv:
        return None
    sid = get_pinned(workspace) or _resume_target_for(workspace)
    if not sid:
        raise RuntimeError(
            f"No prior codex session for workspace {workspace}. "
            "Run codex_ask first (or check $CODEX_HOME/sessions)."
        )
    return sid


def build_args(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    resume_session: Optional[str],
    output_file: str,
    json_stream: bool = False,
) -> list[str]:
    """argv for `codex exec` (fresh) or `codex exec resume <id>` (continue).

    A fresh run sets the sandbox and working root explicitly. Resume inherits the
    session's recorded cwd/sandbox (codex's `resume` subcommand has no `-s`/`-C`),
    so it passes only the id, optional model override, the output file, and the
    prompt — the subprocess cwd is still set to the workspace by the caller.
    `json_stream` adds `--json` so codex emits its events as JSONL on stdout (for
    watch mode); the final answer is still read from `output_file` either way.
    """
    args = [CODEX_BIN, "exec"]
    if resume_session:
        args += ["resume", resume_session]
    if model:
        args += ["-m", model]
    if not resume_session:
        args += ["-s", sandbox, "-C", workspace, "--skip-git-repo-check"]
    if json_stream:
        args += ["--json"]
    args += ["-o", output_file, prompt]
    return args


def _read_output(path: str) -> str:
    """Final agent message codex wrote to its -o file, stripped; "" on any error."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def run_codex(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `codex exec` (fresh or resume) and return the final agent message.

    Reads the answer from codex's `-o/--output-last-message` file (no stdout
    scraping). On a fresh run, captures the new session id and pins it to
    `workspace` so a later codex_continue can resume the exact session — pass
    `pin=False` for parallel swarm workers, where racing rollout snapshots could
    misattribute sessions (swarm runs are one-shot, so they need no pin).

    Signature is positional-friendly so it can be handed to server.py's
    _run_with_progress(run_fn, args, ...) unchanged.
    """
    validate_sandbox(sandbox)
    resume_session = _resolve_resume_session(workspace, continue_conv)

    # NamedTemporaryFile(delete=False): we only want a unique path; codex opens and
    # writes it itself, so we close our handle immediately and clean up in finally.
    fd = tempfile.NamedTemporaryFile(suffix=".txt", prefix="codex_out_", delete=False)
    out_path = fd.name
    fd.close()

    before = _rollout_names() if (not continue_conv and pin) else set()
    try:
        args = build_args(prompt, workspace, sandbox, model, resume_session, out_path)
        proc = subprocess.run(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
            **_spawn_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exited {proc.returncode}\n"
                f"stderr: {(proc.stderr or '')[-1000:]}\n"
                f"stdout: {(proc.stdout or '')[-500:]}"
            )

        answer = _read_output(out_path)
        if not answer:
            # -o produced nothing (rare): fall back to whatever codex put on stdout.
            answer = (proc.stdout or "").strip()
        if not answer:
            raise RuntimeError(
                "codex produced no final message (empty -o file and stdout). "
                f"stderr: {(proc.stderr or '')[-300:]}"
            )

        if not continue_conv and pin:
            sid = _capture_new_session(before)
            if sid:
                _pin(workspace, sid)
        return answer
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def run_codex_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Run `codex exec --json` and stream events live, returning the final answer.

    Like run_codex, but launches codex with --json so it emits one JSON event per
    line on stdout, and calls `on_event(event_dict)` for each parsed event as it
    arrives (this is how watch mode renders steps live). The answer is still read
    from the -o file, not scraped from the stream. A watchdog timer kills codex
    past the deadline, since the blocking stdout read can't otherwise be cut off.
    """
    validate_sandbox(sandbox)
    resume_session = _resolve_resume_session(workspace, continue_conv)

    fd = tempfile.NamedTemporaryFile(suffix=".txt", prefix="codex_out_", delete=False)
    out_path = fd.name
    fd.close()

    before = set() if continue_conv else _rollout_names()
    args = build_args(prompt, workspace, sandbox, model, resume_session, out_path, json_stream=True)
    try:
        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_spawn_kwargs(),
        )
        # The stdout read below blocks, so a hung run can't be caught by an inline
        # clock; a watchdog timer kills the process, which closes the pipe and ends
        # the loop. timed_out distinguishes that kill from a clean exit.
        timed_out = {"v": False}

        def _kill() -> None:
            timed_out["v"] = True
            proc.kill()

        killer = threading.Timer(timeout_s + 30, _kill)
        killer.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or on_event is None:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    on_event(ev)
                except Exception:  # noqa: BLE001 — a viewer hiccup must not kill the run
                    pass
            proc.wait()
        finally:
            killer.cancel()

        stderr = proc.stderr.read() if proc.stderr else ""
        if timed_out["v"]:
            raise RuntimeError(f"codex timed out after {timeout_s + 30}s (watched)")
        if proc.returncode not in (0, None):
            raise RuntimeError(f"codex exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}")

        answer = _read_output(out_path)
        if not answer:
            raise RuntimeError(
                f"codex produced no final message (empty -o file). stderr: {(stderr or '')[-300:]}"
            )

        if not continue_conv and pin:
            sid = _capture_new_session(before)
            if sid:
                _pin(workspace, sid)
        return answer
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ----------------------------------------------------------------- diagnostics
def codex_version() -> Optional[str]:
    """`codex --version` text (single line), or None if codex can't be run."""
    try:
        proc = subprocess.run(
            [CODEX_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text or None


def codex_login_status() -> tuple[bool, str]:
    """(logged_in, detail). Spends no quota — `codex login status` only checks creds.

    Per codex docs, the command exits 0 when credentials are present. The detail is
    codex's own first line (e.g. "Logged in using ChatGPT"), ANSI-stripped.
    """
    try:
        proc = subprocess.run(
            [CODEX_BIN, "login", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return (False, f"could not run `codex login status`: {e}")
    text = _ANSI_RE.sub("", ((proc.stdout or "") + (proc.stderr or ""))).strip()
    logged_in = proc.returncode == 0
    detail = text.splitlines()[0] if text else ("logged in" if logged_in else "not logged in")
    return (logged_in, detail)


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors server.py's _collect_status shape so server.py can render codex rows
    with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = codex_version()
    if ver is None:
        rows.append(("codex CLI", False, f"not found on PATH (set CODEX_BIN; tried {CODEX_BIN!r})"))
    else:
        rows.append(("codex CLI", True, ver))

    logged_in, detail = codex_login_status()
    rows.append(("codex auth", logged_in, detail))

    rows.append(("sessions dir", SESSIONS_DIR.exists(), str(SESSIONS_DIR)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))

    return rows


# ----------------------------------------------------------------- parallel swarm
def _broadcast_workspaces(workspaces, n: int) -> list[str]:
    """Per-worker workspace list of length n. None -> all server cwd; a single
    entry -> broadcast to all; n entries -> as given. Mirrors the agy swarm
    shorthand. Raises if the count is neither 1 nor n."""
    if workspaces is None:
        return [normalize_workspace(None)] * n
    if isinstance(workspaces, str):
        return [normalize_workspace(workspaces)] * n
    if len(workspaces) == 1:
        return [normalize_workspace(workspaces[0])] * n
    if len(workspaces) != n:
        raise ValueError(f"workspaces has {len(workspaces)} entries; expected 1 or {n}")
    return [normalize_workspace(w) for w in workspaces]


def swarm_codex(
    prompts: list[str],
    workspaces=None,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    max_concurrency: int = 4,
    timeout_s: int = 180,
) -> list[dict]:
    """Run several codex prompts in parallel as independent one-shot workers.

    Each worker launches a fresh `codex exec` with its own -o file, so there's no
    shared mutable state to race on — unlike agy (which rewrites
    last_conversations.json per call and thus needs an isolated HOME per worker),
    codex needs no isolation here. Workers run with pin=False since swarm runs are
    one-shot (no codex_continue afterwards). Returns a list aligned to `prompts`:
    {index, workspace, ok, answer} or {index, workspace, ok: False, error}.
    """
    n = len(prompts)
    if n == 0:
        return []
    validate_sandbox(sandbox)
    wss = _broadcast_workspaces(workspaces, n)
    results: list = [None] * n

    def work(i: int) -> dict:
        try:
            ans = run_codex(prompts[i], wss[i], sandbox, model, False, timeout_s, pin=False)
            return {"index": i, "workspace": wss[i], "ok": True, "answer": ans}
        except Exception as e:  # noqa: BLE001 — one worker failing must not sink the rest
            return {"index": i, "workspace": wss[i], "ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=max(1, min(max_concurrency, n))) as ex:
        for r in ex.map(work, range(n)):
            results[r["index"]] = r
    return results


def format_swarm_results(results: list[dict]) -> str:
    """Render swarm_codex results as one human-readable block, one entry per worker."""
    blocks = []
    for r in results:
        repo = os.path.basename((r["workspace"] or "").rstrip("/\\")) or r["workspace"]
        head = f"[{r['index'] + 1}] {repo}"
        if r.get("ok"):
            blocks.append(f"{head}\n{r['answer']}")
        else:
            blocks.append(f"{head}\nERROR: {r.get('error', 'unknown')}")
    return "\n\n---\n\n".join(blocks)
