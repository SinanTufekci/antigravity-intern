"""Offline unit tests for the pure logic in server.py.

These use temp fixtures and never invoke agy, so they cost no AI Pro quota and
can run anywhere (including CI). For the live end-to-end check, see
test_smoke.py instead.

    pytest test_server.py
"""

import json
import os
import subprocess
import time

import pytest

import server

# --------------------------------------------------------------------------
# _normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_returns_cwd():
    assert server._normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_relative_is_absolutised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert server._normalize_workspace("sub/dir") == os.path.abspath("sub/dir")


# --------------------------------------------------------------------------
# _read_last_conv_id
# --------------------------------------------------------------------------


@pytest.fixture
def last_conv_file(tmp_path, monkeypatch):
    f = tmp_path / "last_conversations.json"
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", f)
    return f


def test_read_last_conv_id_exact_match(last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\proj": "conv-1"}), encoding="utf-8")
    assert server._read_last_conv_id("C:\\proj") == "conv-1"


def test_read_last_conv_id_is_case_insensitive(last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\Proj": "conv-2"}), encoding="utf-8")
    assert server._read_last_conv_id("c:\\proj") == "conv-2"


def test_read_last_conv_id_missing_file_returns_none(last_conv_file):
    assert server._read_last_conv_id("anything") is None


def test_read_last_conv_id_malformed_json_returns_none(last_conv_file):
    last_conv_file.write_text("{not valid json", encoding="utf-8")
    assert server._read_last_conv_id("x") is None


def test_read_last_conv_id_absent_key_returns_none(last_conv_file):
    last_conv_file.write_text(json.dumps({"other": "c"}), encoding="utf-8")
    assert server._read_last_conv_id("missing") is None


# --------------------------------------------------------------------------
# _find_newest_conv_after
# --------------------------------------------------------------------------


@pytest.fixture
def brain_dir(tmp_path, monkeypatch):
    d = tmp_path / "brain"
    d.mkdir()
    monkeypatch.setattr(server, "BRAIN_DIR", d)
    return d


def test_find_newest_conv_after_missing_brain_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "does-not-exist")
    assert server._find_newest_conv_after(time.time()) is None


def test_find_newest_conv_after_picks_newest_dir(brain_dir):
    start = time.time()
    old = brain_dir / "old-conv"
    old.mkdir()
    new = brain_dir / "new-conv"
    new.mkdir()
    os.utime(old, (start - 100, start - 100))
    os.utime(new, (start + 5, start + 5))
    assert server._find_newest_conv_after(start) == "new-conv"


def test_find_newest_conv_after_ignores_plain_files(brain_dir):
    start = time.time()
    f = brain_dir / "a-file"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (start + 5, start + 5))
    assert server._find_newest_conv_after(start) is None


def test_find_newest_conv_after_skips_dirs_older_than_start(brain_dir):
    start = time.time()
    stale = brain_dir / "stale"
    stale.mkdir()
    os.utime(stale, (start - 100, start - 100))
    assert server._find_newest_conv_after(start) is None


# --------------------------------------------------------------------------
# _read_response
# --------------------------------------------------------------------------


def _entry(type_, content=None, status="DONE", source="MODEL"):
    e = {"source": source, "status": status, "type": type_}
    if content is not None:
        e["content"] = content
    return json.dumps(e)


def _write_transcript(brain_dir, conv_id, lines):
    logs = brain_dir / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_read_response_returns_last_planner_response_with_content(brain_dir):
    _write_transcript(
        brain_dir,
        "c1",
        [
            _entry("RUN_COMMAND", "step"),
            _entry("PLANNER_RESPONSE", "first"),
            _entry("PLANNER_RESPONSE", "final"),
        ],
    )
    assert server._read_response("c1") == "final"


def test_read_response_ignores_contentless_and_malformed_lines(brain_dir):
    _write_transcript(
        brain_dir,
        "c2",
        [
            "{ broken json",
            "",
            _entry("PLANNER_RESPONSE"),  # no content
            _entry("PLANNER_RESPONSE", "answer"),
        ],
    )
    assert server._read_response("c2") == "answer"


def test_read_response_no_completed_response_raises(brain_dir):
    _write_transcript(
        brain_dir,
        "c3",
        [
            _entry("PLANNER_RESPONSE", "x", status="RUNNING"),
        ],
    )
    with pytest.raises(RuntimeError, match="No completed MODEL response"):
        server._read_response("c3")


