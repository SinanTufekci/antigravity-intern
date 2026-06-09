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
    assert "1.0.6" in msg  # the verified baseline it's compared to


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
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.6")
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


def test_run_agy_continue_with_pinned_id(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "--conversation" in fake_agy["args"]
    assert "c1" in fake_agy["args"]


def test_run_agy_continue_without_id_uses_dash_c(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    _write_transcript(brain_dir, "newest", [_entry("PLANNER_RESPONSE", "ans")])
    os.utime(brain_dir / "newest", (time.time() + 5, time.time() + 5))
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "-c" in fake_agy["args"]
    assert "--conversation" not in fake_agy["args"]


def test_run_agy_ask_has_no_continue_flags(fake_agy, brain_dir, last_conv_file):
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


def test_agy_status_formats_report(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    out = server.agy_status()
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
    assert os.path.isfile(final)


# --------------------------------------------------------------------------
# agy_image (orchestration; _run_agy mocked)
# --------------------------------------------------------------------------


def test_agy_image_happy_path(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def fake_run(prompt, ws, continue_conv, timeout_s):
        (tmp_path / "art.png").write_bytes(_JPEG)  # agy saves JPEG under .png
        return target

    monkeypatch.setattr(server, "_run_agy", fake_run)
    out = server.agy_image("a cat", output_path=target, workspace=str(tmp_path))
    assert str(tmp_path / "art.jpg") in out
    assert "format=JPEG" in out
    assert os.path.isfile(tmp_path / "art.jpg")


def test_agy_image_recovers_when_run_agy_raises(tmp_path, scratch_dir, monkeypatch):
    (tmp_path / "art.png").write_bytes(_JPEG)  # file already on disk
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("transcript read failed")

    monkeypatch.setattr(server, "_run_agy", boom)
    out = server.agy_image("a cat", output_path=target, workspace=str(tmp_path))
    assert "format=JPEG" in out


def test_agy_image_raises_when_nothing_produced(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="no image file found"):
        server.agy_image("a cat", output_path=target, workspace=str(tmp_path))


def test_agy_image_error_mentions_agy_failure(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="agy also failed"):
        server.agy_image("a cat", output_path=target, workspace=str(tmp_path))
