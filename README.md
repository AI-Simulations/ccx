# ccx — Claude Code on any provider

`ccx` launches Claude Code on Anthropic or on a dozen other providers (Command Code, OpenAI OAuth,
OpenRouter, NVIDIA, Gemini, OpenCode, MiniMax, Ollama Cloud, LM Studio). It includes a local
adapter that turns Claude's Messages API into OpenAI Chat Completions. It also adds a `/model` picker
window with per-model cost estimates, a status line with quotas and API cost, subagents that run on
other providers, and fallback routes.

Windows, macOS and Linux. Requirements: Python 3.10+ and `claude` on PATH. The scripts use only the
standard library; the picker window needs Tk (`python3-tk` on Debian/Ubuntu, included with python.org
and Homebrew Python). The `openai-oauth` route also needs Node.js and an
[openai-oauth](https://github.com/EvanZhouDev/openai-oauth) install.

![Launch menu: provider](docs/menu-provider.png)
![Launch menu: model, with context, price, Artificial Analysis and aggregate scores](docs/menu-model.png)

## Install

```sh
pipx install git+https://github.com/AI-Simulations/ccx     # or: uv tool install git+https://github.com/AI-Simulations/ccx
ccx-install
```

Or from a checkout: `python -B install-resume.py`. Run `ccx-install` again after upgrading.

The installer copies the runtime to `~/.config/ccx` and backs up the files it replaces. It adds a `ccx`
shell function (PowerShell 5 and 7 profiles on Windows, `~/.bashrc` and `~/.zshrc` elsewhere), and
installs the status line, the delegation hooks and the `/model` picker mod without touching your other
settings. An older install in `~/.config/cc` is copied over and its `cc` function replaced. Open a new
terminal afterwards. With a redirected Documents folder on Windows, add the
function by hand:

```powershell
function ccx { & "$env:USERPROFILE\.config\ccx\ccx-switch.ps1" @args }
```

Credentials, saved picks and backups stay in `~/.config/ccx` and are never committed. When a provider
has no key, `ccx` asks once and saves it to `keys.json`, readable only by you.

## Usage

```sh
ccx                                   # saved presets (Enter launches), edit one, or manual menus:
                                      # provider, model, effort, auto-compact, subagent, fallback
ccx official                          # normal Claude login
ccx openai-oauth gpt-6.1-sol          # ChatGPT account through the adapter
ccx commandcode-cli deepseek/deepseek-v4.1-flash
ccx openrouter anthropic/claude-opus-5.5 --autocompact 70
ccx -NoRun nvidia <model-id>          # configure this shell only, then run `claude`
ccx --resume [id] | ccx --continue    # reuses the last provider unless you name one
ccx status
```

| Route | How it connects |
|---|---|
| `official` | Through the adapter, untouched until `/model` or a fallback reroutes it (`CCX_OFFICIAL_DIRECT=1` connects directly) |
| `commandcode`, `minimax`, `opencode-*`, `openrouter` | Anthropic-format endpoint, direct; models the endpoint doesn't serve go through the adapter |
| `openai-oauth`, `nvidia`, `gemini`, `ollama-cloud`, `lmstudio` | Local adapter on `127.0.0.1:10532` |
| `commandcode-cli` | Adapter runs `cmdc` once per request (Command Code subscription, Go plan included) |
| `antigravity-cli` | Adapter runs Google's Antigravity CLI (`agy`) once per request (Google AI Pro plan, text tool protocol) |
| your own | OpenAI-format through the adapter, Anthropic-format direct (see below) |

### Custom providers

Any OpenAI- or Anthropic-compatible endpoint can be added as a route of its own:

```sh
ccx providers add myllm http://127.0.0.1:8000/v1 --no-key            # OpenAI-format, keyless local server
ccx providers add gateway https://example.com/anthropic --format anthropic --key gateway --x-api-key
ccx providers                                                         # list
ccx providers remove myllm
```

Entries live in `~/.config/ccx/providers.json`. A key id `gateway` reads `GATEWAY_API_KEY` or `keys.json`
(ccx asks once on first use). The adapter picks up new routes without a restart once it runs this version.

### Unofficial routes

`openai-oauth` uses a ChatGPT login outside OpenAI's own apps, `commandcode-cli` drives the Command Code
CLI, and the OpenCode key renewal automates a signed-in browser. None of these are endorsed by the
providers; check their terms and use them at your own risk. API-key routes are the supported way.

The adapter passes through streaming text, thinking, tools, images, usage and effort. It stores no
keys and logs no prompts. Each model gets its declared context window
(`CLAUDE_CODE_MAX_CONTEXT_TOKENS`), and `/effort` keeps working on models Claude Code doesn't know.
Non-Claude models are experimental in Claude Code: tool use is not guaranteed. Server-side Anthropic
tools and prompt cache controls do not carry over.

## `/model` picker

`/model` with no arguments opens a picker window (Tkinter, no dependencies). It has six tabs: Model,
Subagent, Fallbacks, Auto, Tools and Presets. Each tab has search and sorting by intelligence, score,
context, price or family, and shows the effort and auto-compact controls. The header shows what this
session has cost so far at API list prices. The **This session** column estimates what the same
tokens would have cost on each listed model (cache reads at 0.1x and writes at 1.25x input).

On adapter routes the Model tab switches provider and model live for this session only. Direct routes
copy a `ccx … --resume` command that continues the conversation on the new provider. `/model <id>`
still works the usual way. Without a window (for example, no Tk) the picker falls back to an
in-terminal grid.

Model listings are cached for a day in `~/.config/ccx/models-cache.json`; while cached, a background
`ccx models-refresh <provider>` rechecks at most every 30 minutes, so models added that day still appear.

Each provider and model family shows a coloured initials badge. To use a real logo instead, drop a
24×24 SVG named after the provider or family (`openai.svg`, `deepseek.svg`, …) into
`~/.config/ccx/logos`; it needs Tk 8.7 or newer, which reads SVG.

![Model tab](docs/picker-model.png)
![Presets tab](docs/picker-presets.png)

Presets store one Enter-away bundle per provider (main model, subagent, fallback, Auto and Tools
helpers): `ccx presets`, `ccx presets set <provider> <model> [effort]`. `ccx gui [tab]` opens the window
directly.

### Setting each part separately

Each part has its own model, set one at a time in a preset (kept for later launches) or with a launch flag
(this launch only, over the preset). A launch on a provider starts from that provider's preset (here
`official`); a named preset (any other name) is applied in a session from /model's Presets tab:

