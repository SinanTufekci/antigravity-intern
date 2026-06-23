"""Offline unit tests for the pure logic in swarm.py and swarm_watch.py.

Like test_server.py these use temp fixtures and never invoke agy, so they cost no
AI Pro quota. The live parallel round-trip is in test_smoke.py.

    pytest test_swarm.py
"""

import os

import pytest

import server
import swarm
import swarm_watch

# --------------------------------------------------------------------------
# _normalize_workspaces
# --------------------------------------------------------------------------


def test_normalize_workspaces_none_is_cwd_for_all():
    assert swarm._normalize_workspaces(3, None) == [os.getcwd()] * 3


def test_normalize_workspaces_str_broadcasts(tmp_path):
    out = swarm._normalize_workspaces(2, str(tmp_path))
    assert out == [os.path.abspath(str(tmp_path))] * 2


def test_normalize_workspaces_list_per_worker(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    out = swarm._normalize_workspaces(2, [str(a), str(b)])
    assert out == [os.path.abspath(str(a)), os.path.abspath(str(b))]


def test_normalize_workspaces_length_mismatch_raises():
    with pytest.raises(ValueError):
        swarm._normalize_workspaces(3, ["only", "two"])


# --------------------------------------------------------------------------
# _labels / _repos
# --------------------------------------------------------------------------


def test_labels_takes_first_nonempty_line():
    assert swarm._labels(["first line\nsecond"])[0] == "first line"


def test_labels_truncates_long_prompts():
    label = swarm._labels(["x" * 200])[0]
    assert label.endswith("…") and len(label) == 121  # 120 chars + ellipsis


def test_labels_empty_prompt():
    assert swarm._labels(["   "])[0] == "(empty)"


def test_repos_uses_basename():
    assert swarm._repos(["C:\\a\\b\\my-repo", "/x/y/other"]) == ["my-repo", "other"]


# --------------------------------------------------------------------------
# isolated HOME helpers
# --------------------------------------------------------------------------


def test_make_isolated_home_creates_state_dir():
    home = swarm._make_isolated_home()
    try:
        assert (home / ".gemini" / "antigravity-cli").is_dir()
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def test_env_for_home_redirects_home(tmp_path):
    env = swarm._env_for_home(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)


def _make_brain(home, conv_id, entries):
    """Write a fake isolated transcript for conv_id with the given JSONL entries."""
    import json

    d = home / ".gemini" / "antigravity-cli" / "brain" / conv_id / ".system_generated" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_only_conv_none_when_empty(tmp_path):
    assert swarm._only_conv(tmp_path) is None


def test_only_conv_returns_single(tmp_path):
    _make_brain(tmp_path, "conv-xyz", [])
    assert swarm._only_conv(tmp_path) == "conv-xyz"


def test_read_isolated_response_returns_last_planner(tmp_path):
    entries = [
        {"source": "MODEL", "status": "DONE", "type": "PLANNER_RESPONSE", "content": "first"},
        {"source": "MODEL", "status": "DONE", "type": "PLANNER_RESPONSE", "content": "final"},
    ]
    _make_brain(tmp_path, "c1", entries)
    assert swarm._read_isolated_response(tmp_path, "c1") == "final"


def test_read_isolated_response_raises_without_done(tmp_path):
    _make_brain(tmp_path, "c2", [{"source": "MODEL", "status": "RUNNING", "type": "X"}])
    with pytest.raises(RuntimeError):
        swarm._read_isolated_response(tmp_path, "c2")


# --------------------------------------------------------------------------
# _finalize_image_isolated (extension correction to the real bytes)
# --------------------------------------------------------------------------

_JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_finalize_image_corrects_extension(tmp_path):
    # Requested .png, but the bytes are JPEG -> final path must be .jpg.
    target = str(tmp_path / "out.png")
    with open(target, "wb") as f:
        f.write(_JPEG_HEAD)
    final, fmt, size = swarm._finalize_image_isolated(tmp_path, target, None, 0.0)
    assert fmt == "JPEG"
    assert final.endswith(".jpg")
    assert os.path.isfile(final) and size > 0


def test_finalize_image_keeps_matching_extension(tmp_path):
    target = str(tmp_path / "pic.png")
    with open(target, "wb") as f:
        f.write(_PNG_HEAD)
    final, fmt, _ = swarm._finalize_image_isolated(tmp_path, target, None, 0.0)
    assert fmt == "PNG" and final.endswith(".png")


def test_finalize_image_missing_raises(tmp_path):
    with pytest.raises(RuntimeError):
        swarm._finalize_image_isolated(tmp_path, str(tmp_path / "nope.png"), None, 0.0)


# --------------------------------------------------------------------------
# result formatting
# --------------------------------------------------------------------------


def test_format_text_results_orders_and_marks():
    results = [
        swarm.WorkerResult(1, False, error="boom", elapsed=0.0, workspace="C:\\x\\repo"),
        swarm.WorkerResult(0, True, answer="hello", elapsed=1.2, workspace="C:\\x\\repo"),
    ]
    out = swarm.format_text_results(results)
    assert "1/2 succeeded" in out
    assert out.index("[worker 0]") < out.index("[worker 1]")  # sorted by index
    assert "hello" in out and "boom" in out
    assert "@ repo" in out


def test_format_image_results():
    results = [
        swarm.WorkerResult(
            0, True, elapsed=2.0, image_path="C:\\a.png", image_format="PNG", image_size=123
        ),
        swarm.WorkerResult(1, False, error="nope", elapsed=0.0),
    ]
    out = swarm.format_image_results(results)
    assert "1/2 succeeded" in out
    assert "C:\\a.png" in out and "PNG" in out and "nope" in out


# --------------------------------------------------------------------------
# server._broadcast_workspaces (MCP arg -> swarm contract)
# --------------------------------------------------------------------------


def test_broadcast_none():
    assert server._broadcast_workspaces(None, 3) is None
    assert server._broadcast_workspaces([], 3) is None


def test_broadcast_single_to_str():
    assert server._broadcast_workspaces(["C:\\p"], 3) == "C:\\p"


def test_broadcast_list_passthrough():
    assert server._broadcast_workspaces(["a", "b"], 2) == ["a", "b"]


# --------------------------------------------------------------------------
# swarm_watch in-memory dashboard state
# --------------------------------------------------------------------------


def test_watch_state_lifecycle():
    swarm_watch.init(["promptA", "promptB"], ["repo1", "repo2"], 100.0)
    snap = swarm_watch._snapshot()
    assert len(snap["workers"]) == 2
    assert snap["workers"][0]["label"] == "promptA"
    assert snap["workers"][1]["repo"] == "repo2"
    assert all(w["status"] == "queued" for w in snap["workers"])

    swarm_watch.worker_update(0, status="working", elapsed=1.0)
    swarm_watch.worker_append(0, [{"kind": "command", "text": "ls", "t": 0.5}])
    swarm_watch.worker_finish(1, "done", "the answer", 3.0, image="C:\\img.png")

    snap = swarm_watch._snapshot()
    assert snap["workers"][0]["status"] == "working"
    assert snap["workers"][0]["events"][0]["text"] == "ls"
    assert snap["workers"][1]["answer"] == "the answer"
    assert swarm_watch._allowed_images() == {"C:\\img.png"}


def test_watch_full_prompt_falls_back_to_label():
    # Without explicit prompts, the detail-window prompt mirrors the row label.
    swarm_watch.init(["short label"], ["repo"], 1.0)
    assert swarm_watch._snapshot()["workers"][0]["prompt"] == "short label"


def test_watch_full_prompt_kept_untruncated():
    # The full prompt is stored verbatim for the detail window even though the
    # row label is the clipped, single-line caption.
    full = "Kısaca açıkla: " + "x" * 500
    swarm_watch.init(["clipped…"], ["repo"], 1.0, [full])
    w = swarm_watch._snapshot()["workers"][0]
    assert w["label"] == "clipped…"
    assert w["prompt"] == full


def test_watch_init_stores_timeout():
    # The per-worker time progress bar needs the timeout in dashboard state.
    swarm_watch.init(["p"], ["repo"], 1.0, ["p"], 240)
    assert swarm_watch._snapshot()["timeout"] == 240


def test_dashboard_is_live_reflects_recent_poll(monkeypatch):
    # No recent /events poll -> not live, so a new swarm run opens a window.
    monkeypatch.setattr(swarm_watch, "_LAST_POLL", 0.0)
    assert swarm_watch._dashboard_is_live() is False
    # A poll within the alive window -> live, so a new run reuses the open dashboard.
    monkeypatch.setattr(swarm_watch, "_LAST_POLL", swarm_watch.time.time())
    assert swarm_watch._dashboard_is_live() is True


# --------------------------------------------------------------------------
# unified agent swarm: _normalize_tasks / swarm_agents / format_agent_results
# --------------------------------------------------------------------------


def test_normalize_tasks_mixed_and_aliases(tmp_path):
    out = swarm._normalize_tasks(
        [
            {"backend": "agy", "prompt": "a", "workspace": str(tmp_path)},
            {"backend": "Codex", "prompt": "b", "sandbox": "workspace-write", "model": "m"},
            {"backend": "gemini", "prompt": "c"},
        ]
    )
    assert [t["backend"] for t in out] == ["antigravity", "codex", "antigravity"]
    assert out[0]["workspace"] == os.path.abspath(str(tmp_path))
    assert out[1]["sandbox"] == "workspace-write" and out[1]["model"] == "m"
    # antigravity drops codex-only fields and defaults workspace to cwd
    assert out[2]["sandbox"] is None and out[2]["model"] is None
    assert out[2]["workspace"] == os.getcwd()


def test_normalize_tasks_codex_default_sandbox():
    import codex_bridge

    out = swarm._normalize_tasks([{"backend": "codex", "prompt": "x"}])
    assert out[0]["sandbox"] == codex_bridge.DEFAULT_SANDBOX


def test_normalize_tasks_antigravity_ignores_sandbox_model():
    out = swarm._normalize_tasks(
        [{"backend": "antigravity", "prompt": "x", "sandbox": "danger-full-access", "model": "z"}]
    )
    assert out[0]["sandbox"] is None and out[0]["model"] is None


def test_normalize_tasks_bad_backend_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "llama", "prompt": "x"}])


