"""Offline unit tests for the pure logic in server.py.

These use temp fixtures and never invoke agy, so they cost no AI Pro quota and
can run anywhere (including CI). For the live end-to-end check, see
test_smoke.py instead.

    pytest test_server.py
"""

import asyncio
import json
import os
import sqlite3
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
    # Isolate the SQLite fallback too, so _read_response never reads the real store.
    conv = tmp_path / "conversations"
    conv.mkdir()
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", conv)
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
    assert "1.0.10" in msg  # the verified baseline it's compared to


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
# _env_truthy  (generic truthy env-var reader behind _debug_enabled etc.)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_truthy_true(monkeypatch, value):
    monkeypatch.setenv("AGY_TEST_FLAG", value)
    assert server._env_truthy("AGY_TEST_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_truthy_false(monkeypatch, value):
    monkeypatch.setenv("AGY_TEST_FLAG", value)
    assert server._env_truthy("AGY_TEST_FLAG") is False


def test_env_truthy_false_when_unset(monkeypatch):
    monkeypatch.delenv("AGY_TEST_FLAG", raising=False)
    assert server._env_truthy("AGY_TEST_FLAG") is False


# --------------------------------------------------------------------------
# _update_warning  (nag when a newer bridge tag exists on GitHub)
# --------------------------------------------------------------------------


def test_update_warning_warns_for_newer(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    msg = server._update_warning((0, 9, 0))
    assert msg is not None
    assert "0.9.0" in msg  # the newer version available
    assert "0.8.0" in msg  # the version currently running
    assert "git pull" in msg


def test_update_warning_none_for_equal(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    assert server._update_warning((0, 8, 0)) is None


def test_update_warning_none_for_older(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    assert server._update_warning((0, 7, 5)) is None


def test_update_warning_none_when_latest_unknown():
    assert server._update_warning(None) is None


def test_update_warning_none_when_current_unparseable(monkeypatch):
    monkeypatch.setattr(server, "__version__", "not-a-version")
    assert server._update_warning((9, 9, 9)) is None


# --------------------------------------------------------------------------
# _fetch_latest_release_version  (GitHub tags API; never raises on the network)
# --------------------------------------------------------------------------


class _FakeResp:
    """Minimal urlopen() stand-in: works as a context manager and feeds json.load."""

    def __init__(self, body: str):
        self._body = body.encode()

    def read(self, *_a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_fetch_latest_release_version_picks_highest(monkeypatch):
    body = '[{"name": "v0.5.0"}, {"name": "v0.8.0"}, {"name": "v0.7.1"}, {"name": "nightly"}]'
    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *a, **k: _FakeResp(body))
    assert server._fetch_latest_release_version() == (0, 8, 0)


def test_fetch_latest_release_version_none_on_network_error(monkeypatch):
    def _raise(*_a, **_k):
        raise server.urllib.error.URLError("offline")

    monkeypatch.setattr(server.urllib.request, "urlopen", _raise)
    assert server._fetch_latest_release_version() is None


def test_fetch_latest_release_version_none_on_non_list(monkeypatch):
    # rate-limit / error bodies come back as a JSON object, not a list of tags
    monkeypatch.setattr(
        server.urllib.request, "urlopen", lambda *a, **k: _FakeResp('{"message": "rate limited"}')
    )
    assert server._fetch_latest_release_version() is None


def test_fetch_latest_release_version_none_when_no_semver_tags(monkeypatch):
    monkeypatch.setattr(
        server.urllib.request, "urlopen", lambda *a, **k: _FakeResp('[{"name": "latest"}]')
    )
    assert server._fetch_latest_release_version() is None


# --------------------------------------------------------------------------
# _bridge_version_status  (surfaces the update notice in antigravity_status)
# --------------------------------------------------------------------------


def test_bridge_version_status_flags_newer_release(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "__version__", "0.10.1")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 10, 2))
    label, ok, detail = server._bridge_version_status()
    assert label == "bridge version"
    assert ok is True  # an available update is informational, not a fault
    assert "0.10.2" in detail and "available" in detail
    assert "uvx agent-intern@latest" in detail


def test_bridge_version_status_reports_latest(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "__version__", "0.10.1")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 10, 1))
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "latest" in detail and "available" not in detail


