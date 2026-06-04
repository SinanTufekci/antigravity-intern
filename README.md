<div align="center">

# Claude Code × Antigravity CLI — MCP Bridge

**Use Google's [Antigravity](https://antigravity.google/) (Gemini 3.5 Flash) as a sub-agent inside [Claude Code](https://claude.com/claude-code) — on the AI Pro quota you already pay for.**

[![GitHub release](https://img.shields.io/github/v/release/SinanTufekci/Claude-Code-Antigravity-CLI-MCP-Server?color=2ea44f)](https://github.com/SinanTufekci/Claude-Code-Antigravity-CLI-MCP-Server/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP server](https://img.shields.io/badge/MCP-server-7c3aed)](https://modelcontextprotocol.io/)
[![agy 1.0.5 verified](https://img.shields.io/badge/agy-1.0.5%20verified-2ea44f)](https://antigravity.google/)
[![platform](https://img.shields.io/badge/platform-Windows%20·%20macOS%20·%20Linux-lightgrey)](#requirements)

</div>

---

`agy`, Google's Antigravity CLI, ships a headless print mode (`agy -p`) that's **broken**: it
authenticates, talks to the model, gets the answer back… and then never prints it. This bridge
runs `agy -p` anyway, reads the answer straight out of agy's *own* transcript files, and hands it
to Claude Code as two clean MCP tools. Delegate cheap tool-calling work to Gemini without leaving
your terminal.

> [!WARNING]
> **This runs unsandboxed code with your privileges.** `agy -p` auto-executes its tools
> (read/write files, run shell commands, reach the network) with **no approval gate and no
> opt-out** — we verified there is no agy flag that changes this. The `workspace` argument is a
> *starting context*, **not** a security boundary. Only use it with **trusted prompts on trusted
> content**; for real isolation, run the bridge inside a container or VM. **[Full details →](#security)**

## Why you'd want this

| | |
|---|---|
| 🧠 **Second opinion** | Ask a different model family mid-task without switching tools. |
| 💸 **Cheap delegation** | Burn Antigravity AI Pro quota on grunt work instead of Claude tokens. |
| 📁 **Cross-repo reads** | Point it at another project directory and let Gemini read/answer there. |
| 🔌 **Zero new auth** | Piggybacks the login you already did in the Antigravity IDE — no keys to manage. |

## How it works

```mermaid
flowchart LR
    A([Claude Code]) -- "MCP tool call" --> B["agy bridge<br/>(server.py)"]
    B -- "agy -p prompt" --> C[Antigravity CLI]
    C -- "Gemini 3.5 Flash (High)" --> M((model))
    M -- "answer" --> C
    C -. "writes (but never prints)" .-> T[("transcript.jsonl")]
    B -- "reads final PLANNER_RESPONSE" --> T
    B -- "plain text" --> A
```

`agy -p` persists its real answer — the one it forgets to print — to:

```
~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl
```

The bridge runs agy, locates the conversation via `cache/last_conversations.json` (falling back to
the newest `brain/` directory touched since launch), streams the transcript, and returns the final
`source=MODEL, status=DONE, type=PLANNER_RESPONSE` entry — the answer, minus the intermediate
tool-calling steps. `agy_continue` pins the workspace's **exact** conversation id via
`--conversation`, so it never resumes the wrong thread.

## Set up in 60 seconds

```bash
git clone https://github.com/SinanTufekci/Claude-Code-Antigravity-CLI-MCP-Server.git
cd Claude-Code-Antigravity-CLI-MCP-Server
pip install fastmcp
python test_smoke.py        # 2 real round-trips through agy — should print two PASS lines
```

> [!NOTE]
> The smoke test costs a tiny bit of AI Pro quota and takes ~30–60 s. You must have logged into
> Antigravity **once** (via the IDE or `agy -i`) so agy has a credential to reuse.

Then register the server with Claude Code — add this under `mcpServers` in `~/.claude.json`,
using the absolute path to `server.py`:

<table>
<tr><th>Windows</th><th>macOS / Linux</th></tr>
<tr><td>

```json
"agy": {
  "command": "python",
  "args": ["C:\\path\\to\\server.py"]
}
```

</td><td>

```json
"agy": {
  "command": "python3",
  "args": ["/path/to/server.py"]
}
```

</td></tr>
</table>

Restart Claude Code. Three tools appear: **`mcp__agy__agy_ask`**, **`mcp__agy__agy_continue`**, and **`mcp__agy__agy_status`**.

> *"Use agy_ask to summarize the README of this repo in three bullets."* → Claude routes the prompt
> through the bridge, agy reads the file under the workspace root, and the answer comes back as a
> plain string.

## Tools

| Tool | Purpose |
|---|---|
| `agy_ask(prompt, workspace?, timeout_s?=180)` | Start a **new** Antigravity conversation. |
| `agy_continue(prompt, workspace?, timeout_s?=180)` | Continue the conversation **rooted at `workspace`** (pinned by id). |
| `agy_status()` | Offline setup diagnostics (agy version/compat, state dirs, newest transcript readable). Spends no quota. |

`workspace` defaults to the MCP server's current working directory. Point it at a real project dir
for context-aware answers — agy gives the model access to files under that root.

## Model & auth

- **Model:** effectively **Gemini 3.5 Flash (High)** — whatever the `"model"` field in agy's
  `settings.json` is set to. agy 1.0.5 added a `--model` flag (and a `models` subcommand) that *is*
  wired into print mode, but **switching to a different model in `-p` hangs the call** (verified on
  1.0.5: passing the already-active label returns in seconds, any other label hangs >60 s). So the
  bridge stays single-model; change it via agy's `settings.json` if you need a different one. Flash
  High is speed-optimized for tool-calling, so this fits best as a *fast sub-agent for cheap work*,
  not a heavy reasoning partner.
- **Auth:** piggybacks whatever credential store `agy` uses on your OS (Windows Credential Manager,
  macOS Keychain, libsecret on Linux — the bridge never touches it directly). Log in once; every
  call after that silent-auths on the **same AI Pro quota** you already pay for.

<a id="security"></a>

## ⚠️ Security

`agy -p` runs the model as an **autonomous agent that auto-executes its own tools** — reading and
writing files, running shell commands, and reaching the network — with **no approval gate and no
opt-out**. This isn't a choice the bridge makes; it's how agy's print mode works. Re-verified
empirically on **agy 1.0.5 / Windows**:

- Print mode runs out-of-workspace file writes and live network fetches **even without**
  `--dangerously-skip-permissions` — that flag is a **no-op** for `-p`. There is **no** agy flag
  that disables tool execution in print mode.
- agy 1.0.5 integrated a permission system (its logs show `toolPermission=request-review`), but it
  **still does not gate print-mode execution** — a fresh `-p` run created a file outside the
  workspace with no prompt.
- `--sandbox` does **not** constrain filesystem writes or network egress on Windows, so it buys no
  real protection here.

**What that means for you:**

- The `workspace` argument is only a *starting context*, **not a security boundary** — the agent
  can and does act outside it.
- Every call effectively runs **arbitrary code with your user privileges**.
- Only invoke this with **trusted prompts on trusted content**. Untrusted input here is the classic
  prompt-injection *lethal trifecta*: private-data access + code execution + network egress.
- For real isolation, run the **whole bridge inside a container or VM**.

The bridge itself does only cross-platform filesystem reads under `~/.gemini/antigravity-cli/` — no
private APIs, no token theft. The risk above is entirely in what the agy sub-agent is allowed to do.

## FAQ

<details>
<summary><b>Is this against Google's Terms of Service?</b></summary>

It runs the **official `agy` CLI under your own AI Pro session** — no private APIs, no token theft,
no quota abuse. It just bridges what the CLI already does. That said, your AI Pro / Antigravity ToS
apply, and you're responsible for staying within them.
</details>

<details>
<summary><b>Will it break when agy updates?</b></summary>

Possibly — it reads agy's **internal, undocumented** state files, so a release can change paths or
schemas and break it silently. Re-verified working on **1.0.5** (transcript schema and `-p` JSONL
output unchanged; live smoke test passes). The known future risk is agy's **SQLite (`.db`)
conversation format** (added in 1.0.4, slated to become the default): agy 1.0.5 already
**dual-writes** every conversation to `~/.gemini/antigravity-cli/conversations/<id>.db` alongside
the JSONL transcript, so once it stops writing JSONL the reader needs a SQLite path. Pin a
known-good `agy` version if you depend on this.
</details>

<details>
<summary><b>Why only Gemini 3.5 Flash?</b></summary>

agy 1.0.5 added a `--model` flag, but switching to a different model in `-p` **hangs** (print mode
waits on a step it never gets headless), so in practice you get whatever model agy's `settings.json`
selects — Gemini 3.5 Flash (High) by default. The bridge doesn't expose a model knob because it
would hang on any real switch.
</details>

<details>
<summary><b>Does it cost extra money?</b></summary>

No. It uses the same **AI Pro quota** you already pay for. The smoke test spends a negligible
amount.
</details>

<details>
<summary><b>Does it stream responses?</b></summary>

No. `agy -p` is request/response only, so the bridge is too. Each call typically takes 10–30 s.
</details>

<details>
<summary><b>Can I run several calls at once?</b></summary>

They're **serialized** inside the server. agy rewrites `last_conversations.json` on every call, so
concurrent runs would race and could return the wrong conversation. A `threading.Lock` makes extra
requests queue rather than race — plan latency accordingly under load.
</details>

## Status & caveats

- ✅ **Verified on agy 1.0.5** — base dir, `last_conversations.json`, the
  `brain/.../transcript.jsonl` path, the transcript schema, and the `-p`/`-c`/`--print-timeout`
  flags are all unchanged; a live smoke test passes both round-trips. The 1.0.5 `-p` metadata fix
  also means agy no longer litters the workspace dir.
- ⏳ **SQLite migration is the real risk** — agy 1.0.5 already dual-writes a `.db` per conversation;
  see the [FAQ](#faq). `_read_response` raises a clear, SQLite-aware error if the JSONL transcript
  ever disappears.
- 🐛 **Stdout bug** — `-p` still doesn't print the answer on 1.0.5. If a future release fixes
  stdout, this workaround becomes redundant but harmless.
- 🔒 **No real sandbox** — agy 1.0.5's new permission system still doesn't gate `-p`; see
  [Security](#security).

## Requirements

- Python 3.10+
- [`agy`](https://antigravity.google/) 1.0.0 or newer on `PATH` (state-file layout re-verified on **1.0.5**)
- An active Antigravity / AI Pro session

The bridge uses only cross-platform Python (`Path.home()`, `subprocess`) and reads paths under
`~/.gemini/antigravity-cli/`, which `agy` writes the same way on every OS. **Developed and verified
on Windows; macOS and Linux should work unmodified provided `agy -i` runs there.** If you test it on
those platforms, please open an issue / PR to confirm.

## Development

```bash
pip install -e ".[dev]"      # fastmcp + pytest + ruff
pytest test_server.py        # offline unit tests — no agy, no quota
ruff check . && ruff format --check .
```

`test_server.py` covers the pure parsing/version logic with temp fixtures (no agy needed);
`test_smoke.py` is the live end-to-end check that spends a little quota. Set **`AGY_BRIDGE_DEBUG=1`**
to log per-call diagnostics (resolved conversation id, agy exit code, elapsed) to stderr — and on
startup the server warns if your installed agy is newer than the version it was verified against.

## Contributing

Personal project, **best-effort maintenance** — issues and PRs welcome, but no uptime/compat
promises. If `agy -p` ever starts printing to stdout correctly, this whole repo becomes a fun
historical artefact.

## License

[MIT](LICENSE). Do whatever you want with it.