def test_read_response_missing_transcript_no_db_mentions_sqlite(brain_dir):
    (brain_dir / "c4").mkdir()
    with pytest.raises(RuntimeError, match="SQLite"):
        server._read_response("c4")


def test_read_response_missing_transcript_with_db_points_at_db(brain_dir):
    conv_dir = brain_dir / "c5"
    conv_dir.mkdir()
    (conv_dir / "conversation.db").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"conversation\.db"):
        server._read_response("c5")


# --------------------------------------------------------------------------
# _parse_agy_version
# --------------------------------------------------------------------------


def test_parse_agy_version_bare():
    assert server._parse_agy_version("1.0.4") == (1, 0, 4)


def test_parse_agy_version_trailing_newline():
    assert server._parse_agy_version("1.0.4\n") == (1, 0, 4)


def test_parse_agy_version_with_prefix_and_build():
    assert server._parse_agy_version("agy version 1.2.0 (build abc)") == (1, 2, 0)


def test_parse_agy_version_garbage_returns_none():
    assert server._parse_agy_version("no version here") is None


def test_parse_agy_version_empty_returns_none():
    assert server._parse_agy_version("") is None


# --------------------------------------------------------------------------
# _compat_warning
# --------------------------------------------------------------------------


def test_compat_warning_none_for_verified_version():
    assert server._compat_warning(server.VERIFIED_AGY_VERSION) is None


def test_compat_warning_none_for_older_version():
    assert server._compat_warning((1, 0, 3)) is None


def test_compat_warning_warns_for_newer_version():
    msg = server._compat_warning((1, 1, 0))
    assert msg is not None
    assert "1.1.0" in msg  # the detected version
    assert "1.0.9" in msg  # the verified baseline it's compared to


def test_compat_warning_none_when_version_unknown():
    assert server._compat_warning(None) is None


# --------------------------------------------------------------------------
# _debug_enabled
# --------------------------------------------------------------------------


def test_debug_enabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_DEBUG", raising=False)
    assert server._debug_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_debug_enabled_true_for_truthy(monkeypatch, value):
    monkeypatch.setenv("AGY_BRIDGE_DEBUG", value)
    assert server._debug_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_debug_enabled_false_for_falsy(monkeypatch, value):
    monkeypatch.setenv("AGY_BRIDGE_DEBUG", value)
    assert server._debug_enabled() is False


# --------------------------------------------------------------------------
# _spawn_kwargs  (console-detach so agy's TTY writes don't leak to the host)
# --------------------------------------------------------------------------


def test_spawn_kwargs_detaches_per_platform(monkeypatch):
    monkeypatch.setattr(server.os, "name", "nt")
    nt = server._spawn_kwargs()
    # CREATE_NO_WINDOW == 0x08000000; reference the literal so the assertion is
    # portable (the constant only exists on the Windows subprocess module).
    assert nt == {"creationflags": 0x08000000}

    monkeypatch.setattr(server.os, "name", "posix")
    posix = server._spawn_kwargs()
    assert posix == {"start_new_session": True}


def test_spawn_kwargs_is_subprocess_run_compatible(monkeypatch):
    # The returned mapping must be valid **kwargs for subprocess on this host
    # (no platform-foreign keys leak through).
    kwargs = server._spawn_kwargs()
    assert isinstance(kwargs, dict)
    if server.os.name == "nt":
        assert "creationflags" in kwargs and "start_new_session" not in kwargs
    else:
        assert "start_new_session" in kwargs and "creationflags" not in kwargs


# --------------------------------------------------------------------------
# AGY_BIN  (configurable agy executable; AGY_BIN env var overrides "agy")
# --------------------------------------------------------------------------


def test_build_agy_args_uses_default_agy_bin(monkeypatch):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert args[0] == "agy"


def test_build_agy_args_honors_custom_agy_bin(monkeypatch):
    custom = "C:\\Users\\x\\AppData\\Local\\agy\\bin\\agy.exe"
    monkeypatch.setattr(server, "AGY_BIN", custom)
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert args[0] == custom
    # only argv[0] changes; the rest of the command line is unaffected
    assert "--print-timeout" in args
    assert args[-2:] == ["-p", "hi"]


