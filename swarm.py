"""Parallel agy swarm: run N Antigravity (Gemini) workers truly concurrently.

Each worker runs in its OWN isolated HOME/USERPROFILE temp dir, so agy's
per-process state (brain/, cache/, last_conversations.json) never collides. This
is why the single-agent path in server.py must serialize via _AGY_LOCK, but the
swarm does NOT need the lock: isolated state means no race. Auth still works
because agy reads it from the OS credential store, not from ~/.gemini (verified on
agy 1.0.9 / Windows). cwd is set to each worker's real workspace, so file access
there is unchanged; HOME redirection isolates state only.

SECURITY: a swarm runs N unsandboxed agy agents at once — N times the
prompt-injection "lethal trifecta" surface described in server.py's module
docstring. Only use it with trusted prompts on trusted content.

Pure helpers and config are reused from server.py via lazy imports (inside
functions) so this module can be imported by server.py without a circular import.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

DEFAULT_MAX_CONCURRENCY = 4
_REAL_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


# ----------------------------------------------------------------------------- isolation
def _make_isolated_home() -> Path:
    """Fresh temp HOME seeded with settings.json (to keep the same model)."""
    home = Path(tempfile.mkdtemp(prefix="agy_swarm_"))
    dst = home / ".gemini" / "antigravity-cli"
    dst.mkdir(parents=True, exist_ok=True)
    if _REAL_SETTINGS.exists():
        try:
            (dst / "settings.json").write_text(_REAL_SETTINGS.read_text("utf-8"), "utf-8")
        except OSError:
            pass
    return home


def _env_for_home(home: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if os.name == "nt":
        drive, _, rest = str(home).partition(":")
        env["HOMEDRIVE"] = drive + ":"
        env["HOMEPATH"] = rest or "\\"
    return env


def _isolated_brain(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "brain"


def _only_conv(home: Path) -> Optional[str]:
    """The conversation id in an isolated HOME — exactly one per worker run.

    If more than one somehow exists, pick the newest by mtime (defensive)."""
    brain = _isolated_brain(home)
    if not brain.exists():
        return None
    dirs = [c for c in brain.iterdir() if c.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda c: c.stat().st_mtime).name


def _iso_transcript_entries(home: Path, conv_id: str) -> list[dict]:
    """All parsed JSONL entries for a worker's isolated conversation, or []."""
    transcript = _isolated_brain(home) / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.exists():
        return []
    out: list[dict] = []
    for line in transcript.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_isolated_response(home: Path, conv_id: str) -> str:
    """Final planner answer, read from the isolated HOME's brain dir (not the real one)."""
    chunks = [
        e["content"]
        for e in _iso_transcript_entries(home, conv_id)
        if e.get("source") == "MODEL"
        and e.get("status") == "DONE"
        and e.get("type") == "PLANNER_RESPONSE"
        and e.get("content")
    ]
    if not chunks:
        raise RuntimeError("no completed MODEL response in transcript")
    return chunks[-1]


class _Feed:
    """Pumps a worker's isolated transcript into its watch channel as step events."""

    def __init__(self, home: Path, index: int, start: float):
        self._home, self._index, self._start = home, index, start
        self._conv: Optional[str] = None
        self._cursor = 0

    def pump(self) -> None:
        import server
        import swarm_watch

        if self._conv is None:
            self._conv = _only_conv(self._home)
            if self._conv is None:
                return
            self._cursor = 0
        entries = _iso_transcript_entries(self._home, self._conv)
        new = []
        for entry in entries[self._cursor :]:
            for kind, text in server._entry_to_watch_lines(entry):
                new.append({"kind": kind, "text": text, "t": round(time.time() - self._start, 1)})
        self._cursor = max(self._cursor, len(entries))
        if new:
            swarm_watch.worker_append(self._index, new)


# ----------------------------------------------------------------------------- results
@dataclass
class WorkerResult:
    index: int
    ok: bool
    answer: Optional[str] = None
    error: Optional[str] = None
    elapsed: float = 0.0
    workspace: str = ""
    image_path: Optional[str] = None
    image_format: Optional[str] = None
    image_size: Optional[int] = None


def _normalize_workspaces(n: int, workspaces: Union[None, str, list]) -> list[str]:
    """Per-worker workspace list aligned to n prompts.

    None -> cwd for all; str -> same dir for all; list -> per-worker (len must == n)."""
    if workspaces is None:
        return [os.getcwd()] * n
    if isinstance(workspaces, str):
        return [os.path.abspath(workspaces)] * n
    if len(workspaces) != n:
        raise ValueError(f"workspaces length {len(workspaces)} != prompts {n}")
    return [os.path.abspath(w) for w in workspaces]


def _labels(prompts: list[str]) -> list[str]:
    out = []
    for p in prompts:
        line = p.strip().splitlines()[0] if p.strip() else "(empty)"
        out.append(line[:120] + ("…" if len(line) > 120 else ""))
    return out


