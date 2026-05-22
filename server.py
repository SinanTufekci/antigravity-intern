"""Antigravity CLI (agy) bridge — fastmcp server.

Exposes Antigravity CLI as MCP tools so Claude Code (or any MCP host) can
use it as a sub-agent. Solves the headless print-mode bug in agy 1.0.x
(verified still present in 1.0.1) by running `agy -p` and reading the
response from agy's own transcript files instead of relying on stdout.

Auth: piggybacks on Antigravity's existing Windows Credential Manager entry
(target `gemini:antigravity`). User must have logged in interactively at least
once via the Antigravity IDE or `agy -i`. Uses the same AI Pro quota.

Model: agy print mode is hardcoded to Gemini 3.5 Flash (High). We
verified no env var (CASCADE_DEFAULT_MODEL_OVERRIDE, AGY_MODEL, etc.) or
settings.json field (model/modelId/selectedModel/...) overrides this — the
print-mode default is baked in. Switching models headlessly would require
talking to agy's gRPC language server directly. Out of scope for this bridge.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("agy")

AGY_DATA = Path.home() / ".gemini" / "antigravity-cli"
LAST_CONVERSATIONS = AGY_DATA / "cache" / "last_conversations.json"
BRAIN_DIR = AGY_DATA / "brain"


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
        raise RuntimeError(f"Transcript not found: {transcript}")

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


def _run_agy(prompt: str, workspace: str, continue_conv: bool, timeout_s: int) -> str:
    args = ["agy", "--print-timeout", f"{timeout_s}s"]
    if continue_conv:
        args.append("-c")
    args.extend(["-p", prompt])

    start = time.time()
    proc = subprocess.run(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_s + 30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agy exited {proc.returncode}\n"
            f"stderr: {proc.stderr[-1000:]}\n"
            f"stdout: {proc.stdout[-500:]}"
        )

    time.sleep(0.3)  # let filesystem settle

    conv_id = _read_last_conv_id(workspace) or _find_newest_conv_after(start)
    if conv_id is None:
        raise RuntimeError(
            f"No conversation found after agy run (workspace={workspace}). "
            f"Check {LAST_CONVERSATIONS} and {BRAIN_DIR}."
        )
    return _read_response(conv_id)


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
def agy_continue(
    prompt: str, workspace: Optional[str] = None, timeout_s: int = 180
) -> str:
    """Continue the most recent Antigravity conversation in this workspace.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
    """
    ws = _normalize_workspace(workspace)
    return _run_agy(prompt, ws, continue_conv=True, timeout_s=timeout_s)


if __name__ == "__main__":
    mcp.run()