# --------------------------------------------------------------------------
# _startup_checks  (composition of the tested helpers; agy version injected)
# --------------------------------------------------------------------------


def test_startup_checks_warns_on_newer_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "2.0.0")
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "newer" in caplog.text


def test_startup_checks_silent_on_verified_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.9")
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


def test_startup_checks_silent_when_agy_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


# --------------------------------------------------------------------------
# _resolve_and_read
# --------------------------------------------------------------------------


def test_resolve_and_read_uses_pinned_conv(brain_dir):
    _write_transcript(brain_dir, "pinned", [_entry("PLANNER_RESPONSE", "P")])
    assert server._resolve_and_read("pinned", "C:\\ws", time.time()) == "P"


def test_resolve_and_read_uses_last_conv(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "lc"}), encoding="utf-8")
    _write_transcript(brain_dir, "lc", [_entry("PLANNER_RESPONSE", "L")])
    assert server._resolve_and_read(None, "C:\\ws", time.time()) == "L"


def test_resolve_and_read_falls_back_to_newest(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    start = time.time()
    _write_transcript(brain_dir, "newest", [_entry("PLANNER_RESPONSE", "N")])
    os.utime(brain_dir / "newest", (start + 5, start + 5))
    assert server._resolve_and_read(None, "C:\\ws", start) == "N"


def test_resolve_and_read_raises_when_unresolvable(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="No conversation found"):
        server._resolve_and_read(None, "C:\\ws", time.time())


# --------------------------------------------------------------------------
# _run_agy bounded poll
# --------------------------------------------------------------------------


def _ok_proc(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


def test_run_agy_polls_until_resolve_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(pinned, ws, start):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("not ready")
        return "answer"

    monkeypatch.setattr(server.subprocess, "run", _ok_proc)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resolve_and_read", flaky)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 5.0)

    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "answer"
    assert calls["n"] == 3


def test_run_agy_reraises_after_poll_deadline(monkeypatch):
    def always_fail(pinned, ws, start):
        raise RuntimeError("No conversation found after agy run")

    monkeypatch.setattr(server.subprocess, "run", _ok_proc)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resolve_and_read", always_fail)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    with pytest.raises(RuntimeError, match="No conversation found"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


# --------------------------------------------------------------------------
# _run_agy orchestration (subprocess mocked)
# --------------------------------------------------------------------------


@pytest.fixture
def fake_agy(monkeypatch, brain_dir, last_conv_file):
    """Mock subprocess.run, capture args, no-op the poll sleep."""
    cap = {"args": None, "kwargs": None, "returncode": 0, "stdout": "", "stderr": ""}

    def fake_run(args, **kwargs):
        cap["args"] = args
        cap["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args, cap["returncode"], stdout=cap["stdout"], stderr=cap["stderr"]
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)
    return cap


def test_run_antigravity_continue_with_pinned_id(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "--conversation" in fake_agy["args"]
    assert "c1" in fake_agy["args"]


def test_run_antigravity_continue_without_id_uses_dash_c(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    _write_transcript(brain_dir, "newest", [_entry("PLANNER_RESPONSE", "ans")])
    os.utime(brain_dir / "newest", (time.time() + 5, time.time() + 5))
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "-c" in fake_agy["args"]
    assert "--conversation" not in fake_agy["args"]


def test_run_antigravity_ask_has_no_continue_flags(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "-c" not in fake_agy["args"]
    assert "--conversation" not in fake_agy["args"]


def test_run_agy_nonzero_exit_raises(fake_agy):
    fake_agy["returncode"] = 1
    fake_agy["stderr"] = "boom"
    with pytest.raises(RuntimeError, match="boom"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_unresolved_conversation_raises(fake_agy, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="No conversation found"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_args_include_print_timeout_and_prompt(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    server._run_agy("my-prompt", "C:\\ws", continue_conv=False, timeout_s=42)
    args = fake_agy["args"]
    assert "--print-timeout" in args
    assert "42s" in args
    assert args[-2:] == ["-p", "my-prompt"]
    assert fake_agy["kwargs"]["cwd"] == "C:\\ws"


# --------------------------------------------------------------------------
# streaming: _entry_to_progress / _transcript_entries / _emit_new_progress
# --------------------------------------------------------------------------


def test_entry_to_progress_planner_first_line():
    e = {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "narrate this\nand more"}
    assert server._entry_to_progress(e) == "narrate this"


def test_entry_to_progress_run_command_label():
    e = {"source": "MODEL", "type": "RUN_COMMAND", "content": "Created At: ..."}
    assert server._entry_to_progress(e) == "running a command…"


def test_entry_to_progress_skips_user_and_system():
    user = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "x"}
    system = {"source": "SYSTEM", "type": "CONVERSATION_HISTORY"}
    assert server._entry_to_progress(user) is None
    assert server._entry_to_progress(system) is None


def test_entry_to_progress_planner_without_content_is_none():
    assert server._entry_to_progress({"source": "MODEL", "type": "PLANNER_RESPONSE"}) is None


def test_transcript_entries_parses_and_skips_malformed(brain_dir):
    _write_transcript(brain_dir, "te", ["{bad", "", _entry("PLANNER_RESPONSE", "ok")])
    entries = server._transcript_entries("te")
    assert len(entries) == 1
    assert entries[0]["content"] == "ok"


def test_transcript_entries_missing_returns_empty(brain_dir):
    assert server._transcript_entries("nope") == []


# --------------------------------------------------------------------------
# watch-mode formatters: _clean_tool_arg / _entry_to_watch_lines
# --------------------------------------------------------------------------


def test_clean_tool_arg_unwraps_json_encoded():
    # agy stores args double-encoded: a quoted/escaped string inside a string.
    assert server._clean_tool_arg('"python -c \\"print(1)\\""') == 'python -c "print(1)"'
    assert server._clean_tool_arg('"Compute 50 factorial"') == "Compute 50 factorial"


def test_clean_tool_arg_passthrough_and_none():
    assert server._clean_tool_arg("plain text") == "plain text"
    assert server._clean_tool_arg(None) == ""


def test_entry_to_watch_lines_planner_narration_and_command():
    cmd_arg = '"python -c \\"print(1)\\""'  # double-encoded as agy stores it
    entry = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "content": "I will compute it.",
        "tool_calls": [{"name": "run_command", "args": {"CommandLine": cmd_arg}}],
    }
    lines = server._entry_to_watch_lines(entry)
    assert ("narration", "I will compute it.") in lines
    assert ("command", 'python -c "print(1)"') in lines


def test_entry_to_watch_lines_command_falls_back_to_summary():
    entry = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "x", "args": {"toolSummary": '"Do the thing"'}}],
    }
    assert server._entry_to_watch_lines(entry) == [("command", "Do the thing")]


def test_entry_to_watch_lines_run_command_marker_and_skips_non_model():
    rc = {"source": "MODEL", "type": "RUN_COMMAND", "content": "Output: 1"}
    assert server._entry_to_watch_lines(rc) == [("result", "command finished")]
    user = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "x"}
    assert server._entry_to_watch_lines(user) == []


def test_progress_stream_pinned_skips_history_then_emits_fresh(brain_dir):
    _write_transcript(
        brain_dir,
        "sc",
        [_entry("PLANNER_RESPONSE", "history a"), _entry("PLANNER_RESPONSE", "history b")],
    )
    got = []
    stream = server._ProgressStream("sc", time.time(), lambda s, m: got.append(m))
    stream.poll()  # cursor starts past the two history entries
    assert got == []
    # a new entry lands for this turn (append to the existing transcript file)
    transcript = brain_dir / "sc" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _entry("PLANNER_RESPONSE", "history a"),
                _entry("PLANNER_RESPONSE", "history b"),
                _entry("PLANNER_RESPONSE", "fresh"),
            ]
        ),
        encoding="utf-8",
    )
    stream.poll()
    assert got == ["fresh"]  # only the post-baseline entry, history not replayed