def test_normalize_tasks_missing_prompt_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "codex", "prompt": "  "}])


def test_normalize_tasks_invalid_sandbox_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "codex", "prompt": "x", "sandbox": "yolo"}])


def test_normalize_tasks_non_list_and_non_dict_raise():
    with pytest.raises(ValueError):
        swarm._normalize_tasks("nope")
    with pytest.raises(ValueError):
        swarm._normalize_tasks([["not", "a", "dict"]])


def test_swarm_agents_dispatches_by_backend(monkeypatch):
    calls = []

    def fake_text(index, prompt, workspace, timeout_s):
        calls.append(("antigravity", index, prompt))
        return swarm.WorkerResult(index, True, answer="agy:" + prompt, workspace=workspace)

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s):
        calls.append(("codex", index, prompt, sandbox, model))
        return swarm.WorkerResult(index, True, answer="cdx:" + prompt, workspace=workspace)

    monkeypatch.setattr(swarm, "_run_text_worker", fake_text)
    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)

    results = swarm.swarm_agents(
        [
            {"backend": "antigravity", "prompt": "p0"},
            {"backend": "codex", "prompt": "p1", "sandbox": "workspace-write", "model": "m"},
        ],
        max_concurrency=2,
        timeout_s=5,
        watch=False,
    )
    results.sort(key=lambda r: r.index)
    assert results[0].backend == "antigravity" and results[0].answer == "agy:p0"
    assert results[1].backend == "codex" and results[1].answer == "cdx:p1"
    cdx = next(c for c in calls if c[0] == "codex")
    assert cdx[3] == "workspace-write" and cdx[4] == "m"


def test_swarm_agents_empty_returns_empty():
    assert swarm.swarm_agents([]) == []


def test_format_agent_results_tags_backend():
    results = [
        swarm.WorkerResult(
            0, True, answer="ok", elapsed=1.0, workspace="C:\\x\\repo", backend="codex"
        ),
        swarm.WorkerResult(1, False, error="boom", elapsed=0.0, backend="antigravity"),
    ]
    out = swarm.format_agent_results(results)
    assert "1/2 succeeded" in out
    assert "[worker 0 · codex]" in out and "[worker 1 · antigravity]" in out
    assert "ok" in out and "boom" in out


def test_watch_init_stores_backends():
    swarm_watch.init(
        ["p0", "p1"], ["r0", "r1"], 1.0, ["p0", "p1"], 180, backends=["antigravity", "codex"]
    )
    snap = swarm_watch._snapshot()
    assert snap["workers"][0]["backend"] == "antigravity"
    assert snap["workers"][1]["backend"] == "codex"