```sh
ccx presets set official main official claude-opus-5-5 high  # main model (a new named preset starts here)
ccx presets set official subagent nvidia deepseek-ai/deepseek-v4-pro low   # one subagent model
ccx presets set official subagent router                     # the difficulty router instead
ccx presets set official level easy official claude-haiku-5-5 max  # one router level (`off` removes it)
ccx presets set official ranges 30 75 89 95                  # router ranges: one top per level but the last
ccx presets set official auto openai-oauth gpt-6-luna low    # Auto mode's classifier model (`off` = ccx default)
ccx presets set official tools openai-oauth gpt-6-luna low   # Tools helper model (`off` = ccx default)
ccx presets set official fallback nvidia deepseek-ai/x       # fallback route on another provider (`off` = none)
ccx presets help                                             # the list above

ccx openai-oauth gpt-6.1-sol --subagent router --level easy=official/claude-haiku-5-5@max \
    --auto-model openai-oauth/gpt-6-luna@low --tools-model off --fallback nvidia/deepseek-ai/x
```

Flags take `<provider>/<model>[@effort]` (a trailing `@word` is an effort only when it names one, so ids like
`qwen3-8b@q4_k_m` pass whole); `--subagent` also takes `router` or `off`, and `--level` repeats. A router needs
two or more levels. Auto and Tools helpers need an adapter session; `--fallback` an adapter route or `official`.
On a resume, subagent flags go over that session's own picks. Words after `--`, and every word of plain
`claude`, go to Claude Code untouched (its own `--tools` and `--agent` included).

### Other harnesses: pi, Hermes, opencode, Command Code, Codex, Antigravity CLI

`ccx --harness <pi|hermes|opencode|commandcode|codex|agy> <provider> <model> [--effort E] [part flags] [-- harness arguments]`
starts another agent CLI with the same parts: the provider's preset, then the flags above. Every model call goes
through the adapter at `/ccx/<launch>/v1/messages` (Codex: `/v1/responses`; agy: `/v1/chat/completions`, both
converted to and from the Messages format there), where placeholder models name the part (`ccx-main`,
`ccx-live-<subagent or level>`, `ccx-auto`, `ccx-tools`) and the adapter swaps in this launch's picks, its keys and
its fallback. The claude.ai login (`official`) serves Claude Code only, so it is refused there.

