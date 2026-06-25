<div align="center">

# Claude Code × Antigravity + Codex — MCP Bridge

<img src="assets/bridge-animation.svg" width="100%" alt="Claude Code bridging Google Antigravity and OpenAI Codex" />

**Drive two external coding CLIs — Google's [Antigravity](https://antigravity.google/) (Gemini 3.5 Flash) and [OpenAI Codex](https://developers.openai.com/codex/) — as sub-agents inside [Claude Code](https://claude.com/claude-code). Text answers, image generation, real repo work, and parallel swarms, on quota you already pay for.**

[![CI](https://github.com/SinanTufekci/agent-intern/actions/workflows/ci.yml/badge.svg)](https://github.com/SinanTufekci/agent-intern/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-intern?logo=pypi&logoColor=white&color=2ea44f)](https://pypi.org/project/agent-intern/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/agent-intern?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/agent-intern)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP server](https://img.shields.io/badge/MCP-server-7c3aed)](https://modelcontextprotocol.io/)
[![Glama](https://glama.ai/mcp/servers/SinanTufekci/agent-intern/badges/score.svg)](https://glama.ai/mcp/servers/SinanTufekci/agent-intern)
[![agy 1.0.12 verified](https://img.shields.io/badge/agy-1.0.12%20verified-2ea44f)](https://antigravity.google/)
[![codex 0.141.0 verified](https://img.shields.io/badge/codex--cli-0.141.0%20verified-2ea44f)](https://developers.openai.com/codex/)
[![platform](https://img.shields.io/badge/platform-Windows%20·%20macOS%20·%20Linux-lightgrey)](#requirements)
[![Sponsor](https://img.shields.io/github/sponsors/SinanTufekci?logo=githubsponsors&label=Sponsor&color=ea4aaa)](https://github.com/sponsors/SinanTufekci)

</div>

---

One MCP server, **two backends**. It exposes Google Antigravity and OpenAI Codex to Claude Code as
clean MCP tools so you can delegate work to a different model family mid-task — without leaving your
terminal, and on the subscriptions you already have. Each backend is independent: install one or
both.

- **🛰️ Antigravity (`agy`, Gemini 3.5 Flash High).** Fast, cheap tool-calling — and the **only**
  backend with an image model. Its headless print mode (`agy -p`) is **broken**: it authenticates,
  talks to the model, gets the answer… then writes it to the *controlling terminal* instead of its
  stdout, so anything capturing stdout gets nothing (and, under a TUI, agy's text leaks into the
  host's prompt). The bridge runs `agy -p` anyway, **detaches it from the terminal** so it can't
  leak, and reads the answer straight out of agy's *own* transcript files.
- **🤖 Codex (`codex exec`, OpenAI).** The well-behaved sibling and a stronger reasoner for real
  code/repo work. It writes its final message straight to a file the bridge asks for (no
  transcript-scraping), supports **model selection**, and has a **real sandbox**.

Both share the same niceties: a `*_continue` to resume a thread, a [live "watch" window](#watch-mode)
to see the agent work, a unified [`agent_swarm`](#swarm) that runs many tasks in parallel **across
both backends at once**, and `*_status` diagnostics that spend no quota.

> [!WARNING]
> **This runs unsandboxed code with your privileges.** `agy -p` auto-executes its tools
> (read/write files, run shell commands, reach the network) with **no usable approval gate** — its
> `--sandbox` blocks only *shell commands*, leaving file writes and network egress wide open.
> `codex exec` also runs autonomously, but its `sandbox` flag (default `read-only`) **is** a real
> boundary. In both cases the `workspace` argument is a *starting context*, **not** a security
> boundary. Only use these with **trusted prompts on trusted content**; for real isolation, run the
> bridge inside a container or VM. **[Full details →](#security)**

## Why you'd want this

| | |
|---|---|
| 🧠 **Second opinion** | Ask a different model family — Gemini *or* GPT — mid-task without switching tools. |
| 🎨 **Image generation** | Have Gemini draw an image and get the saved file back — no extra API key or image tool. |
| 🛠️ **Real coding sub-agent** | Hand a focused repo task to Codex with a real `workspace-write` sandbox. |
| 💸 **Cheap delegation** | Burn Antigravity / Codex quota on grunt work instead of Claude tokens. |
| 🐝 **Parallel fan-out** | Run N tasks at once, mixing Gemini and Codex workers in a single swarm. |
| 📁 **Cross-repo reads** | Point a worker at another project directory and let it read/answer there. |
| 🔌 **Zero new auth** | Piggybacks the logins you already did — no keys for the bridge to manage. |

## The two backends at a glance

The bridge normalizes both CLIs into the same shape, but they differ where it matters. Pick per task:

| | 🛰️ **Antigravity** (`agy`) | 🤖 **Codex** (`codex exec`) |
|---|---|---|
| **Model** | Gemini 3.5 Flash (High) — fixed (see [Model & auth](#model--auth)) | Selectable via `model` (codex's `-m`) |
| **Best at** | Fast, cheap tool-calling; quick answers | Heavier reasoning; real code/repo work |
| **Image generation** | ✅ `antigravity_image` (+ `antigravity_image_swarm`) | ❌ no image model |
| **Sandbox** | ❌ no real boundary (`--sandbox` blocks only shell) | ✅ real: `read-only` / `workspace-write` / `danger-full-access` |
| **How the answer is read** | Scraped from agy's `transcript.jsonl` (stdout is broken) | Written to a file via `-o/--output-last-message` |
| **Continue mechanism** | Pins the workspace's conversation id (`--conversation`) | Resumes the session id (`codex exec resume <id>`) |
| **Auth** | OS credential store (AI Pro session) | `codex login` (ChatGPT account or API key) |
| **In a swarm** | Runs with an isolated `HOME` to avoid state races | Fresh one-shot — needs no isolation |

## How it works

Both backends run **headless** and one-shot per call; the bridge's job is to get a clean answer out
of each and hand it to Claude Code as a plain string.

```mermaid
flowchart LR
    A([Claude Code]) -- "MCP tool call" --> B["bridge<br/>(server.py)"]
    B -- "antigravity_*" --> C[agy -p]
    B -- "codex_*" --> D[codex exec]
    C -- "Gemini 3.5 Flash" --> M1((model))
    D -- "GPT / Codex" --> M2((model))
    M1 -. "stdout stays empty" .-> T[("transcript.jsonl")]
    C -.-> T
    B -- "reads final PLANNER_RESPONSE" --> T
    M2 --> O[("output-last-message file")]
    D --> O
    B -- "reads last message" --> O
    B -- "plain text" --> A
```

**Antigravity.** `agy -p` persists its real answer — the one it never sends to stdout — to:

```
~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl
```

The bridge runs agy, locates the conversation via `cache/last_conversations.json` (falling back to
the newest `brain/` directory touched since launch), streams the transcript, and returns the final
`source=MODEL, status=DONE, type=PLANNER_RESPONSE` entry — the answer, minus the intermediate
tool-calling steps. `antigravity_continue` pins the workspace's **exact** conversation id via
`--conversation`, so it never resumes the wrong thread. If agy wrote no JSONL (true for `--sandbox`
runs), the bridge falls back to reading the SQLite `.db` agy dual-writes for every conversation.

**Codex.** `codex exec` is well-behaved: the bridge passes `-o/--output-last-message <file>` and
codex writes its final message straight there — no scraping. Continue works by capturing the session
id from codex's own rollout files (`~/.codex/sessions/.../rollout-*.jsonl`) and resuming with
`codex exec resume <id>`, falling back to the newest on-disk session for that cwd after a server
restart.

## Set up in 60 seconds

**Prerequisites — install whichever backend(s) you want, and sign in once each:**

- **Antigravity:** install `agy` and sign in to Antigravity once (via the IDE or `agy -i`).
- **Codex:** install `codex` and run `codex login` once (ChatGPT account or API key).

You don't need both — the tools for a missing CLI simply report "not found" via their `*_status`
tool.

### Recommended — no clone, you control updates

With [`uv`](https://docs.astral.sh/uv/) installed, register the bridge straight from
[PyPI](https://pypi.org/project/agent-intern/) under `mcpServers` in `~/.claude.json` — no
path to hardcode, no `git pull` to remember:

```json
"agent-intern": {
  "command": "uvx",
  "args": ["agent-intern"]
}
```

uvx pins to the version it first caches and does **not** auto-upgrade, so you never run an update you
didn't choose — important, since the bridge runs [unsandboxed code](#security): a surprise (or
compromised) release can't execute until you opt in. When the startup check warns that a newer
release is out, upgrade deliberately and restart Claude Code:

```bash
uvx agent-intern@latest      # fetch + run the newest release (refreshes uv's cache)
```

> [!TIP]
> Prefer hands-off auto-updates? Put `"args": ["agent-intern@latest"]` in the config instead —
> every launch runs the newest release. Convenient, but it pulls new code without asking each time.

### From source

Clone it instead if you want to hack on the bridge or pin a local copy:

```bash
git clone https://github.com/SinanTufekci/agent-intern.git
cd agent-intern
pip install fastmcp
python test_smoke.py        # 4 real round-trips (ask, continue, image, swarm) — prints four PASS lines
```

> [!NOTE]
> The smoke test costs a tiny bit of quota and takes ~30–60 s. It exercises the Antigravity path.

Then point Claude Code at the absolute path to `server.py` under `mcpServers` in `~/.claude.json`:

<table>
<tr><th>Windows</th><th>macOS / Linux</th></tr>
<tr><td>

```json
"agent-intern": {
  "command": "python",
  "args": ["C:\\path\\to\\server.py"]
}
```

</td><td>

```json
"agent-intern": {
  "command": "python3",
  "args": ["/path/to/server.py"]
}
```

</td></tr>
</table>

Restart Claude Code. **Nine tools** appear, each prefixed `mcp__agent-intern__`:

- **Antigravity (5):** `antigravity_ask`, `antigravity_continue`, `antigravity_image`,
  `antigravity_image_swarm`, `antigravity_status`
- **Codex (3):** `codex_ask`, `codex_continue`, `codex_status`
- **Shared (1):** `agent_swarm` — fans a list of tasks out across **both** backends in one run

The single-prompt tools — Antigravity **and** Codex — take a **`watch=true`** flag for the live
browser view ([Watch mode](#watch-mode)).

> *"Use antigravity_ask to summarize the README of this repo in three bullets."* → Claude routes the
> prompt through the bridge, agy reads the file under the workspace root, and the answer comes back
> as a plain string. Swap in `codex_ask` to have GPT do the same with a `read-only` sandbox.

## Tools

### 🛰️ Antigravity

| Tool | Purpose |
|---|---|
| `antigravity_ask(prompt, workspace?, timeout_s?=180, watch?=false)` | Start a **new** Antigravity conversation. `watch=true` opens the live browser view ([Watch mode](#watch-mode)). |
| `antigravity_continue(prompt, workspace?, timeout_s?=180, watch?=false)` | Continue the conversation **rooted at `workspace`** (pinned by id). `watch=true` opens the live view. |
| `antigravity_image(prompt, output_path?, workspace?, timeout_s?=240, watch?=false)` | Generate an image; saves the file (extension corrected to the real bytes) and returns its path + format/size. `watch=true` streams progress and **shows the image** inline. |
| `antigravity_image_swarm(prompts, output_paths?, workspaces?, max_concurrency?=4, timeout_s?=240, watch?=false)` | Generate **several images in parallel** (one worker per prompt). |
| `antigravity_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, plus agy version/compat, state dirs, and newest-transcript readability. Spends no quota. |

### 🤖 Codex

| Tool | Purpose |
|---|---|
| `codex_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Codex session. `sandbox` is a **real** boundary (see [Codex bridge](#codex-bridge)); `model` selects the model (`-m`). `watch=true` opens the live view, streaming codex's steps from its `--json` event stream. |
| `codex_continue(prompt, workspace?, timeout_s?=180, watch?=false)` | Continue the Codex session **rooted at `workspace`** — resumes the exact session id, falling back to the newest on-disk session for that cwd after a server restart. The resumed session keeps its original sandbox and model. `watch=true` opens the live view. |
| `codex_status()` | Setup diagnostics: codex version, login status (`codex login status`), sessions dir. Spends no quota. |

### 🐝 Shared

| Tool | Purpose |
|---|---|
| `agent_swarm(tasks, max_concurrency?=4, timeout_s?=180, watch?=false)` | Run **several tasks in parallel across both backends** — each task names its `backend` (`antigravity` or `codex`) plus a `prompt` (and, for Codex, `sandbox`/`model`). Every answer comes back in one block; `watch=true` opens the live dashboard ([Swarm](#swarm)). |

`workspace` defaults to the MCP server's current working directory. Point it at a real project dir
for context-aware answers — both backends give the model access to files under that root (Codex
honoring its `sandbox`).

`antigravity_image` forces agy to save to an explicit absolute path — without one, agy
falls back to its own scratch dir (`~/.gemini/antigravity-cli/scratch/`). It then
corrects the file extension to match the real bytes: agy's image model picks the
format itself (JPEG for photo-like images, PNG for flat graphics), so a requested
`out.png` may come back as `out.jpg`. The returned path always reflects the true
format.

<a id="codex-bridge"></a>

## 🤖 Codex bridge — the well-behaved sibling

Where `agy -p` is broken (it never writes to stdout, so the bridge scrapes transcript files),
`codex exec` is clean: it writes its final message to a file the bridge asks for via
`-o/--output-last-message`, so the answer comes back without scraping. Three things make Codex worth
reaching for over Antigravity:

- **Real sandbox.** `sandbox` accepts `read-only` (default — reads and answers, writes nothing),
  `workspace-write` (may edit files under the workspace), or `danger-full-access` (no sandbox —
  avoid). Unlike agy's no-op `--sandbox`, codex's `-s` actually enforces this. `codex exec` has no
  interactive approval gate, so this flag **is** your safety boundary — opt into write access
  deliberately.
- **Model selection works.** `model` maps to codex's `-m`; agy hangs on a model switch in print
  mode, codex does not.
- **Stronger reasoning.** Codex is a coding agent, not an image model — there's no `codex_image`. Its
  strength is reasoning and real code/repo work; hand it the jobs that need a heavier model.

**Auth.** Uses your existing Codex login (ChatGPT account or API key). Run `codex login` once; check
with `codex_status`. No new keys for the bridge to manage.

> [!WARNING]
> `codex exec` runs the model as an **autonomous agent with no interactive approval gate**. The
> `sandbox` flag (default `read-only`) is the real boundary, but `workspace-write` /
> `danger-full-access` let it modify files — and a swarm runs N agents at once. Only use it with
> **trusted prompts on trusted content**.

<a id="watch-mode"></a>

## 👁️ Watch mode — Agent Intern (experimental)

Pass **`watch=true`** to **any single-prompt tool** — `antigravity_ask`, `antigravity_continue`,
`antigravity_image`, `codex_ask`, or `codex_continue` — to **watch the agent work live in a little
terminal-style browser window** called **Agent Intern**. The agent still runs headless; alongside it
the bridge serves a tiny page on `127.0.0.1` and opens it in a small, chromeless app window that
streams the agent's steps — its planner narration (▸), the **real commands** it runs (`$`), and
completions (✓) — read live (from agy's transcript, or codex's `--json` event stream), with the
final answer rendered as Markdown (and, for `antigravity_image` with `watch=true`, the generated
image shown inline).

<div align="center">
<table>
<tr>
<td width="50%" align="center"><b>text ask / continue (agy <i>or</i> codex)</b></td>
<td width="50%" align="center"><b><code>antigravity_image</code> — image inline</b></td>
</tr>
<tr>
<td><img src="assets/watch-ask.gif" width="100%" alt="Agent Intern watch window streaming an agent's steps for a text ask — narration, the real commands it runs, completions — then rendering the final Markdown answer"></td>
<td><img src="assets/watch-image.gif" width="100%" alt="Agent Intern watch window streaming an image generation and rendering the finished image inline"></td>
</tr>
</table>
<sub>Real captures — the agent runs headless while the <b>Agent Intern</b> window live-streams its steps (▸ narration · <code>$</code> commands · ✓ completions), then shows the final answer or image.</sub>
</div>

- **Cross-platform & best-effort.** Prefers a Chromium browser (`--app` mode) for the
  windowed look; falls back to a normal browser window. If nothing can open, the run
  still completes and returns normally.
- **Window size.** Set **`AGY_WATCH_WINDOW_SIZE`** (e.g. `AGY_WATCH_WINDOW_SIZE=480,700`)
  to resize the window; default is `560,760`. Press **Enter / Esc** in the window to
  close it.
- **One window, reused.** Repeated watch calls **reuse the already-open window**
  instead of stacking a new one each time — the open page resets itself for the new
  run (the swarm dashboard rebuilds for the new fan-out). If you closed the window, the
  next run opens a fresh one. Set **`AGY_WATCH_ALWAYS_NEW=1`** to force a new window
  every time.
- **Progress, keyboard & copy.** Each panel shows a time progress bar (elapsed /
  timeout). The swarm dashboard adds an overall done/total bar and per-row time bars;
  use **↑/↓** to select a worker and **↵** to open its detail window. Answers render
  as Markdown with a **copy** button, and a "jump to latest" badge appears if you
  scroll up.
- **Coarse, not token-level.** Both backends flush their step stream in chunks, so you
  get a handful of live steps, not character streaming. The returned value is identical
  to the non-watch call. Nothing is sent anywhere but your own machine.

<a id="swarm"></a>

## 🐝 Swarm — run agents in parallel

`agent_swarm` fans a list of **tasks** out to workers that run **truly
concurrently** (capped at `max_concurrency`, default 4), then returns every
worker's result in one block. Each task names its own `backend`, so a **single
swarm can mix Antigravity (Gemini) and Codex** workers — hand the reasoning-heavy
jobs to Codex and the quick ones to Gemini, all at once. Good for independent
sub-tasks: summarise N files, ask the same question about N repos, fix N bugs.
(`antigravity_image_swarm` stays separate — it generates N images, and only agy
has an image model.)

```
agent_swarm(tasks=[
  {"backend": "antigravity", "prompt": "Summarise src/auth.py in 2 bullets."},
  {"backend": "antigravity", "prompt": "List the public functions in src/api.py."},
  {"backend": "codex", "prompt": "Find and fix the failing test in tests/",
   "sandbox": "workspace-write", "workspace": "./repo"},
])
```

<div align="center">
<img src="assets/watch-swarm.gif" width="62%" alt="Agent Swarm dashboard: workers running in parallel, each row showing its backend badge, repo, prompt, latest step and a per-worker time bar, while the overall done/total counter climbs">
<br>
<sub><code>agent_swarm(..., watch=true)</code> — one row per worker (with a backend badge); the done/total bar climbs as workers finish. Click a row (or <b>↑/↓</b> then <b>↵</b>) to pop that agent into its own window.</sub>
</div>

**How it stays correct under concurrency.** The single-agent agy tools serialize
through a lock because agy rewrites `last_conversations.json` on every call, so
concurrent runs sharing one state dir would race. The swarm sidesteps this: each
**agy** worker runs with its **own isolated `HOME`/`USERPROFILE`**, so agy's
`brain/`, `cache/`, and `last_conversations.json` never collide — no lock needed.
Auth still works because agy reads it from the **OS credential store**, not from
`~/.gemini` (verified on agy 1.0.9). **Codex** workers need no such isolation —
each is a fresh one-shot `codex exec` with its own `-o` output file. Each worker's
`cwd` is its real `workspace`, so file access is unchanged. Measured ~**2.8×
speedup at 3 agy workers** (the AI Pro backend does not serialize per-account);
higher `max_concurrency` trades quota/rate-limit pressure for wall-clock.

- **Per-task fields** — `backend` (`antigravity`/`codex`) and `prompt` are
  required; `workspace` defaults to the server cwd; `sandbox` and `model` apply to
  **Codex only** (ignored for Antigravity). Swarm workers are **one-shot** — there
  is no `*_continue` for a swarm worker's session.
- **Error isolation** — a worker that fails is reported in place; the others still
  return.
- **`watch=true`** — opens a thin live **Agent Swarm** dashboard (one row per
  worker, with a **backend badge**, repo, prompt, and latest step). **Click a row**
  to pop that agent into its own window streaming its full step log.

> [!WARNING]
> A swarm launches **N unsandboxed agents at once** — N× the prompt-injection
> "lethal trifecta" surface of a single call (see [Security](#security)). Only use
> it with **trusted prompts on trusted content**. Codex workers honor their
> `sandbox`; Antigravity workers have no real boundary.

## Model & auth

| | 🛰️ **Antigravity** | 🤖 **Codex** |
|---|---|---|
| **Model** | Effectively **Gemini 3.5 Flash (High)** — whatever the `"model"` field in agy's `settings.json` is set to. Switching model in `-p` **hangs** the call (verified on 1.0.5: the active label returns in seconds, any other hangs >60 s), so the bridge stays single-model. Flash High is speed-optimized for tool-calling — best as a *fast sub-agent for cheap work*. | **Selectable** via the `model` argument (codex's `-m`). codex does not hang on a switch, so model choice is a first-class knob. |
| **Auth** | Piggybacks whatever credential store `agy` uses on your OS (Windows Credential Manager, macOS Keychain, libsecret on Linux — the bridge never touches it directly). Log in once; every call silent-auths on the **same AI Pro quota** you already pay for. | Uses your existing **Codex login** — ChatGPT account or API key. Run `codex login` once; verify with `codex_status`. |

<a id="security"></a>

## ⚠️ Security

Both backends run the model as an **autonomous agent**. The difference is whether you get a real
boundary.

### Antigravity — no usable boundary

`agy -p` auto-executes its own tools — reading and writing files, running shell commands, reaching
the network — with **no approval gate and no opt-out**. This isn't a choice the bridge makes; it's
how agy's print mode works. Re-verified empirically on **agy 1.0.9 / Windows** (all three checks
below still hold):

- Print mode runs out-of-workspace file writes and live network fetches **even without**
  `--dangerously-skip-permissions` — that flag is a **no-op** for `-p`. There is **no** agy flag
  that disables tool execution in print mode.
- agy 1.0.5 integrated a permission system (its logs show `toolPermission=request-review`), but it
  **still does not gate print-mode execution** — a fresh `-p` run created a file outside the
  workspace with no prompt. agy 1.0.12 reshuffled how that permission config *merges* (per-project
  files under `~/.gemini/config/projects/` now take precedence over
  `~/.gemini/antigravity-cli/settings.json`), but that's a config-layer change — it adds no
  print-mode approval gate, and the bridge reads none of it.
- `--sandbox` is **not** a usable boundary. agy 1.0.6 fixed its propagation into `-p` (the 1.0.6/1.0.7
  changelog calls this "sandbox isolation correctly enforced") and it now **does** block terminal/
  shell command execution — but re-verified on 1.0.9 that it leaves the `write_to_file` tool and
  network **wide open**: under `--sandbox` the model still wrote a file *outside* its workspace. agy
  1.0.9 hardened the sandbox's *command* path (stricter exact-match command checks; `.git` added to
  its dangerous-paths list), but none of that closes the out-of-workspace `write_to_file` hole. On
  top of that, a `--sandbox` run whose blocked terminal command halts it writes **no JSONL
  transcript** (only the SQLite `.db`, re-confirmed on 1.0.9). The bridge can now read that `.db`,
  but still never passes `--sandbox` — it's no boundary, with file writes and network left open.

### Codex — a real sandbox you should use

`codex exec` also has **no interactive approval gate**, but its `sandbox` flag is a genuine boundary
that codex enforces:

- **`read-only`** (default) — reads and answers; writes nothing. Safe for untrusted *questions* on
  trusted content.
- **`workspace-write`** — may edit files under the workspace. Opt in deliberately, per task.
- **`danger-full-access`** — no sandbox at all. Avoid.

Because there's no approval prompt, the flag you pass **is** the safety decision — choose it per
call.

### What that means for you

- The `workspace` argument is only a *starting context*, **not a security boundary** — Antigravity
  can and does act outside it; Codex is bounded only by its `sandbox`.
- An Antigravity call effectively runs **arbitrary code with your user privileges**. A Codex call
  does too unless you keep it at `read-only`.
- Only invoke these with **trusted prompts on trusted content**. Untrusted input here is the classic
  prompt-injection *lethal trifecta*: private-data access + code execution + network egress.
- For real isolation, run the **whole bridge inside a container or VM**.

The bridge itself does only cross-platform filesystem reads under `~/.gemini/antigravity-cli/` and
`~/.codex/` — no private APIs, no token theft. The risk above is entirely in what the sub-agents are
allowed to do.

## FAQ

<details>
<summary><b>Is this against Google's / OpenAI's Terms of Service?</b></summary>

It runs the **official `agy` and `codex` CLIs under your own logins** — no private APIs, no token
theft, no quota abuse. It just bridges what the CLIs already do. That said, your AI Pro / Antigravity
and OpenAI / Codex ToS apply, and you're responsible for staying within them.
</details>

<details>
<summary><b>Do I need both CLIs?</b></summary>

No. Each backend is independent — install only the CLI(s) you want. The tools for a missing backend
report "not found" via their `*_status` tool (`antigravity_status` / `codex_status`) and never crash
the server.
</details>

<details>
<summary><b>When should I use Antigravity vs Codex?</b></summary>

Use **Antigravity** for fast, cheap tool-calling, quick answers, and **image generation** (it's the
only backend with an image model). Use **Codex** for heavier reasoning, real code/repo work, when you
need to **pick the model**, or when you want a **real `workspace-write` sandbox**. In a swarm you can
mix both. See [The two backends at a glance](#the-two-backends-at-a-glance).
</details>

<details>
<summary><b>Will it break when agy updates?</b></summary>

Possibly — it reads agy's **internal, undocumented** state files, so a release can change paths or
schemas and break it silently. Re-verified working on **1.0.12** (transcript schema and `-p` JSONL
output unchanged; live ask round-trip + `antigravity_status` diagnostics pass). The big looming change
is agy's **SQLite (`.db`) conversation format** (added in 1.0.4, slated to become the default): agy
1.0.12 still **dual-writes** every conversation to `~/.gemini/antigravity-cli/conversations/<id>.db`
alongside the JSONL transcript. The bridge is **ready for it** — `_read_response` reads the JSONL when
present and **falls back to the `.db`** (parsing the `steps` table's protobuf payload) when it isn't,
which already happens for `--sandbox` runs. Verified to match the JSONL answer across 100+ local
conversations. Still, the `.db` schema is undocumented and could change, so pin a known-good `agy`
version if you depend on this.
</details>

<details>
<summary><b>Why only Gemini 3.5 Flash for Antigravity?</b></summary>

agy 1.0.5 added a `--model` flag, but switching to a different model in `-p` **hangs** (print mode
waits on a step it never gets headless), so in practice you get whatever model agy's `settings.json`
selects — Gemini 3.5 Flash (High) by default. The Antigravity tools don't expose a model knob because
it would hang on any real switch. **Codex does not have this limitation** — its `model` argument
works.
</details>

<details>
<summary><b>Can it generate images?</b></summary>

**Yes — that's the `antigravity_image` tool**, on the Antigravity backend. agy's print mode generates
real images on your AI Pro quota; `antigravity_image` drives it, saves the file to a path you choose
(or a timestamped default in your workspace), fixes the extension to match the real bytes (agy picks
JPEG or PNG itself), and returns the path. Verified on **agy 1.0.9 / Windows**. Codex has no image
model — it's a coding agent.
</details>

<details>
<summary><b>Does it cost extra money?</b></summary>

No. It uses the **same quota you already pay for** — AI Pro for Antigravity, your Codex plan for
Codex. The smoke test spends a negligible amount.
</details>

<details>
<summary><b>Does it stream responses?</b></summary>

The final answer is request/response — both CLIs return it all at once, so the tools return when the
agent finishes (each call typically takes 10–30 s). If you want to *watch* the agent work as it goes,
pass **`watch=true`** to any single-prompt tool: it opens the **Agent Intern** browser window and
live-streams the agent's steps — see [Watch mode](#watch-mode). It's coarse (a handful of steps, not
token-by-token), and the returned value is identical to the non-watch call.
</details>

<details>
<summary><b>Can I run several calls at once?</b></summary>

The **single-agent** tools are **serialized** inside the server: agy rewrites `last_conversations.json`
on every call, so concurrent runs sharing one state dir would race and could return the wrong
conversation. A `threading.Lock` makes extra requests queue rather than race.

For real parallelism use **[`agent_swarm`](#swarm)** — each agy worker runs in its own isolated state
dir (and Codex workers need none), so they don't race and the lock isn't needed (~2.8× at 3 workers).
That's the supported way to run many calls at once, across either backend.
</details>

## Status & caveats

- ✅ **Verified on agy 1.0.12** — base dir, `last_conversations.json` (still keyed by workspace path),
  the `brain/.../transcript.jsonl` path, the transcript schema, and the `-p`/`-c`/`--print-timeout`
  flags are all unchanged; a live ask round-trip + `antigravity_status` diagnostics pass. 1.0.12's
  new `--project`/`--new-project` flags and per-project permission configs under
  `~/.gemini/config/projects/` don't touch the print-mode path — the bridge passes no `--project`
  and never reads agy's config.
- ✅ **Verified on codex-cli 0.141.0** — `codex exec`, `-o/--output-last-message`,
  `codex exec resume`, the `--json` event stream, and the `~/.codex/sessions/.../rollout-*.jsonl`
  layout the continue path reads are all in place; a live `codex_ask` round-trip + `codex_status`
  pass.
- 🖥️ **Console-detach** — agy `-p` writes its progress/answer to the *controlling terminal*,
  not stdout; under a TUI that text leaks into the host's prompt (seen on 1.0.9 before the fix). The
  bridge spawns agy detached from the terminal (`CREATE_NO_WINDOW` / a new POSIX session), so it
  can't leak; the answer is still read from the transcript.
- 💾 **SQLite migration — handled** — agy 1.0.12 still dual-writes a `.db` per conversation; when the
  JSONL transcript is absent (already true for `--sandbox` runs, and the announced future default)
  `_read_response` falls back to reading the `.db`, verified to match across 100+ conversations. See
  the [FAQ](#faq).
- 🐛 **agy stdout bug persists** — `-p` still doesn't print the answer to stdout on 1.0.9 (the 1.0.9
  "print-mode resumption" changelog fix did **not** change this for fresh `-p`). If a future release
  fixes stdout, this workaround becomes redundant but harmless. (Codex never had this problem.)
- 👁️ **Watch mode is experimental** — pass `watch=true` to any single-prompt tool to open the
  **Agent Intern** window and watch the agent work live (coarse steps; image shown inline).
  Best-effort and cross-platform; see [Watch mode](#watch-mode).
- 🔒 **Sandbox** — agy's `--sandbox` (since 1.0.6) blocks only shell commands in `-p`; file writes
  and network egress stay open, so it's no boundary and the bridge never passes it. **Codex's
  `sandbox` is real** — use it; default is `read-only`. See [Security](#security).

## Requirements

- Python 3.10+
- **For the Antigravity tools:** [`agy`](https://antigravity.google/) 1.0.0+ on `PATH` (state-file layout re-verified on **1.0.12**) and an active Antigravity / AI Pro session
- **For the Codex tools:** [`codex`](https://developers.openai.com/codex/) on `PATH` and logged in (`codex login`) — verified on **codex-cli 0.141.0**

Each backend is independent — install only the CLI(s) you plan to use; the other tools simply report "not found" via their `*_status` tool.

> [!TIP]
> If `agy` isn't reliably on `PATH` (e.g. a new terminal or reboot drops it on Windows), set the
> **`AGY_BIN`** env var to its full path and the bridge will use that instead of `"agy"` — e.g.
> `AGY_BIN=%LOCALAPPDATA%\agy\bin\agy.exe`. Likewise, set **`CODEX_BIN`** if `codex` isn't reliably on
> `PATH` (the native Windows installer puts it under `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\`).

The bridge uses only cross-platform Python (`Path.home()`, `subprocess`) and reads paths under
`~/.gemini/antigravity-cli/` and `~/.codex/`, which the CLIs write the same way on every OS.
**Developed and verified on Windows; macOS and Linux should work unmodified provided the CLIs run
there.** If you test it on those platforms, please open an issue / PR to confirm.

## Development

```bash
pip install -e ".[dev]"          # fastmcp + pytest + ruff
pytest test_server.py test_swarm.py test_codex.py   # offline unit tests — no agy/codex, no quota
ruff check . && ruff format --check .
```

`test_server.py`, `test_swarm.py`, and `test_codex.py` cover the pure parsing/version/swarm/Codex
logic with temp fixtures (no agy or codex needed); `test_smoke.py` is the live end-to-end check (ask,
continue, image, and a parallel swarm) that spends a little quota. Set **`AGY_BRIDGE_DEBUG=1`**
to log per-call diagnostics (resolved conversation id, agy exit code, elapsed) to stderr — and on
startup the server warns if your installed agy is newer than the version it was verified against.

**Staying up to date.** Updates are opt-in by design: plain `uvx agent-intern` pins to the
version it first cached, and a `git clone` never auto-updates — so the bridge only ever runs code
you chose to install (it runs unsandboxed, so this is deliberate, not laziness). Nothing updates a
*running* server either; new versions take effect on the next Claude Code restart. You find out about
a release two ways, both best-effort GitHub tag checks against the running code (`__version__` in
`server.py`):

- **In chat — [`antigravity_status`](#tools)** leads with a `bridge version` row, e.g.
  `v0.15.2 (latest)` or `v0.15.2 -> v0.15.3 available; upgrade: uvx agent-intern@latest`. This
  is the notice you actually see in the MCP client's UI (an available update stays `[ok]` — it's
  informational, not a fault).
- **At startup — stderr**, where the server logs the same one-line warning. This lands in the host's
  MCP logs only (e.g. via `/mcp` in Claude Code), not the chat.

Upgrade with `uvx agent-intern@latest` (or `git pull`) and restart, or opt into hands-off
auto-updates by putting `agent-intern@latest` in the config. Both checks are silent when
offline or rate-limited and never block startup. Control them with:

| Env var | Effect |
|---|---|
| `AGY_BRIDGE_NO_UPDATE_CHECK=1` | Skip the GitHub check entirely (fully offline startup). |
| `AGY_BRIDGE_REPO=owner/name` | Point the check at a fork instead of the upstream repo. |

**Releasing.** Bump the version in **both** `pyproject.toml` and `server.py` (`__version__`), update
[`CHANGELOG.md`](CHANGELOG.md), then tag:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

The tag triggers two workflows: `release.yml` cuts a GitHub Release with auto-generated notes, and
`publish.yml` builds and uploads to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(no stored token — `publish.yml` verifies the tag matches `pyproject.toml` first). One-time setup:
register the trusted publisher at `pypi.org/manage/project/agent-intern/settings/publishing/`
(repo `SinanTufekci/agent-intern`, workflow `publish.yml`, environment `pypi`).

## Contributing

Personal project, **best-effort maintenance** — issues and PRs welcome, but no uptime/compat
promises. If `agy -p` ever starts printing to stdout correctly, the Antigravity half of this repo
becomes a fun historical artefact.

## 🌐 Community & Acknowledgments

- **Qiita (Japan):** A huge thanks to `@fallout` and the Japanese developer community for featuring this project and providing invaluable feedback!
  - [Detailed Hybrid Setup Guide (Claude Code × Antigravity CLI)](https://qiita.com/fallout/items/5097f0575b58f4c69b81)
  - [Quick Installation Guide](https://qiita.com/fallout/items/d699df3d6931c07eb38d)

> 💡 **Path Resolution Fix:** Thanks to their community's real-world testing, we identified and resolved a Windows PATH edge case where the MCP server inherits a *stale* `PATH` at startup and can't find `agy`. The `AGY_BIN` environment-variable fallback was implemented directly inspired by their report!

## License

[MIT](LICENSE). Do whatever you want with it.
