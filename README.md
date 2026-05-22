# Claude Code × Antigravity CLI — MCP Bridge

An MCP server that lets [Claude Code](https://claude.com/claude-code) (or any
MCP-compatible host) call Google's **Antigravity CLI** (`agy`) as a sub-agent,
backed by your existing AI Pro quota.

Use it when you want Claude to delegate a fast tool-calling task to Gemini
3.5 Flash (High) without leaving your terminal — for second opinions, quick
file reads inside another workspace, or burning Antigravity quota instead of
Claude tokens for cheap work.

> **Heads up — read before you depend on this:**
> - This is a workaround that reads `agy`'s **internal, undocumented** state
>   files (`brain/.../transcript.jsonl`, `cache/last_conversations.json`). A
>   future `agy` release can change those paths or schemas and break the
>   bridge silently. Expect bitrot; pin to a known-good `agy` version if
>   you're using this for anything real.
> - It runs the **official `agy` CLI under your own AI Pro session** — no
>   private APIs, no token theft, no quota abuse. It just bridges what the
>   CLI already does. Still, your AI Pro / Antigravity ToS apply; you are
>   responsible for using it within them.
> - Personal project, **best-effort maintenance**. Issues and PRs welcome,
>   but I make no uptime/compat promises. If `agy -p` ever starts printing
>   to stdout correctly, this whole repo becomes a fun historical artefact.

## The problem this solves

`agy 1.0.x` (verified through **1.0.1**) ships a `--print` / `-p` flag for
non-interactive use, but the flag is broken in non-TTY contexts: the CLI
authenticates, sends the message, gets a response back from the model... and
then never writes that response to stdout. Exit code is 0; pipe is empty.

The response *is*, however, persisted to disk under:

```
~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl
```

This server runs `agy -p` under the hood, locates the conversation in agy's
own state files, parses the transcript, and returns the model's final
`PLANNER_RESPONSE` as plain text. From Claude's perspective it's just two
clean MCP tools.

## Tools exposed

| Tool | Purpose |
| --- | --- |
| `agy_ask(prompt, workspace?, timeout_s?=180)` | Start a **new** Antigravity conversation. |
| `agy_continue(prompt, workspace?, timeout_s?=180)` | Continue the most recent conversation rooted at `workspace`. |

`workspace` defaults to the current working directory of the MCP server.
Point it at a real project directory if you want context-aware answers — agy
gives the model access to files under that root.

## Model

Always **Gemini 3.5 Flash (High)**. `agy -p` hardcodes the print-mode model;
neither env vars (`CASCADE_DEFAULT_MODEL_OVERRIDE`, `AGY_MODEL`, `GEMINI_MODEL`,
…) nor `settings.json` fields (`model`, `modelId`, `selectedModel`, …) override
it. Switching to Pro/Sonnet/etc. headlessly would require speaking to agy's
gRPC language server directly — out of scope for this bridge.

Flash High is the speed-optimized tool-calling model, so this fits best as a
"fast sub-agent for cheap work" rather than as a heavy reasoning partner.

## Auth

Piggybacks on whatever credential store `agy` itself uses on your OS
(Windows Credential Manager on Windows, Keychain on macOS, libsecret /
similar on Linux — the bridge never touches it directly). Log in **once**
interactively, either through the Antigravity IDE or with:

```
agy -i
```

After that this server silent-auths on every call, using the same AI Pro
quota you already pay for. No keys to copy, no tokens to manage.

## Install

```
git clone https://github.com/SinanTufekci/Claude-Code-Antigravity-CLI-MCP-Server.git
cd Claude-Code-Antigravity-CLI-MCP-Server
pip install fastmcp
python test_smoke.py   # end-to-end sanity check; should print two PASS lines
```

The smoke test makes two real round-trips through `agy`, so it costs a tiny
bit of your AI Pro quota and takes ~30–60 seconds.

## Register with Claude Code

Add an entry under `mcpServers` in `~/.claude.json`. Use the absolute path
to `server.py` on your machine.

**Windows:**

```json
"agy": {
  "command": "python",
  "args": ["C:\\path\\to\\Claude-Code-Antigravity-CLI-MCP-Server\\server.py"]
}
```

**macOS / Linux:**

```json
"agy": {
  "command": "python3",
  "args": ["/path/to/Claude-Code-Antigravity-CLI-MCP-Server/server.py"]
}
```

Restart Claude Code. Two new tools will appear: `mcp__agy__agy_ask` and
`mcp__agy__agy_continue`.

## Quick example

From inside a Claude Code session:

> *"Use agy_ask to summarize the README of this repo in three bullets."*

Claude will route the prompt through the MCP server, agy will read the file
under the workspace root, and the response comes back as a plain string.

## Requirements

- Python 3.10+
- [`agy`](https://antigravity.google/) 1.0.0 or newer on `PATH`
- An active Antigravity / AI Pro session

The bridge itself uses only cross-platform Python (`Path.home()`,
`subprocess`) and reads paths under `~/.gemini/antigravity-cli/` — which
`agy` writes the same way on every OS. **Developed and verified on
Windows; macOS and Linux should work without modification provided
`agy -i` runs there successfully.** If you test it on those platforms,
please open an issue / PR to confirm.

## How it works (the workaround in one paragraph)

`agy -p "<prompt>"` is invoked with `--print-timeout` and a working directory.
When it exits, the server reads `~/.gemini/antigravity-cli/cache/last_conversations.json`
to map `workspace → conversation id`, falling back to "newest dir under
`brain/` modified since launch" if the cache hasn't been updated yet. It
then streams the conversation's `transcript.jsonl`, collects every entry
matching `source=MODEL, status=DONE, type=PLANNER_RESPONSE`, and returns the
last one — that's the final answer (earlier ones are intermediate
tool-calling steps).

## Status & caveats

- **Stdout bug**: filed mentally as "still broken in 1.0.1". If a future
  release fixes it, the workaround becomes redundant but harmless — the
  server keeps working.
- **No streaming**: the bridge is request/response only. `agy -p` doesn't
  stream, so neither does this.
- **Calls are serialized inside the server**: agy rewrites
  `last_conversations.json` on every invocation, so concurrent runs would
  race and could return the wrong conversation's transcript. The server
  guards `_run_agy` with a `threading.Lock`, meaning additional requests
  simply queue up rather than racing or erroring. Each `agy` call typically
  takes 10–30 s, so plan latency accordingly under load.

## License

MIT. Do whatever you want with it.
