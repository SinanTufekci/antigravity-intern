"""Offline unit tests for the pure logic in server.py.

These use temp fixtures and never invoke agy, so they cost no AI Pro quota and
can run anywhere (including CI). For the live end-to-end check, see
test_smoke.py instead.

    pytest test_server.py
"""

import json
import os
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
    assert "1.0.5" in msg  # the verified baseline it's compared to


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
# _startup_checks  (composition of the tested helpers; agy version injected)
# --------------------------------------------------------------------------


def test_startup_checks_warns_on_newer_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "2.0.0")
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "newer" in caplog.text


def test_startup_checks_silent_on_verified_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
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