def test_progress_stream_new_conv_locks_on_and_ignores_others(brain_dir):
    # a prior conversation already exists before the stream starts
    _write_transcript(brain_dir, "old", [_entry("PLANNER_RESPONSE", "OLD STEP")])
    start = time.time()
    got = []
    stream = server._ProgressStream(None, start, lambda s, m: got.append(m))  # snapshots {"old"}
    # this run's new conversation appears after launch
    _write_transcript(
        brain_dir,
        "new",
        [_entry("PLANNER_RESPONSE", "NEW STEP"), _entry("RUN_COMMAND", "Created At: ...")],
    )
    os.utime(brain_dir / "new", (start + 5, start + 5))
    stream.poll()
    assert got == ["NEW STEP", "running a command…"]  # locked to 'new', 'old' ignored


def test_progress_stream_new_conv_emits_nothing_without_new_conv(brain_dir):
    _write_transcript(brain_dir, "old", [_entry("PLANNER_RESPONSE", "OLD STEP")])
    got = []
    stream = server._ProgressStream(None, time.time(), lambda s, m: got.append(m))
    stream.poll()
    assert got == []  # no brand-new conversation -> nothing


# --------------------------------------------------------------------------
# streaming: _run_agy_streamed (subprocess.Popen mocked)
# --------------------------------------------------------------------------


