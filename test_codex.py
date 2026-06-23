"""Offline unit tests for the pure logic in codex_bridge.py.

Like test_server.py / test_swarm.py these use temp fixtures and monkeypatching and
never invoke codex, so they cost no OpenAI quota. The live round-trip lives in the
smoke test.

    pytest test_codex.py
"""

import json
import os
from pathlib import Path

import pytest

import codex_bridge
import server

SAMPLE_SID = "019ef10a-fc0e-7180-b22f-5bd19fe8fc5b"


# --------------------------------------------------------------------------
# validate_sandbox / defaults
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", codex_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert codex_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError):
        codex_bridge.validate_sandbox("yolo")


def test_default_sandbox_is_read_only():
    # The safe default agreed for codex_ask: reads/answers, writes nothing.
    assert codex_bridge.DEFAULT_SANDBOX == "read-only"
    assert "read-only" in codex_bridge.SANDBOX_MODES


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert codex_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert codex_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args — the heart of the bridge: fresh vs resume argv shape
# --------------------------------------------------------------------------


def test_build_args_fresh_basic():
    args = codex_bridge.build_args("hello", "C:\\ws", "read-only", None, None, "out.txt")
    assert args[0] == codex_bridge.CODEX_BIN
    assert args[1] == "exec"
    assert "resume" not in args
    assert args[args.index("-s") + 1] == "read-only"
    assert args[args.index("-C") + 1] == "C:\\ws"
    assert "--skip-git-repo-check" in args
    assert args[args.index("-o") + 1] == "out.txt"
    assert args[-1] == "hello"  # prompt is positional and last


def test_build_args_fresh_with_model():
    args = codex_bridge.build_args("p", "ws", "workspace-write", "gpt-5.5", None, "o.txt")
    assert args[args.index("-m") + 1] == "gpt-5.5"
    assert args[args.index("-s") + 1] == "workspace-write"


def test_build_args_resume_omits_sandbox_and_cd():
    # resume inherits the session's recorded cwd/sandbox; codex's resume subcommand
    # has no -s/-C, so we must NOT pass them.
    args = codex_bridge.build_args("again", "ws", "read-only", None, SAMPLE_SID, "o.txt")
    assert args[1] == "exec"
    assert args[2] == "resume"
    assert args[3] == SAMPLE_SID
    assert "-s" not in args
    assert "-C" not in args
    assert "--skip-git-repo-check" not in args
    assert args[args.index("-o") + 1] == "o.txt"
    assert args[-1] == "again"


def test_build_args_resume_with_model():
    args = codex_bridge.build_args("p", "ws", "read-only", "o3", SAMPLE_SID, "o.txt")
    assert "resume" in args
    assert args[args.index("-m") + 1] == "o3"


# --------------------------------------------------------------------------
# session id extraction + rollout parsing
# --------------------------------------------------------------------------


def _write_rollout(sessions_dir, sid, cwd, mtime=None, name=None):
    """Create a minimal rollout file under sessions_dir/2026/06/22/ with a
    session_meta first line, mirroring codex's real layout."""
    day = sessions_dir / "2026" / "06" / "22"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or f"rollout-2026-06-22T23-34-49-{sid}.jsonl")
    meta = {
        "timestamp": "2026-06-22T20:37:31.604Z",
        "type": "session_meta",
        "payload": {"id": sid, "cwd": cwd, "cli_version": "0.141.0"},
    }
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_session_id_from_name():
    p = Path(f"rollout-2026-06-22T23-34-49-{SAMPLE_SID}.jsonl")
    assert codex_bridge._session_id_from_name(p) == SAMPLE_SID


def test_session_id_from_name_no_uuid():
    assert codex_bridge._session_id_from_name(Path("rollout-nope.jsonl")) is None


def test_session_meta_reads_cwd_and_id(tmp_path):
    p = _write_rollout(tmp_path, SAMPLE_SID, "C:\\proj")
    meta = codex_bridge._session_meta(p)
    assert meta["id"] == SAMPLE_SID
    assert meta["cwd"] == "C:\\proj"


def test_session_meta_returns_none_for_non_meta(tmp_path):
    p = tmp_path / "junk.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert codex_bridge._session_meta(p) is None