| Harness | Main | Subagents / router | Auto | Tools | Your own config |
|---|---|---|---|---|---|
| pi | `--model ccx/ccx-main --thinking` | `subagent` tool from ccx's extension (`harness/pi/ccx.ts`) | the extension's gate on tool calls | compaction | untouched (`-e` per launch) |
| Hermes | `--provider ccx -m ccx-main` | `delegation` (one model) or the `ccx-router` plugin | `auxiliary.approval` | compaction, titles, MCP, memory | untouched: a `ccx<id>` Hermes profile per set of picks (your Hermes gateway serves profiles; a separate home cannot start beside it), reusing your skills; delete one with `hermes profile delete <name>` |
| opencode | `agents.build.model` with the effort variant | one `ccx-<name>` subagent per profile or level | ccx's plugin judges what your rules allow or ask; your denies stay final | title, summary, compaction | merged over yours (`OPENCODE_CONFIG_CONTENT`, `--standalone`) |
| Command Code | BYOK provider `ccx`, `--effort` | `ccx-<name>` agents | `--yolo` plus a PreToolUse hook asking the classifier on every tool | title, compaction, tool descriptions, branch summaries | untouched: a ccx-owned home per set of picks under `%LOCALAPPDATA%\ccx\homes` (`HOME`/`USERPROFILE`) that links back every folder and file of yours, your Command Code login, skills, mods and settings included; `--local-only` |
| Codex | provider `ccx` on the Responses route, `-m ccx-main` | one agent role per profile or level (`agents.ccx-<name>`) | Codex's own reviewer (`approvals_reviewer = auto_review`), answered by the Auto pick | `/review`, memories, compaction | untouched (`-c` overrides per launch) |
| agy | gateway mode (`AGY_LLM_GATEWAY_*`), `--model ccx-main` | every profile or level as a gateway model | agy's own | agy's own | untouched (environment per launch) |

Launches with the same picks share one id (and one profile or home); other picks never rewrite a running
session's. Those homes link to your own folders: never delete one with a tool that follows links. ccx does not
clean them up: delete an old Hermes profile with `hermes profile delete ccx<id>`, and an old Command Code home by
removing its links first. Codex role files live in `~/.config/ccx/codex`. A launch's routes expire after 30 days; start the harness through ccx again to resume
on it.

A part you leave unset keeps the harness's own behaviour (pi has no approvals; Hermes, opencode and Command Code
their usual ones). In pi and opencode a `/model` pick inside the session is saved to your own settings as usual.
Codex takes model names only from its own list, so each subagent profile or router level is a Codex agent role
(`ccx-<name>`) and the `default` role is the one profile or the router's middle level. agy has no Auto or Tools
model of its own to set.

Inside a harness session, `/ccx` runs ccx on that session's launch with no model turn: alone it opens the model
picker window, and `/ccx route <provider> <model>`, `/ccx fallback ...`, `/ccx helper auto|tools ...` and
`/ccx agents ...` change that session's parts only. pi, Hermes, opencode and Command Code get `/ccx` from a ccx
extension, plugin or mod loaded for the launch; Codex and agy cannot add slash commands, so there it is
`!ccx ...` (a `ccx` command ccx puts first on the session's `PATH`, which a model's shell tool reaches too).

## Status line

![Status line](docs/statusline.png)