def _basename_any(path: str) -> str:
    """Last path component, handling both '/' and '\\' regardless of host OS.

    os.path.basename uses only the host's separator, so on Linux a Windows-style
    workspace ("C:\\a\\b\\repo") wouldn't be shortened (and a POSIX path on Windows
    likewise). Splitting on both separators keeps worker labels clean everywhere.
    """
    cleaned = path.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or path


def _repos(workspaces: list[str]) -> list[str]:
    return [_basename_any(w) for w in workspaces]


# ----------------------------------------------------------------------------- text swarm
def _run_text_worker(index, prompt, workspace, timeout_s) -> WorkerResult:
    import server

    home = _make_isolated_home()
    t0 = time.time()
    try:
        os.makedirs(workspace, exist_ok=True)
        args = [server.AGY_BIN, "--print-timeout", f"{timeout_s}s", "-p", prompt]
        proc = subprocess.run(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            env=_env_for_home(home),
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
            **server._spawn_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr[-300:]}")
        deadline = time.time() + 5.0
        while True:
            conv = _only_conv(home)
            try:
                if conv:
                    ans = _read_isolated_response(home, conv)
                    return WorkerResult(
                        index,
                        True,
                        answer=ans,
                        elapsed=round(time.time() - t0, 1),
                        workspace=workspace,
                    )
            except RuntimeError:
                pass
            if time.time() >= deadline:
                raise RuntimeError("no readable transcript after agy exit")
            time.sleep(0.1)
    except Exception as e:  # error isolation: never propagate, return the failure
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - t0, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_text_worker_watched(index, prompt, workspace, timeout_s) -> WorkerResult:
    import server
    import swarm_watch

    home = _make_isolated_home()
    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)
    feed = _Feed(home, index, start)
    try:
        os.makedirs(workspace, exist_ok=True)
        args = [server.AGY_BIN, "--print-timeout", f"{timeout_s}s", "-p", prompt]
        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env_for_home(home),
            **server._spawn_kwargs(),
        )
        hard = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard:
                proc.kill()
                raise RuntimeError("timeout")
            feed.pump()
            swarm_watch.worker_update(index, elapsed=round(time.time() - start, 1))
            time.sleep(0.4)
        feed.pump()
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"agy exited {proc.returncode}: {(stderr or '')[-300:]}")
        deadline = time.time() + 5.0
        while True:
            conv = _only_conv(home)
            try:
                if conv:
                    ans = _read_isolated_response(home, conv)
                    swarm_watch.worker_finish(index, "done", ans, time.time() - start)
                    return WorkerResult(
                        index,
                        True,
                        answer=ans,
                        elapsed=round(time.time() - start, 1),
                        workspace=workspace,
                    )
            except RuntimeError:
                pass
            if time.time() >= deadline:
                raise RuntimeError("no readable transcript after agy exit")
            time.sleep(0.1)
    except Exception as e:
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def swarm_ask(
    prompts: list[str],
    workspaces: Union[None, str, list] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout_s: int = 180,
    watch: bool = False,
) -> list[WorkerResult]:
    """Run N text prompts as parallel agy workers; results aligned to prompts."""
    ws = _normalize_workspaces(len(prompts), workspaces)
    runner = _run_text_worker_watched if watch else _run_text_worker
    if watch:
        import swarm_watch

        swarm_watch.init(_labels(prompts), _repos(ws), time.time(), prompts)
        swarm_watch.open_window(len(prompts))
    results: list[Optional[WorkerResult]] = [None] * len(prompts)
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as ex:
        futs = [ex.submit(runner, i, p, ws[i], timeout_s) for i, p in enumerate(prompts)]
        for fut in futs:
            r = fut.result()
            results[r.index] = r
    return [r for r in results if r is not None]


# ----------------------------------------------------------------------------- image swarm
def _finalize_image_isolated(home: Path, target: str, agy_text: Optional[str], start: float):
    """Locate the generated image (target / agy-reported path / isolated scratch),
    correct its extension to the real bytes, and return (path, fmt, size)."""
    import server

    scratch = home / ".gemini" / "antigravity-cli" / "scratch"
    candidates = [target]
    if agy_text and agy_text.strip():
        candidates.append(agy_text.strip().splitlines()[0].strip().strip('"'))
    if scratch.exists():
        imgs = [
            c
            for c in scratch.iterdir()
            if c.is_file() and c.stat().st_mtime > start - 2 and server._detect_image_format(str(c))
        ]
        if imgs:
            candidates.append(str(max(imgs, key=lambda c: c.stat().st_mtime)))

    src = next((c for c in candidates if c and os.path.isfile(c)), None)
    if src is None:
        raise RuntimeError(f"no image file produced (looked at {target} and scratch)")
    fmt = server._detect_image_format(src)
    if fmt is None:
        raise RuntimeError(f"{src} is not a recognized image")
    final_path = server._with_ext(target, server._canonical_ext(fmt))
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(final_path):
        if os.path.exists(final_path):
            os.remove(final_path)
        shutil.move(src, final_path)
    return final_path, fmt, os.path.getsize(final_path)