def test_bridge_version_status_unavailable_when_offline(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "unavailable" in detail


def test_bridge_version_status_respects_opt_out(monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_NO_UPDATE_CHECK", "1")

    def _boom():
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr(server, "_fetch_latest_release_version", _boom)
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "disabled" in detail


def test_collect_status_first_row_is_bridge_version(monkeypatch):
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)  # skip the agy subprocess
    rows = server._collect_status()
    assert rows[0][0] == "bridge version"


# --------------------------------------------------------------------------
# _run_with_progress  (threaded agy run + best-effort MCP progress notifications)
# --------------------------------------------------------------------------


def test_run_with_progress_no_ctx_returns_result():
    # ctx=None (direct call / no progressToken): plain threaded call, no progress.
    result = asyncio.run(server._run_with_progress(lambda a, b: f"{a}-{b}", ("x", "y"), None, 10))
    assert result == "x-y"


def test_run_with_progress_reports_progress_with_ctx(monkeypatch):
    monkeypatch.setattr(server, "_PROGRESS_NOTIFY_INTERVAL_S", 0.02)

    class _Ctx:
        def __init__(self):
            self.calls = 0

        async def report_progress(self, progress, total=None, message=None):
            self.calls += 1
            assert 0 <= progress <= total  # time bar stays within [0, timeout]

    ctx = _Ctx()

    def slow():
        time.sleep(0.15)  # spans several 0.02s notify intervals
        return "done"

    result = asyncio.run(server._run_with_progress(slow, (), ctx, 10))
    assert result == "done"
    assert ctx.calls >= 1


def test_run_with_progress_propagates_worker_errors():
    def boom():
        raise RuntimeError("agy failed")

    with pytest.raises(RuntimeError, match="agy failed"):
        asyncio.run(server._run_with_progress(boom, (), None, 10))


def test_run_with_progress_survives_progress_errors(monkeypatch):
    # A throwing report_progress must not break the run — progress is cosmetic.
    monkeypatch.setattr(server, "_PROGRESS_NOTIFY_INTERVAL_S", 0.02)

    class _BadCtx:
        async def report_progress(self, *a, **k):
            raise RuntimeError("transport down")

    def slow():
        time.sleep(0.1)
        return "ok"

    assert asyncio.run(server._run_with_progress(slow, (), _BadCtx(), 10)) == "ok"


# --------------------------------------------------------------------------
# _spawn_kwargs  (console-detach so agy's TTY writes don't leak to the host)
# --------------------------------------------------------------------------


def test_spawn_kwargs_detaches_per_platform():
    # Pass the platform explicitly — monkeypatching os.name globally would break
    # pathlib (and pytest's own per-test bookkeeping) on non-Windows CI runners.
    # CREATE_NO_WINDOW == 0x08000000; assert the literal so it's host-portable.
    assert server._spawn_kwargs("nt") == {"creationflags": 0x08000000}
    assert server._spawn_kwargs("posix") == {"start_new_session": True}


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
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "newer" in caplog.text


def test_startup_checks_silent_on_verified_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


def test_startup_checks_silent_when_agy_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


def test_startup_checks_warns_on_newer_bridge_release(monkeypatch, caplog):
    # agy is fine; a newer bridge tag exists on GitHub -> update nag fires.
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setattr(server, "__version__", "0.8.0")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 9, 0))
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "0.9.0" in caplog.text
    assert "git pull" in caplog.text


def test_startup_checks_skips_update_check_when_disabled(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setenv("AGY_BRIDGE_NO_UPDATE_CHECK", "1")

    def _boom():
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr(server, "_fetch_latest_release_version", _boom)
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
# transcript reading: _transcript_entries
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# subprocess test double (shared by the watched-run tests)
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
    out = asyncio.run(
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    )
    assert str(tmp_path / "art.jpg") in out
    assert "format=JPEG" in out
    assert os.path.isfile(tmp_path / "art.jpg")


def test_antigravity_image_recovers_when_run_agy_raises(tmp_path, scratch_dir, monkeypatch):
    (tmp_path / "art.png").write_bytes(_JPEG)  # file already on disk
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("transcript read failed")

    monkeypatch.setattr(server, "_run_agy", boom)
    out = asyncio.run(
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    )
    assert "format=JPEG" in out


def test_antigravity_image_raises_when_nothing_produced(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="no image file found"):
        asyncio.run(server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path)))