The first line shows the model, its effort dots (one per step of that model's own ladder), context
use with the auto-compact point, and quota windows with time to reset. The second line shows the
Auto and Tools helper models. Below come the subagent rows: one per router level, with its score range,
model, effort dots and compact window, then its context, cache share and cost once it has run. The last line
is the session's API-price cost, then the week's spend against the plan's equivalent quota. Quotas
come from the provider's usage endpoint or from the adapter's last rate-limit headers. A failed fetch
names its cause (`key expired`, `auth failed`) instead of hiding the bar.

## Subagents on other providers

Profiles in `~/.config/ccx/agents.json` each run as their own headless `claude -p`, with their own
provider, model, effort and auto-compact:

```json
{ "cheap": {"provider": "openai-oauth", "model": "gpt-6-luna", "effort": "low", "autocompact": 60},
  "deep":  {"provider": "official", "model": "claude-opus-5-5", "effort": "max"} }
```

```sh
ccx agents                                    # edit profiles in a menu
ccx agents set cheap --provider nvidia --model deepseek-ai/deepseek-v4-pro --effort low
ccx agent cheap "task text"                   # run one task directly
```

Each profile becomes a `ccx-<name>` subagent type. The installed hooks steer delegation to those
types. A worker resumes an isolated copy of the parent conversation, plus project instructions and
memory, and never rewrites the original transcript. In adapter sessions the worker runs in-process,
so its tab shows the full chat.

### Router

The `very-easy`, `easy`, `mid`, `hard` and `very-hard` profiles form a difficulty router. With it on, the main
model starts each call's description with a 0-100 score (`[35] rename helper`); ccx runs the call on the level
whose range holds it, whichever level was named, and a task that fails review reruns one range up. Defaults:
very easy 0-30, easy 31-75, mid 76-89, hard 90-95, very hard 96-100. A level the running session does not know
yet (added after it started) falls back to the nearest one it knows until `/reload-plugins`.

```sh
ccx agents router on|off                      # this session
ccx agents router ranges 30 75 89 95          # one top per level but the last
ccx agents router calibrate 20 40 20 15       # tops from logged scores, as shares per level
ccx agents router best openai-oauth           # (model, effort) per level from AA score and price
```

`best` picks, for each level, the cheapest (model, effort) pair that reaches a share of the provider's best
Artificial Analysis score (very easy 50%, easy 65%, mid 80%, hard 90%; very hard takes the best). AA scores
each reasoning effort separately; `ccx facts <provider>` lists them as `aa_efforts`, and the `/model` window's
AA column follows its Effort box. Higher efforts count as dearer (an estimate: AA prices tokens, not efforts).

![Subagent tab with the router's difficulty bar](docs/picker-router.png)

In `/model`'s Subagent tab the Difficulty bar shows each level's range, model, effort and compact window in its colour: drag a
boundary to move a range, click a level to pick its model, effort and Compact (Apply keeps the model when only those change). Preset levels store a compact window each; none means inherit. Presets start
with the router on; **Best picks** fills the levels from `best`.

In auto mode Claude Code refuses an Agent call that a hook rewrote, so there the hint asks the main model to
name the level's in-process worker (`ccx-easy-live`) directly; a call ccx still has to remap is denied once
with the exact call to resubmit.

At launch the subagent step opens the same editor in the terminal: router on/off, each level's route, the
ranges and best picks; **edit a preset…** asks router or one model and keeps a router preset a router.

Each routed call's score lands in `~/.config/ccx/router-log.jsonl`. `calibrate` needs 20 scores and uses the
last 500.

## Fallbacks and limits

- **Fallback route:** an adapter session can name a second route, used once when the first answers
  429, 402 or 5xx. Set it with `/fallback <provider> <model>`, `/fallback off`, or in the launch menu.
- **Key rotation:** put spare keys, comma-separated, in `<KEY>_API_KEYS`. A key that hit a limit
  rests until its retry-after.
- **Usage limits:** when a provider reports a real reset time, the adapter returns Claude Code's own
  usage-limit headers, so `/rate-limit-options` and auto-continue work as on Anthropic.
- **OpenCode keys:** an expired Go/Zen key is renewed from a signed-in Chrome profile
  (`python opencode_key.py`).

## Tests

```sh
python -B -m pytest -q             # offline: fake credentials, fake claude/codex, stubbed GUI
python -B critic_mock_check.py     # optional: real Claude Code against local mock providers
```

`test_ccx_switch.py` drives the PowerShell launcher and runs on Windows; `test_ccx_posix.py` sources
`apply.sh` in a real shell and reads keys through a pty on macOS/Linux. No paid generation runs in
these checks. They do not cover live OAuth generation, account model access or model quality.

References: [LLM gateway](https://code.claude.com/docs/en/llm-gateway) ·
[gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol) ·
[Command Code provider](https://commandcode.ai/docs/provider) ·
[OpenRouter + Claude Code](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration)