class _FakePopen:
    def __init__(self, *a, polls=1, returncode=0, **k):
        self._polls = polls
        self.returncode = returncode

    def poll(self):
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode

    def communicate(self, timeout=None):
        return ("", "")

    def kill(self):
        self.returncode = -9


def test_run_agy_streamed_emits_progress_and_returns(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "sc"}), encoding="utf-8")

    def create_transcript():
        # agy "writes" the transcript mid-run; it must appear AFTER the stream
        # snapshots existing convs, so it's seen as this run's new conversation.
        _write_transcript(
            brain_dir,
            "sc",
            [
                _entry("PLANNER_RESPONSE", "step one narration"),
                _entry("RUN_COMMAND", "Created At: ..."),
                _entry("PLANNER_RESPONSE", "final answer"),
            ],
        )
        os.utime(brain_dir / "sc", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()  # appears during the run
            return None if self._n > 0 else self.returncode

        def communicate(self):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    progress = []
    out = server._run_agy_streamed(
        "hi",
        "C:\\ws",
        continue_conv=False,
        timeout_s=10,
        on_progress=lambda step, msg: progress.append(msg),
    )
    assert out == "final answer"
    assert "step one narration" in progress
    assert "running a command…" in progress


def test_run_agy_streamed_nonzero_exit_raises(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "sc"}), encoding="utf-8")
    _write_transcript(brain_dir, "sc", [_entry("PLANNER_RESPONSE", "x")])
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda *a, **k: _FakePopen(polls=0, returncode=1)
    )
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="agy exited 1"):
        server._run_agy_streamed(
            "hi", "C:\\ws", continue_conv=False, timeout_s=10, on_progress=lambda s, m: None
        )


# --------------------------------------------------------------------------
# watch mode: browser viewer state + _WatchFeed + _run_agy_watched
# --------------------------------------------------------------------------


def test_watch_state_lifecycle():
    server._watch_reset("my title", 100.0)
    snap = server._watch_snapshot()
    assert snap["status"] == "working"
    assert snap["title"] == "my title"
    assert snap["events"] == []
    server._watch_append([{"kind": "command", "text": "ls", "t": 1.0}])
    assert len(server._watch_snapshot()["events"]) == 1
    server._watch_finish("done", "the answer", 5.0)
    snap = server._watch_snapshot()
    assert snap["status"] == "done"
    assert snap["answer"] == "the answer"
    assert snap["elapsed"] == 5.0
    # snapshot is a copy — mutating it must not affect the shared state
    snap["events"].append("x")
    assert len(server._watch_snapshot()["events"]) == 1


def test_watch_feed_locks_on_new_conv_and_emits_rich_events(brain_dir):
    _write_transcript(brain_dir, "old", [_entry("PLANNER_RESPONSE", "OLD")])
    start = time.time()
    server._watch_reset("t", start)
    feed = server._WatchFeed(None, start)  # snapshots {"old"}
    cmd_arg = '"python -c \\"print(1)\\""'  # double-encoded as agy stores it
    logs = brain_dir / "new" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    entry = json.dumps(
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "I will run it.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": cmd_arg}}],
        }
    )
    (logs / "transcript.jsonl").write_text(entry, encoding="utf-8")
    os.utime(brain_dir / "new", (start + 5, start + 5))

    feed.pump()
    assert feed.conv == "new"  # locked onto this run's conversation, not 'old'
    pairs = [(e["kind"], e["text"]) for e in server._watch_snapshot()["events"]]
    assert ("narration", "I will run it.") in pairs
    assert ("command", 'python -c "print(1)"') in pairs