def _run_image_worker(index, prompt, target, workspace, timeout_s) -> WorkerResult:
    import server

    home = _make_isolated_home()
    start = time.time()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
        wrapped = server._wrap_image_prompt(prompt, target)
        args = [server.AGY_BIN, "--print-timeout", f"{timeout_s}s", "-p", wrapped]
        proc = subprocess.run(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            env=_env_for_home(home),
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
            **server._spawn_kwargs(),
        )
        agy_text = None
        if proc.returncode == 0:
            conv = _only_conv(home)
            if conv:
                try:
                    agy_text = _read_isolated_response(home, conv)
                except RuntimeError:
                    pass
        final_path, fmt, size = _finalize_image_isolated(home, target, agy_text, start)
        return WorkerResult(
            index,
            True,
            answer=final_path,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            image_path=final_path,
            image_format=fmt,
            image_size=size,
        )
    except Exception as e:
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_image_worker_watched(index, prompt, target, workspace, timeout_s) -> WorkerResult:
    import server
    import swarm_watch

    home = _make_isolated_home()
    start = time.time()
    swarm_watch.worker_update(index, status="working", started=start)
    feed = _Feed(home, index, start)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
        wrapped = server._wrap_image_prompt(prompt, target)
        args = [server.AGY_BIN, "--print-timeout", f"{timeout_s}s", "-p", wrapped]
        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env_for_home(home),
            **server._spawn_kwargs(),
        )
        hard = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard:
                proc.kill()
                raise RuntimeError("timeout")
            feed.pump()
            swarm_watch.worker_update(index, elapsed=round(time.time() - start, 1))
            time.sleep(0.4)
        feed.pump()
        proc.communicate()
        agy_text = None
        conv = _only_conv(home)
        if conv:
            try:
                agy_text = _read_isolated_response(home, conv)
            except RuntimeError:
                pass
        final_path, fmt, size = _finalize_image_isolated(home, target, agy_text, start)
        caption = f"Saved to {final_path}\nformat={fmt} · {size} bytes"
        swarm_watch.worker_finish(index, "done", caption, time.time() - start, image=final_path)
        return WorkerResult(
            index,
            True,
            answer=final_path,
            elapsed=round(time.time() - start, 1),
            workspace=workspace,
            image_path=final_path,
            image_format=fmt,
            image_size=size,
        )
    except Exception as e:
        swarm_watch.worker_finish(index, "error", str(e), time.time() - start)
        return WorkerResult(
            index, False, error=str(e), elapsed=round(time.time() - start, 1), workspace=workspace
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def swarm_image(
    prompts: list[str],
    output_paths: list[str],
    workspaces: Union[None, str, list] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout_s: int = 240,
    watch: bool = False,
) -> list[WorkerResult]:
    """Generate N images in parallel (one isolated agy worker each)."""
    if len(output_paths) != len(prompts):
        raise ValueError("output_paths must align with prompts")
    ws = _normalize_workspaces(len(prompts), workspaces)
    targets = [os.path.abspath(p) for p in output_paths]
    runner = _run_image_worker_watched if watch else _run_image_worker
    if watch:
        import swarm_watch

        swarm_watch.init(_labels(prompts), _repos(ws), time.time(), prompts)
        swarm_watch.open_window(len(prompts))
    results: list[Optional[WorkerResult]] = [None] * len(prompts)
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as ex:
        futs = [
            ex.submit(runner, i, prompts[i], targets[i], ws[i], timeout_s)
            for i in range(len(prompts))
        ]
        for fut in futs:
            r = fut.result()
            results[r.index] = r
    return [r for r in results if r is not None]


# ----------------------------------------------------------------------------- formatting
def format_text_results(results: list[WorkerResult]) -> str:
    """Render text-swarm results as one readable block for the MCP host."""
    parts = []
    for r in sorted(results, key=lambda r: r.index):
        head = f"[worker {r.index}] {'OK' if r.ok else 'ERROR'} ({r.elapsed}s)"
        if r.workspace:
            head += f" @ {_basename_any(r.workspace)}"
        body = r.answer if r.ok else f"(failed) {r.error}"
        parts.append(f"{head}\n{body}")
    ok = sum(1 for r in results if r.ok)
    return f"swarm: {ok}/{len(results)} succeeded\n\n" + "\n\n".join(parts)


def format_image_results(results: list[WorkerResult]) -> str:
    """Render image-swarm results as one readable block for the MCP host."""
    parts = []
    for r in sorted(results, key=lambda r: r.index):
        if r.ok:
            parts.append(
                f"[image {r.index}] OK ({r.elapsed}s) {r.image_format} "
                f"{r.image_size} bytes -> {r.image_path}"
            )
        else:
            parts.append(f"[image {r.index}] ERROR ({r.elapsed}s) {r.error}")
    ok = sum(1 for r in results if r.ok)
    return f"image swarm: {ok}/{len(results)} succeeded\n\n" + "\n".join(parts)