def test_session_id_of_prefers_filename_over_bad_body(tmp_path):
    day = tmp_path / "2026" / "06" / "22"
    day.mkdir(parents=True)
    p = day / f"rollout-x-{SAMPLE_SID}.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert codex_bridge._session_id_of(p) == SAMPLE_SID


# --------------------------------------------------------------------------
# _resume_target_for / _capture_new_session  (SESSIONS_DIR monkeypatched)
# --------------------------------------------------------------------------


def test_resume_target_matches_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, "11111111-1111-7111-8111-111111111111", "C:\\other", mtime=100)
    want = "22222222-2222-7222-8222-222222222222"
    _write_rollout(sessions, want, "C:\\proj", mtime=200)
    assert codex_bridge._resume_target_for("C:\\proj") == want


def test_resume_target_none_when_no_cwd_match(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\elsewhere")
    assert codex_bridge._resume_target_for("C:\\proj") is None


def test_resume_target_picks_newest_for_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    old = "33333333-3333-7333-8333-333333333333"
    new = "44444444-4444-7444-8444-444444444444"
    _write_rollout(sessions, old, "C:\\proj", mtime=100, name=f"rollout-a-{old}.jsonl")
    _write_rollout(sessions, new, "C:\\proj", mtime=300, name=f"rollout-b-{new}.jsonl")
    assert codex_bridge._resume_target_for("C:\\proj") == new


def test_capture_new_session_finds_added_rollout(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\proj", name=f"rollout-old-{SAMPLE_SID}.jsonl")
    before = codex_bridge._rollout_names()
    new = "55555555-5555-7555-8555-555555555555"
    _write_rollout(sessions, new, "C:\\proj", name=f"rollout-new-{new}.jsonl")
    assert codex_bridge._capture_new_session(before) == new


def test_capture_new_session_none_when_nothing_new(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_SESSION_POLL_DEADLINE_S", 0.2)
    _write_rollout(sessions, SAMPLE_SID, "C:\\proj")
    before = codex_bridge._rollout_names()
    assert codex_bridge._capture_new_session(before) is None


# --------------------------------------------------------------------------
# pinning + output reading + diagnostics
# --------------------------------------------------------------------------


def test_pin_and_get(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    assert codex_bridge.get_pinned("C:\\ws") is None
    codex_bridge._pin("C:\\ws", SAMPLE_SID)
    assert codex_bridge.get_pinned("C:\\ws") == SAMPLE_SID


def test_read_output_reads_and_strips(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("  the answer  \n", encoding="utf-8")
    assert codex_bridge._read_output(str(f)) == "the answer"


def test_read_output_missing_returns_empty(tmp_path):
    assert codex_bridge._read_output(str(tmp_path / "nope.txt")) == ""


def test_ansi_strip_regex():
    raw = "\x1b[31;1mLogged in using ChatGPT\x1b[0m"
    assert codex_bridge._ANSI_RE.sub("", raw) == "Logged in using ChatGPT"


def test_status_rows_codex_missing(monkeypatch):
    monkeypatch.setattr(codex_bridge, "codex_version", lambda: None)
    monkeypatch.setattr(codex_bridge, "codex_login_status", lambda: (False, "not logged in"))
    rows = {label: (ok, detail) for label, ok, detail in codex_bridge.status_rows()}
    assert rows["codex CLI"][0] is False
    assert rows["codex auth"][0] is False


def test_status_rows_codex_ok(monkeypatch):
    monkeypatch.setattr(codex_bridge, "codex_version", lambda: "codex-cli 0.141.0")
    monkeypatch.setattr(
        codex_bridge, "codex_login_status", lambda: (True, "Logged in using ChatGPT")
    )
    rows = {label: (ok, detail) for label, ok, detail in codex_bridge.status_rows()}
    assert rows["codex CLI"] == (True, "codex-cli 0.141.0")
    assert rows["codex auth"][0] is True


# --------------------------------------------------------------------------
# watch mode: --json flag, resume resolution, event -> watch-line mapping
# --------------------------------------------------------------------------


def test_build_args_json_stream_adds_flag_before_output():
    args = codex_bridge.build_args("p", "ws", "read-only", None, None, "o.txt", json_stream=True)
    assert "--json" in args
    assert args.index("--json") < args.index("-o")  # --json before -o/prompt


def test_build_args_no_json_by_default():
    args = codex_bridge.build_args("p", "ws", "read-only", None, None, "o.txt")
    assert "--json" not in args


def test_resolve_resume_session_fresh_is_none():
    assert codex_bridge._resolve_resume_session("C:\\ws", False) is None


def test_resolve_resume_session_uses_pin(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    codex_bridge._pin("C:\\ws", SAMPLE_SID)
    assert codex_bridge._resolve_resume_session("C:\\ws", True) == SAMPLE_SID


def test_resolve_resume_session_raises_without_prior(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "none")
    with pytest.raises(RuntimeError):
        codex_bridge._resolve_resume_session("C:\\ws", True)


def test_watch_lines_agent_message_first_line():
    ev = {"type": "item.completed", "item": {"type": "agent_message", "text": "hello\nworld"}}
    assert server._codex_event_to_watch_lines(ev) == [("narration", "hello")]


def test_watch_lines_command_execution():
    ev = {"type": "item.completed", "item": {"type": "command_execution", "command": "ls -la"}}
    assert server._codex_event_to_watch_lines(ev) == [("command", "ls -la")]


def test_watch_lines_file_change():
    ev = {"type": "item.completed", "item": {"type": "file_change", "changes": [1, 2]}}
    kind, text = server._codex_event_to_watch_lines(ev)[0]
    assert kind == "result" and "2" in text


def test_watch_lines_ignores_noise():
    assert server._codex_event_to_watch_lines({"type": "turn.completed", "usage": {}}) == []
    assert server._codex_event_to_watch_lines({"type": "thread.started"}) == []


# --------------------------------------------------------------------------
# swarm: workspace broadcast, result formatting, parallel dispatch
# --------------------------------------------------------------------------


def test_broadcast_workspaces_none_is_cwd(monkeypatch):
    # A static absolute path — NOT os.path.abspath(), which would re-enter the
    # monkeypatched getcwd on POSIX (the path isn't absolute there) and recurse.
    fake = os.path.join(os.sep, "fake", "cwd")
    monkeypatch.setattr(os, "getcwd", lambda: fake)
    assert codex_bridge._broadcast_workspaces(None, 3) == [fake] * 3


def test_broadcast_workspaces_single_broadcasts():
    assert codex_bridge._broadcast_workspaces(["C:\\a"], 2) == [os.path.abspath("C:\\a")] * 2


def test_broadcast_workspaces_per_worker():
    out = codex_bridge._broadcast_workspaces(["C:\\a", "C:\\b"], 2)
    assert out == [os.path.abspath("C:\\a"), os.path.abspath("C:\\b")]


def test_broadcast_workspaces_mismatch_raises():
    with pytest.raises(ValueError):
        codex_bridge._broadcast_workspaces(["C:\\a", "C:\\b"], 3)


def test_swarm_codex_empty():
    assert codex_bridge.swarm_codex([]) == []


def test_format_swarm_results_ok_and_error():
    results = [
        {"index": 0, "workspace": "C:\\proj", "ok": True, "answer": "hi"},
        {"index": 1, "workspace": "C:\\other", "ok": False, "error": "boom"},
    ]
    out = codex_bridge.format_swarm_results(results)
    assert "[1] proj" in out and "hi" in out
    assert "[2] other" in out and "ERROR: boom" in out


def test_swarm_codex_runs_all_with_pin_false(monkeypatch):
    calls = []

    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True):
        calls.append(pin)
        return f"ans:{prompt}"

    monkeypatch.setattr(codex_bridge, "run_codex", fake_run)
    results = codex_bridge.swarm_codex(["a", "b", "c"], workspaces=["C:\\w"], max_concurrency=2)
    answers = {r["index"]: r["answer"] for r in results}
    assert answers == {0: "ans:a", 1: "ans:b", 2: "ans:c"}
    assert calls == [False, False, False]  # swarm must never pin


def test_swarm_codex_isolates_worker_failure(monkeypatch):
    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True):
        if prompt == "boom":
            raise RuntimeError("worker exploded")
        return f"ans:{prompt}"

    monkeypatch.setattr(codex_bridge, "run_codex", fake_run)
    results = codex_bridge.swarm_codex(["ok", "boom"], workspaces=["C:\\w"])
    by_index = {r["index"]: r for r in results}
    assert by_index[0]["ok"] is True and by_index[0]["answer"] == "ans:ok"
    assert by_index[1]["ok"] is False and "exploded" in by_index[1]["error"]