def test_run_agy_watched_returns_answer_and_populates_state(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")

    def create_transcript():
        _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "final watch answer")])
        os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()  # the conversation appears once agy "starts"
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    opened = {}
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)  # no real server
    monkeypatch.setattr(server, "_open_watch_window", lambda url: opened.update(url=url))
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "final watch answer"
    assert opened.get("url", "").startswith("http://127.0.0.1:12345/")
    snap = server._watch_snapshot()
    assert snap["status"] == "done"
    assert snap["answer"] == "final watch answer"


def test_run_agy_watched_browser_failure_is_nonfatal(monkeypatch, brain_dir, last_conv_file):
    # If opening the browser blows up, the run must still complete and return.
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")
    _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "answer anyway")])
    os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    def boom():
        raise OSError("no display")

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _FakePopen(polls=0))
    monkeypatch.setattr(server, "_ensure_watch_server", boom)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "answer anyway"


def test_open_watch_window_uses_chromium_app_mode(monkeypatch):
    monkeypatch.setattr(server, "_chromium_app_browsers", lambda: ["/opt/chrome"])
    captured = {}
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda args, **k: captured.update(args=args) or object()
    )
    server._open_watch_window("http://127.0.0.1:9/")
    assert captured["args"][0] == "/opt/chrome"
    assert "--app=http://127.0.0.1:9/" in captured["args"]
    assert any(a.startswith("--window-size=") for a in captured["args"])


def test_open_watch_window_falls_back_to_new_window(monkeypatch):
    monkeypatch.setattr(server, "_chromium_app_browsers", lambda: [])  # no Chromium found
    opened = {}
    monkeypatch.setattr(
        server.webbrowser, "open", lambda url, new=0: opened.update(url=url, new=new)
    )
    server._open_watch_window("http://x/")
    assert opened == {"url": "http://x/", "new": 1}


def test_watch_html_substitutes_window_size(monkeypatch):
    monkeypatch.setattr(server, "_WATCH_WINDOW_SIZE", "480,640")
    html = server._watch_html()
    assert "window.resizeTo(480,640)" in html
    assert "__WIN_W__" not in html and "__WIN_H__" not in html


def test_watch_html_bad_size_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(server, "_WATCH_WINDOW_SIZE", "garbage")
    html = server._watch_html()
    assert "window.resizeTo(600,820)" in html


def test_watch_reset_clears_image():
    server._watch_set_image("C:/x/pic.png")
    assert server._watch_snapshot()["image"] == "C:/x/pic.png"
    server._watch_reset("t", 1.0)
    assert server._watch_snapshot()["image"] == ""


def test_run_agy_image_watched_shows_image_and_returns(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "ic"}), encoding="utf-8")

    def create_transcript():
        _write_transcript(brain_dir, "ic", [_entry("PLANNER_RESPONSE", "C:/out/art.jpg")])
        os.utime(brain_dir / "ic", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda url: None)
    monkeypatch.setattr(
        server, "_finalize_image", lambda target, txt, start: ("C:/out/art.jpg", "JPEG", 2048)
    )
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_image_watched(
        "wrapped prompt", "C:/out/art.png", "C:\\ws", 10, "draw a cat"
    )
    assert "C:/out/art.jpg" in out
    assert "format=JPEG" in out
    assert server._watch_snapshot()["image"] == "C:/out/art.jpg"


# --------------------------------------------------------------------------
# _collect_status
# --------------------------------------------------------------------------


@pytest.fixture
def status_dirs(tmp_path, monkeypatch):
    data = tmp_path / "antigravity-cli"
    brain = data / "brain"
    conv = data / "conversations"
    last = data / "cache" / "last_conversations.json"
    brain.mkdir(parents=True)
    conv.mkdir(parents=True)
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "AGY_DATA", data)
    monkeypatch.setattr(server, "BRAIN_DIR", brain)
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", conv)
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", last)
    return {"data": data, "brain": brain, "conv": conv, "last": last}


def _status_dict(rows):
    return {label: (ok, detail) for label, ok, detail in rows}