def test_antigravity_image_error_mentions_agy_failure(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="agy also failed"):
        asyncio.run(server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path)))


def test_viewer_is_live_reflects_recent_poll(monkeypatch):
    # No recent /events poll -> not live, so a new watch run opens a window.
    monkeypatch.setattr(server, "_WATCH_LAST_POLL", 0.0)
    assert server._viewer_is_live() is False
    # A poll within the alive window -> live, so a new run reuses the open window.
    monkeypatch.setattr(server, "_WATCH_LAST_POLL", time.time())
    assert server._viewer_is_live() is True


# --------------------------------------------------------------------------
# SQLite (.db) transcript fallback: protobuf helpers + _read_response_db
# --------------------------------------------------------------------------


def _pb_enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_tag(field, wt):
    return _pb_enc_varint((field << 3) | wt)


def _pb_str(field, s):
    b = s.encode("utf-8")
    return _pb_tag(field, 2) + _pb_enc_varint(len(b)) + b


def _pb_varint_field(field, n):
    return _pb_tag(field, 0) + _pb_enc_varint(n)


def _pb_submsg(field, payload):
    return _pb_tag(field, 2) + _pb_enc_varint(len(payload)) + payload


def _planner_payload(text):
    """A step_payload shaped like agy's: step_type(f1)=15, status(f4)=3, and the
    answer at field 20 -> field 1 (the layout _read_response_db reads)."""
    return _pb_varint_field(1, 15) + _pb_varint_field(4, 3) + _pb_submsg(20, _pb_str(1, text))


def _make_steps_db(path, rows):
    """rows: (idx, step_type, status, step_payload) tuples -> a minimal agy `.db`."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, "
        "status integer NOT NULL DEFAULT 0, step_payload blob, PRIMARY KEY (idx))"
    )
    con.executemany(
        "INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?,?,?,?)", rows
    )
    con.commit()
    con.close()


def test_pb_wire_roundtrip():
    blob = _pb_varint_field(1, 15) + _pb_str(3, "héllo") + _pb_submsg(20, _pb_str(1, "answer"))
    fields = server._pb_fields(blob)
    assert (1, 0, 15) in fields  # varint field
    assert server._pb_bytes(fields, 3)[0].decode("utf-8") == "héllo"  # string field
    sub = server._pb_bytes(fields, 20)[0]  # sub-message
    assert server._pb_bytes(server._pb_fields(sub), 1)[0].decode("utf-8") == "answer"


def test_pb_fields_tolerates_garbage():
    # Best-effort: malformed trailing bytes must not raise.
    assert isinstance(server._pb_fields(b"\xff\xff\xff"), list)
    assert server._pb_fields(b"") == []


def test_read_response_db_returns_last_done_planner(tmp_path, monkeypatch):
    conv = "11111111-1111-1111-1111-111111111111"
    _make_steps_db(
        str(tmp_path / f"{conv}.db"),
        [
            (0, 15, 3, _planner_payload("first draft")),  # earlier planner response
            (1, 8, 3, b"\x08\x08tool-step"),  # non-planner step (filtered out)
            (2, 15, 0, _planner_payload("still working")),  # planner but not DONE (filtered)
            (3, 15, 3, _planner_payload("FINAL ✓ answer")),  # last completed planner -> wins
        ],
    )
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    assert server._read_response_db(conv) == "FINAL ✓ answer"


def test_read_response_db_missing_or_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    assert server._read_response_db("does-not-exist") is None
    conv = "22222222-2222-2222-2222-222222222222"
    _make_steps_db(str(tmp_path / f"{conv}.db"), [(0, 8, 3, b"\x08\x08only-a-tool")])
    assert server._read_response_db(conv) is None  # no planner-response step


def test_read_response_falls_back_to_db(tmp_path, monkeypatch):
    conv = "33333333-3333-3333-3333-333333333333"
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "brain")  # no JSONL transcript
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    _make_steps_db(str(tmp_path / f"{conv}.db"), [(0, 15, 3, _planner_payload("from the db"))])
    assert server._read_response(conv) == "from the db"


def test_read_response_raises_when_neither_source(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "brain")
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        server._read_response("44444444-4444-4444-4444-444444444444")