def test_collect_status_all_ok(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    _write_transcript(status_dirs["brain"], "c1", [_entry("PLANNER_RESPONSE", "ans")])
    (status_dirs["conv"] / "c1.db").write_text("", encoding="utf-8")
    rows = server._collect_status()
    d = _status_dict(rows)
    assert d["agy CLI"][0] is True
    assert d["base dir"][0] is True
    assert d["brain dir"][0] is True
    assert d["newest transcript"][0] is True
    assert all(ok for _, ok, _ in rows)


def test_collect_status_agy_missing(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    rows = server._collect_status()
    assert _status_dict(rows)["agy CLI"][0] is False


def test_collect_status_dirs_absent(tmp_path, monkeypatch):
    missing = tmp_path / "nope"
    monkeypatch.setattr(server, "AGY_DATA", missing)
    monkeypatch.setattr(server, "BRAIN_DIR", missing / "brain")
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", missing / "conversations")
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", missing / "cache" / "last.json")
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    rows = server._collect_status()
    d = _status_dict(rows)
    assert d["base dir"][0] is False
    assert d["brain dir"][0] is False


def test_collect_status_unreadable_transcript(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    (status_dirs["brain"] / "c1").mkdir()  # conv dir exists but no transcript
    rows = server._collect_status()
    assert _status_dict(rows)["newest transcript"][0] is False


def test_antigravity_status_formats_report(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    out = server.antigravity_status()
    assert out.startswith("agy bridge status")
    assert "[ok]" in out
    assert "Overall:" in out


# --------------------------------------------------------------------------
# image generation: byte fixtures + _detect_image_format / ext helpers
# --------------------------------------------------------------------------

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_GIF = b"GIF89a" + b"\x00" * 8
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 4


def test_detect_image_format_jpeg(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_JPEG)
    assert server._detect_image_format(str(p)) == "JPEG"


def test_detect_image_format_png(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_PNG)
    assert server._detect_image_format(str(p)) == "PNG"


def test_detect_image_format_gif(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_GIF)
    assert server._detect_image_format(str(p)) == "GIF"


def test_detect_image_format_webp(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_WEBP)
    assert server._detect_image_format(str(p)) == "WEBP"


def test_detect_image_format_text_is_none(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(b"not an image at all")
    assert server._detect_image_format(str(p)) is None


def test_detect_image_format_missing_file_is_none(tmp_path):
    assert server._detect_image_format(str(tmp_path / "nope")) is None


def test_canonical_ext_maps_known_formats():
    assert server._canonical_ext("JPEG") == ".jpg"
    assert server._canonical_ext("PNG") == ".png"
    assert server._canonical_ext("GIF") == ".gif"
    assert server._canonical_ext("WEBP") == ".webp"


def test_with_ext_replaces_extension():
    assert server._with_ext("C:\\a\\b.png", ".jpg") == "C:\\a\\b.jpg"
    assert server._with_ext("/a/b/c.jpeg", ".jpg") == "/a/b/c.jpg"


# --------------------------------------------------------------------------
# _resolve_output_path
# --------------------------------------------------------------------------


def test_resolve_output_path_default_name(tmp_path):
    out = server._resolve_output_path(None, str(tmp_path))
    assert out.startswith(os.path.join(str(tmp_path), "agy-image-"))
    assert out.endswith(".png")


def test_resolve_output_path_relative_joined_to_workspace(tmp_path):
    out = server._resolve_output_path("sub/pic.png", str(tmp_path))
    assert out == os.path.abspath(os.path.join(str(tmp_path), "sub/pic.png"))


def test_resolve_output_path_absolute_kept(tmp_path):
    p = str(tmp_path / "abs.png")
    assert server._resolve_output_path(p, "C:\\other") == os.path.abspath(p)


# --------------------------------------------------------------------------
# _newest_scratch_image_after
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_dir(tmp_path, monkeypatch):
    d = tmp_path / "scratch"
    d.mkdir()
    monkeypatch.setattr(server, "SCRATCH_DIR", d)
    return d


def test_newest_scratch_image_after_picks_newest_image(scratch_dir):
    start = time.time()
    img = scratch_dir / "x.png"
    img.write_bytes(_JPEG)
    os.utime(img, (start + 5, start + 5))
    assert server._newest_scratch_image_after(start) == str(img)


def test_newest_scratch_image_after_ignores_nonimage(scratch_dir):
    start = time.time()
    f = scratch_dir / "notes.txt"
    f.write_bytes(b"hello")
    os.utime(f, (start + 5, start + 5))
    assert server._newest_scratch_image_after(start) is None


def test_newest_scratch_image_after_ignores_old(scratch_dir):
    start = time.time()
    img = scratch_dir / "old.png"
    img.write_bytes(_JPEG)
    os.utime(img, (start - 100, start - 100))
    assert server._newest_scratch_image_after(start) is None


def test_newest_scratch_image_after_missing_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SCRATCH_DIR", tmp_path / "nope")
    assert server._newest_scratch_image_after(time.time()) is None


# --------------------------------------------------------------------------
# _wrap_image_prompt
# --------------------------------------------------------------------------


def test_wrap_image_prompt_embeds_target_and_prompt():
    w = server._wrap_image_prompt("a red cat", "C:\\out\\img.png")
    assert "a red cat" in w
    assert "C:\\out\\img.png" in w
    assert "absolute file path" in w


def test_wrap_image_prompt_avoids_double_period():
    w = server._wrap_image_prompt("a red cat.", "C:\\out\\img.png")
    assert w.startswith("a red cat. Save")  # no ".." when prompt already ends in '.'


# --------------------------------------------------------------------------
# _finalize_image
# --------------------------------------------------------------------------


def test_finalize_image_corrects_extension_at_target(tmp_path, scratch_dir):
    (tmp_path / "art.png").write_bytes(_JPEG)  # JPEG bytes under a .png name
    target = str(tmp_path / "art.png")
    final, fmt, size = server._finalize_image(target, None, time.time())
    assert final == str(tmp_path / "art.jpg")
    assert fmt == "JPEG"
    assert size == len(_JPEG)
    assert os.path.isfile(tmp_path / "art.jpg")
    assert not os.path.isfile(target)


def test_finalize_image_moves_scratch_file_to_target(tmp_path, scratch_dir):
    start = time.time()
    s = scratch_dir / "gen.png"
    s.write_bytes(_JPEG)
    os.utime(s, (start + 5, start + 5))
    target = str(tmp_path / "out.png")  # does not exist
    final, fmt, size = server._finalize_image(target, None, start)
    assert final == str(tmp_path / "out.jpg")
    assert fmt == "JPEG"
    assert os.path.isfile(final)
    assert not os.path.exists(s)


def test_finalize_image_not_found_raises(tmp_path, scratch_dir):
    target = str(tmp_path / "missing.png")
    with pytest.raises(RuntimeError, match="no image file found"):
        server._finalize_image(target, None, time.time())


def test_finalize_image_non_image_raises(tmp_path, scratch_dir):
    (tmp_path / "refusal.png").write_bytes(b"I cannot create that image.")
    target = str(tmp_path / "refusal.png")
    with pytest.raises(RuntimeError, match="not a recognized image"):
        server._finalize_image(target, None, time.time())


def test_finalize_image_uses_agy_text_when_target_missing(tmp_path, scratch_dir):
    (tmp_path / "actual.jpg").write_bytes(_JPEG)
    agy_path = str(tmp_path / "actual.jpg")
    target = str(tmp_path / "requested.png")  # never created
    final, fmt, size = server._finalize_image(target, agy_path, time.time())
    assert fmt == "JPEG"
    assert final == str(tmp_path / "requested.jpg")  # landed at target's base name
    assert os.path.isfile(final)
    assert not os.path.exists(tmp_path / "actual.jpg")  # moved, not copied


# --------------------------------------------------------------------------
# antigravity_image (orchestration; _run_agy mocked)
# --------------------------------------------------------------------------


def test_antigravity_image_happy_path(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def fake_run(prompt, ws, continue_conv, timeout_s):
        (tmp_path / "art.png").write_bytes(_JPEG)  # agy saves JPEG under .png
        return target

    monkeypatch.setattr(server, "_run_agy", fake_run)
    out = server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    assert str(tmp_path / "art.jpg") in out
    assert "format=JPEG" in out
    assert os.path.isfile(tmp_path / "art.jpg")


def test_antigravity_image_recovers_when_run_agy_raises(tmp_path, scratch_dir, monkeypatch):
    (tmp_path / "art.png").write_bytes(_JPEG)  # file already on disk
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("transcript read failed")

    monkeypatch.setattr(server, "_run_agy", boom)
    out = server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    assert "format=JPEG" in out


def test_antigravity_image_raises_when_nothing_produced(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="no image file found"):
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))


def test_antigravity_image_error_mentions_agy_failure(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="agy also failed"):
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
