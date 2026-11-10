#!/usr/bin/env python3
# ccx-switch.py - Claude Code provider switcher (menu UI, stdlib only)
# Usage:
#   ccx                     interactive menu, then opens Claude Code
#   ccx <provider> [model]  e.g. ccx official | ccx minimax | ccx opencode-go
#   ccx -NoRun <provider>   switch without launching a client
#   ccx status              show current session state
# Only affects the current shell: env vars are applied by the shell's `ccx` function
# sourcing apply.ps1 (PowerShell) or apply.sh (bash/zsh), since a child process cannot set them.

# tempfile and urllib.request load where used: hooks run often and never need them
import contextlib, itertools, json, os, re, subprocess, sys, time, uuid
from pathlib import Path

sys.path.insert(0, os.environ.get("AI_SUITE_DIR") or str(Path(__file__).resolve().parents[1] / "ai-suite"))
import importlib


class _Lazy:
    """Imports on first use: ai_suite pulls in openai and google.genai (~2 s), which hooks never need."""
    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)

    def __setattr__(self, attr, value):
        # Writes reach the real module, so patching `catalog._arrow_menu` patches its callers too.
        setattr(importlib.import_module(object.__getattribute__(self, "_name")), attr, value)

    def __delattr__(self, attr):
        delattr(importlib.import_module(object.__getattribute__(self, "_name")), attr)


_service = _Lazy("ai_suite.service")
catalog = _Lazy("ai_suite.providers")  # book writer's menus, model facts and colors


def background_process_options():
    return _service.background_process_options()


def ensure_openai_oauth_proxy():
    return _service.ensure_openai_oauth_proxy()

CFG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ccx")
APPLY_FILE = os.path.join(CFG_DIR, "apply.ps1" if os.name == "nt" else "apply.sh")
PYTHON = "python" if os.name == "nt" else "python3"  # the command a relay's Bash tool runs
STATE_FILE = os.path.join(CFG_DIR, "state.json")
AGENTS_FILE = os.path.join(CFG_DIR, "agents.json")
SESSION_AGENTS_FILE = os.path.join(CFG_DIR, "session-agents.json")
ROUTES_FILE = os.path.join(CFG_DIR, "routes.json")
KEYS_FILE = os.path.join(CFG_DIR, "keys.json")
EFFORTS_FILE = os.path.join(CFG_DIR, "efforts.json")
# Every provider's model listing, shared by the CLI, the /model window, the picker and the adapter: served
# from disk for MODELS_TTL, and refetched in a background process at most every MODELS_RECHECK meanwhile.
MODELS_CACHE_FILE = os.path.join(CFG_DIR, "models-cache.json")
MODELS_TTL = 86400
MODELS_RECHECK = 1800
CLAUDE_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
AGENTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "agents")
OPENCODE_AUTH = os.path.expanduser(r"~\.local\share\opencode\auth.json")
OPENCODE_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.jsonc")
OAUTH_PORT = 10531
ADAPTER_PORT = 10532

CCX_VARS = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
           "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_CUSTOM_HEADERS", "ENABLE_TOOL_SEARCH", "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_MODE_SERVER", "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
           "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
           "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME", "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", "ENABLE_CLAUDEAI_MCP_SERVERS", "API_TIMEOUT_MS",
           "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
           "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"]
# /model picker entries beyond the chosen model (which also takes Opus and Haiku, so
# background work stays on it), filled with this provider's other models.
PICKER_SLOTS = ["ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_CUSTOM_MODEL_OPTION"]
# Claude Code's own /effort levels (claude --help).
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Auto-compact thresholds offered by the menus; --autocompact takes any 1-100.
AUTOCOMPACTS = [50, 60, 70, 80, 90, 95]
COMPACT_TOKENS = ["100k", "200k", "300k", "400k", "600k"]
AGENT_FLAGS = ("--provider", "--model", "--effort", "--autocompact", "--description")
OAI_VARS = ["OPENAI_BASE_URL", "OPENAI_API_KEY"]
CCX_VARS += [slot + "_SUPPORTED_CAPABILITIES" for slot in ["ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", *PICKER_SLOTS]]
CCX_VARS += ["CCX_AGENT_CHILD", "CCX_LAUNCH_ID", "CCX_MAIN_PROVIDER", "CCX_WORKER_PROFILE", "CLAUDE_CODE_FORK_SUBAGENT"]
CONTEXT_PREFIX = "CCX_CONTEXT_V1 "

PROVIDERS = {
    "commandcode": {"label": "Command Code API (needs Provider plan; Go plan is CLI-only)", "format": "anthropic", "base": "https://api.commandcode.ai/provider", "key": "commandcode"},
    "commandcode-cli": {"label": "Command Code subscription on the cmdc CLI login (Go plan; native tools, prompt cache)", "format": "openai", "key": "cli"},
    "official":     {"label": "Claude official (claude.ai login)", "format": "anthropic", "base": ""},
    "minimax":      {"label": "MiniMax (Anthropic API)", "format": "anthropic", "base": "https://api.minimax.io/anthropic", "key": "minimax"},
    "opencode-go":  {"label": "OpenCode Go subscription (x-api-key, live key)", "format": "anthropic", "base": "https://opencode.ai/zen/go", "key": "opencode-go", "header": "x-api-key"},
    "opencode-zen": {"label": "OpenCode Zen PAYG (needs account funds; free models are OpenCode-only)", "format": "anthropic", "base": "https://opencode.ai/zen", "key": "opencode", "header": "x-api-key"},
    "openai-oauth": {"label": "OpenAI OAuth proxy - ChatGPT account (OpenAI-format)", "format": "openai", "base": f"http://127.0.0.1:{OAUTH_PORT}/v1", "key": "oauth"},
    "openrouter":   {"label": "Claude Code via OpenRouter (API credits)", "format": "anthropic", "base": "https://openrouter.ai/api", "key": "openrouter", "auth": "bearer"},
    "nvidia":       {"label": "Claude Code via NVIDIA NIM", "format": "openai", "base": "https://integrate.api.nvidia.com/v1", "key": "nvidia"},
    "gemini":       {"label": "Google Gemini API (OpenAI-format; opencode's google key)", "format": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": "google"},
    "ollama-cloud": {"label": "Ollama Cloud (OpenAI-format)", "format": "openai", "base": "https://ollama.com/v1", "key": "ollama-cloud"},
    "lmstudio":     {"label": "LM Studio local server (:1234, start it first)", "format": "openai", "base": "http://127.0.0.1:1234/v1", "key": "lmstudio"},
}

BUILTIN_PROVIDERS = frozenset(PROVIDERS)
# Your own endpoints: {"name": {"base": url, "format": "openai"|"anthropic", "key": key id ("" = none),
# "header": "x-api-key" (anthropic only), "label": text}}. `ccx providers add` writes it.
CUSTOM_PROVIDERS_FILE = os.path.join(CFG_DIR, "providers.json")
_CUSTOM_SEEN = [None, {}]  # (mtime, entries) of the last read


def check_custom_provider(name, entry):
    """A cleaned custom provider entry; ValueError says what is wrong."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name or "") or name in BUILTIN_PROVIDERS:
        raise ValueError(f"'{name}': use lowercase letters, digits and hyphens, not a built-in provider name")
    if not isinstance(entry, dict) or not re.fullmatch(r"https?://[^\s]+", str(entry.get("base") or "")):
        raise ValueError(f"{name}: base must be an http(s) URL")
    fmt = entry.get("format", "openai")
    if fmt not in ("openai", "anthropic"):
        raise ValueError(f"{name}: format must be openai or anthropic")
    key = entry.get("key", name)
    if not isinstance(key, str) or (key and not re.fullmatch(r"[a-z0-9-]+", key)) or key in ("oauth", "cli"):
        raise ValueError(f"{name}: key must be a key id (lowercase, digits, hyphens) or empty for none")
    clean = {"label": str(entry.get("label") or f"{name} (custom, {fmt}-format)"), "format": fmt,
             "base": entry["base"].rstrip("/"), "key": key, "custom": True}
    if fmt == "anthropic" and entry.get("header") == "x-api-key":
        clean["header"] = "x-api-key"
    return clean


def custom_providers():
    """The valid entries of providers.json, also merged into PROVIDERS (reread when the file changes)."""
    try:
        mtime = os.path.getmtime(CUSTOM_PROVIDERS_FILE)
    except OSError:
        mtime = None
    if mtime != _CUSTOM_SEEN[0]:
        entries = {}
        try:
            with open(CUSTOM_PROVIDERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for name, entry in (data.items() if isinstance(data, dict) else ()):
                try:
                    entries[name] = check_custom_provider(name, entry)
                except ValueError as exc:
                    print(f"ccx: providers.json: {exc}", file=sys.stderr)  # one bad entry must not hide the rest
        except (OSError, ValueError):
            pass
        for name in set(PROVIDERS) - BUILTIN_PROVIDERS - set(entries):
            del PROVIDERS[name]
        PROVIDERS.update(entries)
        _CUSTOM_SEEN[:] = [mtime, entries]
    return _CUSTOM_SEEN[1]


def providers_command(args):
    """ccx providers [add <name> <base> [--format openai|anthropic] [--key <id>|--no-key] [--x-api-key] [--label text] | remove <name>]"""
    add = ("ccx providers add <name> <base-url> [--format openai|anthropic] [--key <key id>|--no-key] "
           "[--x-api-key] [--label <text>]")
    usage = f"Usage: {add}\n       ccx providers remove <name>"
    try:
        with open(CUSTOM_PROVIDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    if not args:
        for name, entry in custom_providers().items():
            key = entry["key"] or "none"
            print(f"  {name:<16} {entry['format']:<9} {entry['base']}  key: {key}")
        if not data:
            print("No custom providers. " + add)
        return
    if args[0] == "remove" and len(args) == 2:
        if data.pop(args[1], None) is None:
            die(f"No custom provider '{args[1]}'.")
        write_json(CUSTOM_PROVIDERS_FILE, data, indent=1)
        _CUSTOM_SEEN[0] = "stale"  # the mtime may not tick within the same instant
        custom_providers()
        return print(f"Removed {args[1]}.")
    if args[0] != "add" or len(args) < 3:
        die(usage)
    entry, rest = {"base": args[2]}, args[3:]
    while rest:
        flag = rest.pop(0)
        if flag == "--no-key":
            entry["key"] = ""
        elif flag == "--x-api-key":
            entry["header"] = "x-api-key"
        elif flag in ("--format", "--key", "--label") and rest:
            entry[flag[2:]] = rest.pop(0)
        else:
            die(usage)
    try:
        clean = check_custom_provider(args[1], entry)
    except ValueError as exc:
        die(str(exc))
    data[args[1]] = entry
    write_json(CUSTOM_PROVIDERS_FILE, data, indent=1)
    _CUSTOM_SEEN[0] = "stale"
    custom_providers()
    hint = f" Key: set {key_env_name(clean['key'])} or run `ccx` and pick it to be asked." if clean["key"] else ""
    print(f"Added {args[1]} ({clean['format']}-format, {clean['base']}).{hint}")


custom_providers()

# Routes the adapter serves, so a session on one can fall back to another (ccx fallback).
FALLBACK_ROUTES = sorted(n for n, p in PROVIDERS.items() if p["format"] == "openai" and p.get("key") != "cli")
FALLBACKS_FILE = os.path.join(CFG_DIR, "fallbacks.json")

# go and zen are DIFFERENT gateways with different model catalogs
FALLBACK_MODELS = {
    "commandcode": [("claude-opus-5-5", ""), ("claude-fable-5-1", ""), ("claude-sonnet-5", ""),
                    ("gpt-6-sol", ""), ("deepseek/deepseek-v4.1-flash", "")],
    "opencode-go":  [("glm-5.3", "GLM-5.3"), ("kimi-k3", "Kimi K3"), ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
                     ("deepseek-v4-pro", "DeepSeek V4 Pro"), ("qwen3.8-max", "Qwen3.8 Max"),
                     ("minimax-m3", "MiniMax M3"), ("gpt-6-luna", "GPT-6 Luna")],
    "opencode-zen": [("claude-opus-5-5", ""), ("claude-fable-5-1", ""), ("claude-sonnet-5", ""),
                     ("gpt-6-sol", ""), ("deepseek-v4.1-flash", "")],
    "openrouter":   [("anthropic/claude-opus-5.5", ""), ("anthropic/claude-fable-5.1", ""),
                     ("openai/gpt-6-sol", ""), ("deepseek/deepseek-v4.1-flash", "")],
    "commandcode-cli": [("deepseek/deepseek-v4.1-flash", ""), ("moonshotai/kimi-k3", ""), ("zai-org/glm-5.3", "")],
    "nvidia":       [("deepseek-ai/deepseek-v4.1-flash", ""), ("z-ai/glm-5.3", ""), ("moonshotai/kimi-k2.6", "")],
    "openai-oauth": [("gpt-6.1-sol", ""), ("gpt-6.1-terra", ""), ("gpt-6-sol", ""), ("gpt-6-luna", "")],
    "minimax":      [("MiniMax-M3", ""), ("MiniMax-M2.7", "")],
}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"  WARNING: {msg}", file=sys.stderr)


def user_env(name):
    """A user-level variable set in Windows' registry but not yet in this shell; '' elsewhere."""
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v
    except OSError:
        return ""


def auth_key(name):
    # Only read this provider's credentials; never send another provider's key.
    env_name = {"commandcode": "CMD_API_KEY"}.get(name) or name.upper().replace("-", "_") + "_API_KEY"
    key = os.environ.get(env_name) or user_env(env_name)
    if key:
        return key
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            stored = json.load(f).get(name)
        if isinstance(stored, str) and stored:
            return stored
    except (OSError, ValueError):
        pass
    if name == "commandcode":
        try:
            with open(os.path.expanduser(r"~\.commandcode\auth.json"), encoding="utf-8-sig") as f:
                return json.load(f).get("apiKey", "")
        except (OSError, ValueError):
            return ""
    try:
        auth = json.load(open(OPENCODE_AUTH, encoding="utf-8"))
    except Exception:
        auth = {}
    # opencode files a MiniMax Coding Plan key under its own provider id.
    for c in {"minimax": ["minimax", "minimax-coding-plan"]}.get(name, [name]):
        e = auth.get(c)
        if e and e.get("type") == "api" and e.get("key"):
            return e["key"]
    return ""


def key_env_name(name):
    return {"commandcode": "CMD_API_KEY"}.get(name) or name.upper().replace("-", "_") + "_API_KEY"


def save_key(name, value):
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            keys = json.load(f)
    except (OSError, ValueError):
        keys = {}
    keys[name] = value
    write_json(KEYS_FILE, keys, indent=1)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass


_OPENCODE_CHECKED = set()


def renew_opencode_key(key):
    """Replace a rejected OpenCode key from the signed-in Chrome profile (see opencode_key.py)."""
    import opencode_key
    # Only opencode's own auth.json is renewed; an env or keys.json key stays the user's call.
    if key != opencode_key.stored_key() or opencode_key.key_ok(key) is not False:
        return
    print("OpenCode rejected its API key; creating a new one from the console...", file=sys.stderr)
    interactive = os.environ.get("CCX_AGENT_CHILD") != "1" and sys.stdin.isatty() and sys.stdout.isatty()
    try:
        new = opencode_key.renew(interactive)
    except Exception as exc:
        warn(f"OpenCode key renewal failed: {exc}. Run: python {opencode_key.__file__}")
        return
    if auth_key("opencode-go") != new:
        warn("OPENCODE_GO_API_KEY or keys.json overrides the renewed key in opencode's auth.json.")


def ensure_key(name):
    """Ask for a missing provider key once, so the model menu can list its models."""
    p = PROVIDERS.get(name, {})
    key_id = p.get("key")
    if not key_id or key_id in ("oauth", "cli") or name == "lmstudio":
        return
    key = auth_key(key_id)
    if key and key_id in ("opencode", "opencode-go") and key_id not in _OPENCODE_CHECKED:
        _OPENCODE_CHECKED.add(key_id)
        renew_opencode_key(key)
    if key:
        return
    if os.environ.get("CCX_AGENT_CHILD") == "1" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return  # route_env reports the missing key
    print(f"No API key for {name}. Get one from its account page.", file=sys.stderr)
    try:
        value = input(f"{name} API key (Enter to cancel): ").strip()
    except EOFError:
        return
    if not value:
        return
    save_key(key_id, value)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_json(path, data, **dump):
    # Replace, not rewrite: a Ctrl+C quit mid-save must not leave a truncated file.
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, **dump)
    os.replace(path + ".tmp", path)


def save_state(data):
    write_json(STATE_FILE, data)


def quit_on_ctrl_c():
    """Ctrl+C quits at once. Python's KeyboardInterrupt cannot interrupt a Windows socket or
    child-process wait, so a model listing would otherwise hold it until its timeout."""
    if not sys.stdin.isatty():
        return
    if os.name != "nt":
        import signal
        import termios
        saved = termios.tcgetattr(sys.stdin.fileno())

        def stop(*_):
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)  # a menu may hold cbreak mode
            sys.__stdout__.write("\nNo change.\n")
            sys.__stdout__.flush()
            os._exit(0)
        signal.signal(signal.SIGINT, stop)
        return
    import ctypes

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    def handler(event):
        if event != 0:  # CTRL_C_EVENT only
            return 0
        sys.__stdout__.write("\nNo change.\n")
        sys.__stdout__.flush()
        os._exit(0)  # nothing is half-written: saves replace whole files

    quit_on_ctrl_c.handler = handler  # the console keeps calling it; keep it alive
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


def fetch(url, headers=None, timeout=15):
    import urllib.request
    h = {"User-Agent": "Mozilla/5.0 ccx-switch"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def go_limits():
    try:
        cfg = json.load(open(OPENCODE_CONFIG, encoding="utf-8"))
        return {n: (v.get("limit") or {}) for n, v in cfg["provider"]["opencode-go"]["models"].items()}
    except Exception:
        return {}


# Command Code's plans in ascending order and the categories each may use (its own tables):
# a model's minimumPlan is the cheapest plan allowed to use it; without one, its category
# decides. Purchased or free credits unlock every model, as in cmdc.
CCX_PLANS = ["individual-go", "individual-go-v1", "individual-goat", "individual-pro",
            "individual-pro-v1", "individual-provider", "individual-max", "individual-ultra", "teams-pro"]
CCX_PLAN_CATEGORIES = {
    "individual-go": {"opensource"}, "individual-go-v1": {"opensource"}, "individual-goat": {"opensource"},
    "individual-pro": {"premium", "opensource"}, "individual-pro-v1": {"premium", "opensource"},
    "individual-provider": {"premium", "opensource"}, "individual-max": {"premium", "opensource"},
    "individual-ultra": {"premium", "opensource"}, "teams-pro": {"premium", "opensource"},
}
CCX_ACTIVE_SUBSCRIPTIONS = {"active", "trialing", "past_due"}
COMMANDCODE_API = "https://api.commandcode.ai"
PLAN_TTL = 600
_PLAN_CACHE = {}


def commandcode_model_allowed(m, plan, rank):
    """cmdc's access rule for one catalog row: plan rank, else the plan's categories."""
    if m.get("hidden"):
        return False
    category = m.get("category")
    if category not in ("premium", "opensource"):
        return True  # a model cmdc cannot classify is not restricted
    if "minimumPlan" not in m:
        return category in CCX_PLAN_CATEGORIES[plan]
    minimum = m["minimumPlan"]
    if minimum is None:
        return False  # cmdc locks a null minimumPlan for every plan
    if minimum in CCX_PLANS:
        return rank >= CCX_PLANS.index(minimum)
    return category in CCX_PLAN_CATEGORIES[plan]  # a newer plan id than this ccx knows


_CATALOG_CACHE = {}


def commandcode_catalog():
    """The client catalog rows (plan, category, reasoning efforts), cached like the plan."""
    cached = _CATALOG_CACHE.get("value")
    if cached and time.time() - cached[0] < PLAN_TTL:
        return cached[1]
    try:
        rows = fetch(f"{COMMANDCODE_API}/provider/v1/models?view=client",
                     {"Authorization": "Bearer " + auth_key("commandcode")})["data"]
    except Exception:
        rows = None
    _CATALOG_CACHE["value"] = (time.time(), rows)
    return rows


def _efforts_cache():
    """{model id: efforts} fetched in the last day, or None when it must be refetched."""
    try:
        with open(EFFORTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("at", 0) < 86400:
            return data.get("models") or {}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def _efforts_save(models):
    """The status line reads this instead of asking the network every refresh."""
    try:
        write_json(EFFORTS_FILE, {"at": time.time(), "models": models}, indent=1)
    except OSError:
        pass


def commandcode_efforts(model_id):
    """The reasoning efforts this model accepts ([] for none); None when it is not listed.

    Every catalog row carries `reasoningEfforts`; models such as MiMo V2.6 Pro carry null,
    which means they take no adjustable effort (cmdc exits 1 if `--effort` is passed).
    """
    key = (model_id or "").casefold()
    cached = _efforts_cache()
    if cached:
        for mid, efforts in cached.items():
            if str(mid).casefold() == key:
                return efforts
        return None  # a fresh, complete listing: an unlisted model takes no adjustable effort
    rows = commandcode_catalog()
    if rows is None:
        return None
    models = {}
    for m in rows:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            efforts = m.get("reasoningEfforts")
            models[m["id"]] = efforts if isinstance(efforts, list) else []
    if models:  # never cache an empty listing over a transient failure
        _efforts_save(models)
    for mid, efforts in models.items():
        if mid.casefold() == key:
            return efforts
    return None


def codex_efforts(model_id):
    """The reasoning efforts Codex's model cache lists for an OpenAI model; None when unknown."""
    try:
        path = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "models_cache.json")
        with open(path, encoding="utf-8") as f:
            rows = json.load(f).get("models") or []
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("slug", "")).casefold() == (model_id or "").casefold():
            levels = row.get("supported_reasoning_levels")
            if not isinstance(levels, list):
                return None
            return [e["effort"] for e in levels if isinstance(e, dict) and isinstance(e.get("effort"), str)]
    return None


def helper_default(kind, provider):
    """Provider-specific helper defaults; explicit launch selections take precedence."""
    try:
        with open(os.path.join(CFG_DIR, "helper-defaults.json"), encoding="utf-8") as f:
            target = json.load(f).get(kind, {}).get(provider)
        if isinstance(target, dict) and isinstance(target.get("model"), str) and target.get("effort") in EFFORTS:
            return {"provider": provider, "model": target["model"], "effort": target["effort"]}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def lowest_effort(name, model_id):
    """Tools default to low when supported; otherwise inherit the main effort."""
    levels = model_efforts(name, model_id)
    return "low" if name == "official" or levels is not None and "low" in levels else None


def preset_targets():
    """The /model picker's quick presets: each provider's default model and effort.

    A preset the person saved wins, then the curated auto-helper default, then the model this
    account picked last, then the first mirror of its model list. One Enter per provider.
    """
    try:
        with open(os.path.join(CFG_DIR, "helper-defaults.json"), encoding="utf-8") as f:
            defaults = json.load(f).get("auto") or {}
    except (OSError, ValueError, AttributeError, TypeError):
        defaults = {}
    if not isinstance(defaults, dict):
        defaults = {}
    saved = load_presets()
    recent = load_state().get("recent") or {}
    names = route_providers()
    if not on_adapter():
        names = ["official", *names]  # a native session's own model, then the relaunch presets
    rows = []
    for name in names:
        custom = saved.get(name)
        if isinstance(custom, dict) and isinstance(custom.get("model"), str) and custom["model"]:
            effort = custom.get("effort") if custom.get("effort") in EFFORTS else None
            rows.append({"provider": name, "model": custom["model"], "effort": effort, "custom": True})
            continue
        default = defaults.get(name) if isinstance(defaults.get(name), dict) else {}
        model = default.get("model") if isinstance(default.get("model"), str) else None
        if name == "official":
            # the classifier's helper default is not a main-model preset: the last pick is
            model = next((m for m in recent.get("official") or [] if isinstance(m, str)), None) or model or "claude-opus-5-5"
        if not model:
            model = next((m for m in recent.get(name) or [] if isinstance(m, str)), None)
        if not model:
            first = FALLBACK_MODELS.get(name) or []
            model = first[0][0] if first else None
        if not model:
            continue
        effort = default.get("effort") if name != "official" and default.get("effort") in EFFORTS else None
        rows.append({"provider": name, "model": model, "effort": effort})
    for name, custom in saved.items():
        # a named preset the person created: its main model may be on any reachable provider
        main = saved_bundle(name, saved)
        main = main and main["model"]
        if name not in PROVIDERS and main and main.get("provider") in names:
            rows.append({"name": name, "provider": main["provider"], "model": main["model"],
                         "effort": main.get("effort"), "custom": True})
    return rows


def presets_path():
    return os.path.join(CFG_DIR, "presets.json")


def load_presets():
    """The presets the person edited, by provider; a missing or broken file is empty."""
    try:
        with open(presets_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_presets(data):
    write_json(presets_path(), data, indent=1)


PRESET_CATEGORIES = ("model", "subagent", "fallback", "auto", "tools")
PRESET_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def preset_bundle(row):
    """Expand legacy main-model presets without rewriting the person's saved file."""
    main = {"provider": row["provider"], "model": row["model"]}
    if row.get("effort"):
        main["effort"] = row["effort"]
    settings = {"model": dict(main), "subagent": default_router_target() or dict(main), "fallback": {"provider": "off"},
                "auto": helper_default("auto", row["provider"]) or dict(main),
                "tools": helper_default("tools", row["provider"]) or dict(main)}
    if not helper_default("tools", row["provider"]):
        level = lowest_effort(row["provider"], row["model"])
        if level:
            settings["tools"]["effort"] = level
    custom = load_presets().get(row.get("name") or row["provider"])
    overrides = custom.get("settings") if isinstance(custom, dict) else None
    if isinstance(overrides, dict):
        for key in PRESET_CATEGORIES[1:]:
            target = overrides.get(key)
            if isinstance(target, dict) and isinstance(target.get("provider"), str):
                if target["provider"] == "off" and key in ("fallback", "auto", "tools"):
                    settings[key] = {"provider": "off"}
                elif key == "subagent" and target["provider"] == "router" and isinstance(target.get("levels"), dict):
                    settings[key] = target
                elif target["provider"] in PROVIDERS and isinstance(target.get("model"), str) and target["model"]:
                    settings[key] = {"provider": target["provider"], "model": target["model"]}
                    if target.get("effort") in EFFORTS:
                        settings[key]["effort"] = target["effort"]
    return settings


def saved_bundle(name, saved=None):
    """The five-category preset saved under this provider or name in /model's Presets tab, or None."""
    custom = (load_presets() if saved is None else saved).get(name)
    settings = custom.get("settings") if isinstance(custom, dict) else None
    if not (isinstance(settings, dict) and set(PRESET_CATEGORIES) <= set(settings)
            and all(isinstance(settings[k], dict) for k in PRESET_CATEGORIES)):
        return None
    return settings


def default_router_target(agents=None, only=None):
    """The preset subagent by default: the router on the level profiles as set now (or `agents`, limited to the
    levels in `only`); None without two of them."""
    agents = load_agents(session=False) if agents is None else agents
    levels = [n for n in ROUTER_LEVELS if (agents.get(n) or {}).get("provider") in PROVIDERS and (agents.get(n) or {}).get("model")
              and (only is None or n in only)]
    if len(levels) < 2:
        return None
    ranges = level_ranges({n: agents[n] for n in levels})
    return {"provider": "router", "levels": {n: {k: agents[n][k] for k in ("provider", "model", "effort", "autocompact") if agents[n].get(k)}
                                             | ({"upto": ranges[n][1]} if n != levels[-1] else {})
                                             for n in levels}}


def preset_profiles(target, profiles):
    """`profiles` with a preset's subagent applied. A router target sets the levels (model, effort, range)
    and leaves the one-model profiles alone; a one-model target sets those and leaves the levels alone."""
    profiles = dict(profiles or {"sub": {}})
    if target and target.get("provider") == "router" and isinstance(target.get("levels"), dict):
        for level, pick in target["levels"].items():
            old = profiles.get(level) or {}
            profiles[level] = {**{k: v for k, v in old.items() if k not in ("provider", "model", "effort", "upto", "autocompact")},
                               **pick, "description": old.get("description") or LEVEL_TEXT[level]}
        return profiles
    if not (target and target.get("provider") in PROVIDERS and target.get("model")):
        return None
    route = {"provider": target["provider"], "model": target["model"], **({"effort": target["effort"]} if target.get("effort") else {})}
    if not any(n not in ROUTER_LEVELS for n in profiles):
        profiles["sub"] = {}
    return {n: a if n in ROUTER_LEVELS else {**{k: v for k, v in a.items() if k not in ("provider", "model", "effort")}, **route}
            for n, a in profiles.items()}


def preset_agents(name):
    """Every subagent profile on the saved preset's subagent; None without a saved preset."""
    bundle = saved_bundle(name)
    return preset_profiles(bundle["subagent"], load_agents(session=False)) if bundle else None


def is_router_target(target):
    return isinstance(target, dict) and target.get("provider") == "router"


def use_preset_subagent(name):
    """ccx presets use-subagent <name>: this session's subagents from the preset (router or one model)."""
    if name not in PROVIDERS and name not in load_presets():
        die(f"Unknown preset '{name}'.")
    bundle = saved_bundle(name)
    target = bundle["subagent"] if bundle else default_router_target()
    agents = preset_profiles(target, load_agents())
    if agents is None:
        die(f"Preset '{name}' has no usable subagent.")
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        save_agents(agents)
    set_router(is_router_target(target))


def is_resume(claude_args):
    return any(a in ("--resume", "-r", "--continue", "-c") or a.startswith("--resume=") for a in claude_args)


def seed_launch_from_preset(name, launch, adapter, providers):
    """A new launch starts on its provider's saved preset: subagents, and on adapter routes the auto/tools helpers."""
    agents = preset_agents(name)
    # save_agents writes the global file outside a launch; only seed the launch it would edit
    if agents and session_launch() == launch and _session_agents(launch) is None:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            save_agents(agents)
            _set_session_flag("router", is_router_target(saved_bundle(name)["subagent"]), "router on|off")
    bundle = saved_bundle(name)
    if not bundle or not adapter:
        return
    data = load_routes()
    row = dict(data.get(launch) or {})
    if row.get("helpers"):
        return
    helpers = {k: {key: bundle[k][key] for key in ("provider", "model", "effort") if bundle[k].get(key)}
               for k in ("auto", "tools") if bundle[k].get("provider") in providers and bundle[k].get("model")}
    if helpers:
        data[launch] = {**row, "helpers": helpers, "at": time.time()}
        write_json(ROUTES_FILE, data, indent=1)


def presets_command(words):
    """List main-model picks or edit a complete five-category preset bundle."""
    if words == ["bundles"]:
        print(json.dumps([{"name": row["provider"], **row, "settings": preset_bundle(row)} for row in preset_targets()]))
        return
    if words[:1] == ["use-subagent"] and len(words) == 2:
        return use_preset_subagent(words[1].strip().lower())
    if words[:1] == ["set-bundle"] and len(words) == 3:
        name = words[1].strip().lower()
        if name not in PROVIDERS and not PRESET_NAME.fullmatch(name):
            die("A preset name is 1-32 letters, digits, '-' or '_'.")
        try:
            settings = json.loads(words[2])
        except ValueError:
            die("Invalid preset JSON.")
        if not isinstance(settings, dict) or set(settings) != set(PRESET_CATEGORIES):
            die("A preset must include model, subagent, fallback, auto and tools.")
        def check_target(key, target, allowed):
            if not isinstance(target, dict):
                die(f"Invalid {key} preset.")
            provider, model, effort = target.get("provider"), target.get("model"), target.get("effort", "default")
            if not isinstance(provider, str) or provider not in allowed:
                die(f"Invalid {key} provider.")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", model):
                die(f"Invalid {key} model id.")
            if effort != "default" and effort not in EFFORTS:
                die(f"Invalid {key} effort.")
            if effort != "default":
                levels = model_efforts(provider, model)
                if levels is not None and effort not in levels:
                    die(f"{provider}/{model} does not support effort {effort}.")
            return {"provider": provider, "model": model, **({"effort": effort} if effort != "default" else {})}

        cleaned = {}
        for key, target in settings.items():
            if not isinstance(target, dict):
                die(f"Invalid {key} preset.")
            provider = target.get("provider")
            if provider == "off" and key in ("fallback", "auto", "tools"):
                cleaned[key] = {"provider": "off"}
                continue
            if provider == "router" and key == "subagent":
                levels = target.get("levels")
                order = [n for n in ROUTER_LEVELS if n in (levels or {})] if isinstance(levels, dict) else []
                if len(order) < 2 or len(order) != len(levels):
                    die(f"A router preset sets two or more of these levels: {', '.join(ROUTER_LEVELS)}.")
                picks = {level: check_target(level, levels[level], PROVIDERS) for level in order}
                for level in order:  # each level may keep its own compact window
                    compact = levels[level].get("autocompact")
                    if compact not in (None, "", "inherit"):
                        check_agents({level: {"autocompact": compact}})
                        picks[level]["autocompact"] = compact
                tops = [levels[level].get("upto") for level in order[:-1]]
                if not (all(type(t) is int for t in tops) and all(a < b for a, b in zip([-1, *tops], [*tops, 100]))):
                    die(f"Router ranges: give {', '.join(order[:-1])} rising tops between 0 and 99; "
                        f"{order[-1]} always reaches 100.")
                for level, top in zip(order, tops):
                    picks[level]["upto"] = top
                cleaned[key] = {"provider": "router", "levels": picks}
                continue
            allowed = FALLBACK_ROUTES if key == "fallback" else route_providers() if key in ("auto", "tools") else PROVIDERS
            cleaned[key] = check_target(key, target, allowed)
        if name in PROVIDERS and cleaned["model"]["provider"] != name:
            die("The main model must use the preset's provider.")
        saved = load_presets()
        saved[name] = {"model": cleaned["model"]["model"],
                       **({"effort": cleaned["model"]["effort"]} if cleaned["model"].get("effort") else {}),
                       "settings": cleaned}
        save_presets(saved)
        print(f"Preset saved: {name} (all five categories)")
        return
    if not words or words == ["list"]:
        for row in preset_targets():
            effort = row["effort"] or ""
            label = f'{row["provider"]} · {row["model"]}' + (f' · {row["effort"]}' if row["effort"] else "")
            print(f'{row["provider"]}\t{row["model"]}\t{effort}\t{label}\t{"1" if row.get("custom") else ""}')
        return
    if words[0] == "set" and len(words) in (3, 4):
        name, model = words[1].lower(), words[2]
        if name not in PROVIDERS:
            die(f"Unknown provider '{name}'. Valid: {', '.join(PROVIDERS)}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", model):
            die("Invalid model id.")
        effort = words[3].lower() if len(words) == 4 else "default"
        if effort != "default" and effort not in EFFORTS:
            die(f"Effort must be one of: {', '.join(['default', *EFFORTS])}.")
        saved = load_presets()
        previous = saved.get(name)
        settings = previous.get("settings") if isinstance(previous, dict) else None
        saved[name] = {"model": model, **({"effort": effort} if effort != "default" else {}),
                       **({"settings": settings} if isinstance(settings, dict) else {})}
        save_presets(saved)
        print(f"Preset: {name}/{model}" + (f", effort {effort}" if effort != "default" else ""))
        return
    if words[0] == "reset" and len(words) == 2:
        name = words[1].lower()
        saved = load_presets()
        if saved.pop(name, None) is None:
            die(f"No custom preset for '{name}'.")
        save_presets(saved)
        print(f"Preset {'reset' if name in PROVIDERS else 'deleted'}: {name}")
        return
    die("Usage: ccx presets [list | bundles | set <provider> <model> [effort] | set-bundle <provider|name> <json> | use-subagent <provider|name> | reset <provider|name>]")


def models_command(words):
    """ccx models <provider>: the provider's model ids, for the pickers' editors."""
    if len(words) != 1:
        die("Usage: ccx models <provider>")
    name = words[0].lower()
    if name not in PROVIDERS:
        die(f"Unknown provider '{name}'. Valid: {', '.join(PROVIDERS)}")
    ensure_key(name)
    for model in get_models(name):
        print(model["id"])


def model_facts_command(words):
    """ccx facts <provider>: one JSON row per model, the same facts the launch menu sorts by."""
    if len(words) != 1:
        die("Usage: ccx facts <provider>")
    name = words[0].lower()
    if name not in PROVIDERS:
        die(f"Unknown provider '{name}'. Valid: {', '.join(PROVIDERS)}")
    ensure_key(name)
    with catalog._loading(f"{name} model facts"):
        models = get_models(name)
        catalog._models_dev()
        catalog._artificial_analysis()
    for row in model_facts(name, models):
        print(json.dumps(row))


def model_facts(name, models=None):
    """One dict per model of `name`: id, ctx, in/out $ per M, aa (Artificial Analysis), score, missing."""
    if models is None:
        with catalog._loading(f"{name} model facts"):
            models = get_models(name)
            catalog._models_dev()
            catalog._artificial_analysis()
    facts_name = {"official": "claude", "commandcode-cli": "commandcode"}.get(name, name)
    infos = []
    for model in models:
        info = catalog._model_facts(facts_name, model["id"])
        if type(model.get("context")) is int and model["context"] > 0:
            info = {**info, "limit": {**(info.get("limit") or {}), "context": model["context"]}}
        infos.append(info)
    scales = catalog._scales(infos)
    rows = []
    for model, info, scale in zip(models, infos, scales):
        context = (info.get("limit") or {}).get("context")
        cost = info.get("cost") or {}
        rows.append({
            "id": model["id"],
            "ctx": context if type(context) is int and context > 0 else None,
            "in": cost.get("input") if "input" in cost else None,
            "out": cost.get("output") if "output" in cost else None,
            "aa": info.get("intelligence"),
            "aa_efforts": info.get("intelligence_efforts") or {},
            "score": round(float(scale.get("score") or 0) * 100, 1),
            "missing": catalog._missing(info),
        })
    return rows


def level_effort(level, levels):
    """A level's effort on its pick, from the efforts that model has: cheaper models think longer, the
    strongest needs less. On Claude's ladder: very easy and easy xhigh, mid high, hard medium, very hard high."""
    levels = [v for v in levels if v != "off"] or list(EFFORTS)
    wanted = {"very-easy": "xhigh", "easy": "xhigh", "mid": "high", "hard": "medium", "very-hard": "high"}[level]
    if wanted in levels:
        return wanted
    at = {"very-easy": len(levels) - 2, "easy": len(levels) - 2, "mid": len(levels) // 2, "hard": 1,
          "very-hard": len(levels) // 2}[level]  # a ladder without that name
    return levels[max(0, min(at, len(levels) - 1))]
LEVEL_TEXT = {"very-easy": "Very easy tasks: one-step lookups, reading a value, trivial one-line edits.",
              "very-hard": "Very hard tasks: architecture calls and bugs where a wrong answer is costly.",
              "easy": "Easy tasks: lookups, counting, renames, simple mechanical edits.",
              "mid": "Medium tasks: ordinary feature work, single-file edits, moderate analysis.",
              "hard": "Hard tasks: multi-file design, subtle bugs, tricky reasoning."}


# Each level's pick: the cheapest (model, effort) pair that reaches this share of the provider's best AA
# score; very hard takes the best pair outright.
LEVEL_AA_SHARE = {"very-easy": 0.5, "easy": 0.65, "mid": 0.8, "hard": 0.9}
# judgement, not data: AA prices tokens, not efforts, and a higher effort writes more of them
EFFORT_COST = {"off": 0.4, "minimal": 0.5, "low": 0.6, "medium": 0.8, "high": 1.0, "xhigh": 1.4, "max": 1.8}


def best_levels(rows, efforts=None):
    """{level: (model id, effort or None)} from model_facts rows. Candidates are every (model, effort)
    with an AA score for that effort (`aa_efforts`; else the model's AA at its default effort), priced at
    in+out $/M times EFFORT_COST. `efforts(model)` lists the efforts a model takes (None: any)."""
    pairs = []
    for r in rows:
        if r.get("aa") is None or r.get("in") is None or r.get("out") is None:
            continue
        allowed = efforts(r["id"]) if efforts else None
        scored = {e: aa for e, aa in (r.get("aa_efforts") or {}).items()
                  if e != "default" and (allowed is None or e in allowed)}
        if not scored:
            scored = {None: r["aa"]}
        for effort, aa in scored.items():
            pairs.append((aa, (r["in"] + r["out"]) * EFFORT_COST.get(effort, 1.0), r["id"], effort))
    if not pairs:
        return {}
    top = max(pairs, key=lambda p: (p[0], -p[1]))
    picks = {"very-hard": top}
    for level, share in LEVEL_AA_SHARE.items():
        picks[level] = min((p for p in pairs if p[0] >= top[0] * share), key=lambda p: (p[1], -p[0]))
    return {level: (picks[level][2], picks[level][3]) for level in ROUTER_LEVELS}


def set_best_levels(provider):
    """ccx agents router best [provider]: the levels on `provider`'s best picks (default: the sub profile's)."""
    agents = load_agents()
    provider = (provider or (agents.get("sub") or {}).get("provider") or "official").lower()
    if provider not in PROVIDERS:
        die(f"Unknown provider '{provider}'.")
    picks = best_levels(model_facts(provider), lambda model: model_efforts(provider, model))
    if not picks:
        die(f"No {provider} model has both an AA score and a price; set the levels by hand.")
    efforts = {level: effort or level_effort(level, model_efforts(provider, model) or EFFORTS)
               for level, (model, effort) in picks.items()}
    picks = {level: model for level, (model, _) in picks.items()}
    for level, model in picks.items():
        agents[level] = {**{k: v for k, v in (agents.get(level) or {}).items() if k not in ("provider", "model", "effort")},
                         "provider": provider, "model": model, "effort": efforts[level],
                         "description": (agents.get(level) or {}).get("description") or LEVEL_TEXT[level]}
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        save_agents(agents)
    print(f"Best {provider} picks: " + ", ".join(f"{level} {model} {efforts[level]}" for level, model in picks.items()))


def process_alive(pid):
    """Whether a PID is still running (Windows: its exit code is STILL_ACTIVE; elsewhere: signal 0)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def gui_lock_path():
    """The pid file ccx-gui.py writes for this session (same naming rule as its lock_file)."""
    key = os.environ.get("CCX_LAUNCH_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "default"
    safe = "".join(ch for ch in key if ch.isalnum() or ch in "-_")[:60] or "default"
    return os.path.join(CFG_DIR, f"gui-{safe}.pid")


def gui_lock_pid(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def gui_command(words):
    """ccx gui [model|subagent|fallback|auto|tools|presets]: the picker as its own window."""
    tabs = ("model", "subagent", "fallback", "auto", "tools", "presets")
    tab = (words[0].lower() if words else "model")
    tab = "fallback" if tab == "fallbacks" else tab
    if len(words) > 1 or tab not in tabs:
        die("Usage: ccx gui [model|subagent|fallback|auto|tools|presets]")
    script = os.path.join(CFG_DIR, "ccx-gui.py")
    if not os.path.isfile(script):
        die(f"Missing {script}; reinstall ccx.")
    python = sys.executable
    if os.name == "nt":
        windowed = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(windowed):
            python = windowed  # no console window behind the picker
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log = os.path.join(CFG_DIR, "gui-error.log")
    try:  # a stale failure from a previous launch must not name this one's cause
        log_before = os.path.getmtime(log)
    except OSError:
        log_before = None
    try:
        proc = subprocess.Popen([python, "-B", script, "--tab", tab], cwd=CFG_DIR,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True, start_new_session=os.name != "nt")
    except OSError as exc:
        die(f"Could not open the picker window: {exc}")
    # A detached child cannot report a Tk or startup failure through its exit code. The window
    # registers its pid first, so wait for that: the launcher then tells the mod the truth, and
    # the mod keeps its in-terminal grid when no window can open.
    lock, deadline = gui_lock_path(), time.time() + 5.0
    while time.time() < deadline:
        if process_alive(gui_lock_pid(lock)):
            time.sleep(0.15)  # a Tk failure unlinks the lock and exits just after claiming it
            if proc.poll() is None and process_alive(gui_lock_pid(lock)):
                print(f"Opened the model picker window ({tab}).")
                return
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    # A slow start (a busy PC, antivirus scanning python) is still a window on its way: a failure exits.
    # Reporting it as missing made the mod open its in-terminal grid beside the window.
    if proc.poll() is None or process_alive(gui_lock_pid(lock)):
        print(f"Opened the model picker window ({tab}).")
        return
    detail = ""
    try:
        if os.path.getmtime(log) != log_before:
            with open(log, encoding="utf-8") as f:
                detail = (f.read().strip().splitlines() or [""])[-1]
    except OSError:
        pass
    code = proc.poll()
    die(f"Could not open the picker window: {detail or (f'ccx-gui.py exited {code}' if code is not None else 'it did not start')}")


def default_effort(name, model_id):
    """Launcher defaults, independent of another provider's persisted /effort."""
    main = (saved_bundle(name) or {}).get("model") or {}
    if main.get("effort") and (model_id or "").casefold() == str(main.get("model", "")).casefold():
        return main["effort"]
    if name == "openai-oauth" and (model_id or "").casefold() == "gpt-6.1-sol":
        return "high"
    return None


def model_efforts(name, model_id):
    """Reasoning efforts this provider publishes for the model; None when it publishes none.

    The status line maps a level's circles and colour onto this list, so `high` on a
    model that tops out there reads as its maximum rather than three fifths of one.
    """
    if name in ("commandcode", "commandcode-cli"):
        return commandcode_efforts(model_id)
    if name == "openai-oauth":
        return codex_efforts(model_id)
    return None


def commandcode_plan_models():
    """This plan's model ids, casefolded; None when that cannot be determined.

    The billing catalog and the route catalogs spell some ids differently
    (`moonshotai/Kimi-K3` vs `moonshotai/kimi-k3`), so callers compare casefolded.
    """
    cached = _PLAN_CACHE.get("value")
    if cached and time.time() - cached[0] < PLAN_TTL:
        return cached[1]
    try:
        rows = commandcode_catalog()
        if rows is None:
            raise ValueError("no catalog")
        headers = {"Authorization": "Bearer " + auth_key("commandcode")}
        subscription = fetch(f"{COMMANDCODE_API}/alpha/billing/subscriptions", headers)["data"]
        credits = fetch(f"{COMMANDCODE_API}/alpha/billing/credits", headers).get("credits") or {}
        name, status = subscription.get("planId"), subscription.get("status")
        if name not in CCX_PLANS or status not in CCX_ACTIVE_SUBSCRIPTIONS:
            # cmdc trusts only an active subscription; anything else allows every model.
            result = None
        elif credits.get("purchasedCredits") or credits.get("freeCredits"):
            result = {m["id"].casefold() for m in rows if not m.get("hidden")}
        else:
            rank = CCX_PLANS.index(name)
            result = {m["id"].casefold() for m in rows if commandcode_model_allowed(m, name, rank)}
    except Exception:
        result = None  # no data: keep every advertised model rather than hide working ones
    _PLAN_CACHE["value"] = (time.time(), result)
    return result


def get_models(name):
    """`name`'s models: a listing under a day old from disk (refetched in the background every half hour, so
    models added during the day show up), else a live fetch that refills the cache."""
    entry = _models_cache().get(name) if MODELS_TTL > 0 else None
    now = time.time()
    if entry and now - entry.get("at", 0) < MODELS_TTL:
        if now - entry.get("checked", entry["at"]) >= MODELS_RECHECK:
            _refresh_models_later(name)
        return entry["models"]
    models, live = fetch_models(name)
    if live:
        _save_models(name, models)
    return models


def _models_cache():
    try:
        with open(MODELS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("models"), list)}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_models(name, models, **fields):
    """One provider's entry, merged into the shared file (written whole, then swapped in)."""
    data = _models_cache()
    now = time.time()
    data[name] = {**data.get(name, {}), "at": now, "checked": now, "models": models, **fields} if models is not None \
        else {**data.get(name, {}), **fields}
    os.makedirs(os.path.dirname(MODELS_CACHE_FILE), exist_ok=True)
    tmp = f"{MODELS_CACHE_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, MODELS_CACHE_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _refresh_models_later(name):
    """A detached `ccx models-refresh <name>`; marked first, so other callers in the meantime don't start one too."""
    _save_models(name, None, checked=time.time())
    try:
        subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "models-refresh", name],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                         if os.name == "nt" else 0, start_new_session=os.name != "nt")
    except OSError:
        pass


def fetch_models(name):
    """(models, live): the provider's listing now; live is False when the fetch failed and the
    offline fallback (never cached) stands in."""
    models, filtered, failed = [], False, False
    try:
        if name == "official":
            models = [{"id": mid, "name": "", "context": None, "output": None} for mid in catalog._provider_models("claude")]
        elif name == "commandcode":
            # Claude ids use /messages; the rest route through the adapter's /chat/completions.
            # Models serving neither (e.g. /responses only) cannot work from Claude Code.
            live = fetch("https://api.commandcode.ai/provider/v1/models", {"Authorization": "Bearer " + auth_key(name)})
            allowed = commandcode_plan_models()
            filtered = allowed is not None
            efforts = {m["id"].casefold(): m.get("reasoningEfforts") for m in commandcode_catalog() or ()
                       if isinstance(m, dict) and isinstance(m.get("id"), str)}
            for m in live["data"]:
                if allowed is not None and m["id"].casefold() not in allowed:
                    continue
                if {"/messages", "/chat/completions"} & set(m.get("supported_endpoints", [])):
                    models.append({"id": m["id"], "name": m.get("name", ""), "context": m.get("context_window") or m.get("context_length"),
                                   "output": None, "efforts": efforts.get(m["id"].casefold())})
        elif name == "commandcode-cli":
            # The plan applies to both Command Code routes. cmdc's own ids are the ones it
            # accepts, so filter that listing rather than the billing catalog's spelling.
            allowed = commandcode_plan_models()
            filtered = allowed is not None
            ids = sorted(catalog._live_model_ids("commandcode") or ())
            if allowed is not None:
                ids = [mid for mid in ids if mid.casefold() in allowed]
            efforts = {m["id"].casefold(): m.get("reasoningEfforts") for m in commandcode_catalog() or ()
                       if isinstance(m, dict) and isinstance(m.get("id"), str)}
            models = [{"id": mid, "name": "", "context": None, "output": None,
                       "efforts": efforts.get(mid.casefold())} for mid in ids]
        elif name == "opencode-go":
            live = fetch("https://opencode.ai/zen/go/v1/models", {"x-api-key": auth_key("opencode-go")})
            limits = go_limits()
            for m in live["data"]:
                lim = limits.get(m["id"], {})
                models.append({"id": m["id"], "name": "", "context": lim.get("context"), "output": lim.get("output")})
            models.sort(key=lambda m: m["output"] or 0, reverse=True)
        elif name == "opencode-zen":
            live = fetch("https://opencode.ai/zen/v1/models", {"x-api-key": auth_key("opencode")})
            for m in live["data"]:
                if re.search(r"image|tts|embed", m["id"]):
                    continue
                models.append({"id": m["id"], "name": "", "context": m.get("context_window") or m.get("context_length"), "output": None})
        elif name == "openrouter":
            live = fetch("https://openrouter.ai/api/v1/models")
            for m in live["data"]:
                if re.search(r"image|tts|embed", m["id"]):
                    continue
                models.append({"id": m["id"], "name": m.get("name", ""),
                               "context": m.get("context_length"), "output": m.get("top_provider", {}).get("max_completion_tokens")})
        elif name == "nvidia":
            live = fetch("https://integrate.api.nvidia.com/v1/models",
                         {"Authorization": f"Bearer {auth_key('nvidia')}"})
            for m in live["data"]:
                if re.search(r"image|tts|embed", m["id"]):
                    continue
                models.append({"id": m["id"], "name": "", "context": m.get("context_window") or m.get("context_length"), "output": None})
        elif name in ("gemini", "ollama-cloud", "lmstudio") or PROVIDERS[name].get("custom"):
            p = PROVIDERS[name]
            key = auth_key(p["key"]) if p["key"] else ""
            headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if p["format"] == "anthropic" and p.get("header")
                       else {"Authorization": f"Bearer {key}"} if key else {})
            live = fetch(p["base"] + ("/v1/models" if p["format"] == "anthropic" else "/models"), headers)
            for m in live["data"]:
                if re.search(r"image|tts|embed|imagen|veo|aqa", m["id"]):
                    continue
                # Gemini lists ids as models/<id>; its chat endpoint takes the bare id.
                models.append({"id": m["id"].removeprefix("models/"), "name": "", "context": m.get("context_length"), "output": None})
        elif name == "openai-oauth":
            ensure_oauth_proxy()
            live = fetch(f"http://127.0.0.1:{OAUTH_PORT}/v1/models", timeout=10)
            # OAuth route limits can differ from the public API model limits.
            try:
                codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
                with open(os.path.join(codex_home, "models_cache.json"), encoding="utf-8") as f:
                    limits = {m["slug"]: m.get("max_context_window") or m.get("context_window")
                              for m in json.load(f)["models"]}
            except (OSError, ValueError, KeyError, TypeError):
                limits = {}
            for m in live["data"]:
                if re.search(r"image|tts|embed", m["id"]):
                    continue
                models.append({"id": m["id"], "name": "", "context": m.get("context_window") or limits.get(m["id"]), "output": m.get("max_output_tokens")})
    except Exception as e:
        failed = True
        print(f"  (model fetch failed: {e} - using fallback)", file=sys.stderr)

    live = not failed and bool(models or filtered)
    if not models and not filtered:  # offline fallback must not re-offer plan-locked models
        models = [{"id": i, "name": n, "context": None, "output": None} for i, n in FALLBACK_MODELS.get(name, [])]
    return models, live


def ensure_oauth_proxy():
    try:
        ensure_openai_oauth_proxy()
    except RuntimeError as exc:
        die(str(exc))


def stop_port_owner(port):
    """Kill the process listening on 127.0.0.1:<port> and wait for the port to free."""
    if os.name != "nt":
        import signal
        out = subprocess.run(["lsof", "-t", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        for pid in out.split():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid), signal.SIGTERM)
    else:
        stop_windows_port_owner(port)
    for _ in range(30):
        try:
            fetch(f"http://127.0.0.1:{port}/health", timeout=0.5)
        except (OSError, ValueError):
            return
        time.sleep(0.1)
    die(f"Could not stop the outdated adapter on port {port}; end that process and retry.")


def stop_windows_port_owner(port):
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    pids = {parts[4] for parts in map(str.split, out.splitlines())
            if len(parts) == 5 and parts[1] == f"127.0.0.1:{port}" and parts[3] == "LISTENING"} - {"0"}
    if not pids and port == ADAPTER_PORT:
        # Windows can report the listener as PID 0; find the adapter by its command line instead.
        query = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                 "Where-Object { $_.CommandLine -match 'ccx-anthropic-proxy[.]py' } | ForEach-Object { $_.ProcessId }")
        pids = set(subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.split())
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


@contextlib.contextmanager
def adapter_start_lock():
    """One launcher replaces the shared adapter; other sessions wait and recheck health."""
    if os.name != "nt":
        import fcntl
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(os.path.join(CFG_DIR, 'adapter-start.lock'), 'a+b') as lock:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        die('Another ccx launcher is still starting the adapter; retry shortly.')
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return
    import msvcrt
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(os.path.join(CFG_DIR, 'adapter-start.lock'), 'a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic() + 30
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    die('Another ccx launcher is still starting the adapter; retry shortly.')
                time.sleep(0.1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_anthropic_proxy():
    with adapter_start_lock():
        return _ensure_anthropic_proxy()


def _ensure_anthropic_proxy():
    url = f"http://127.0.0.1:{ADAPTER_PORT}/health"
    expected = {"service": "ccx-anthropic-proxy", "version": 35}  # unchanged: a bump restarts every session's adapter
    try:
        running = fetch(url, timeout=1)
    except (OSError, ValueError):
        running = None
    if running == expected:
        return
    # "cc-anthropic-proxy": the same adapter from before the ccx rename.
    if isinstance(running, dict) and running.get("service") in (expected["service"], "cc-anthropic-proxy"):
        # An older adapter from a previous install: replace it. Sessions still on it drop.
        stop_port_owner(ADAPTER_PORT)
    elif running is not None:
        die(f"Port {ADAPTER_PORT} is occupied by another service; free it before retrying.")
    options = background_process_options()
    # Below normal: every cmdc the adapter starts inherits it, so workers never starve the desktop.
    options["creationflags"] = options.get("creationflags", 0) | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
    subprocess.Popen([sys.executable, os.path.join(CFG_DIR, "ccx-anthropic-proxy.py")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    for _ in range(40):
        try:
            if fetch(url, timeout=1) == expected:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    die(f"Claude Messages adapter did not start on port {ADAPTER_PORT}.")


# ---------------- menu ----------------
# Same arrow menus, colors and model facts as book writer (ai_suite.providers).

def _target_text(t):
    if not t or t.get("provider") in (None, "off"):
        return "off"
    return f'{t["provider"]}/{t.get("model", "")}' + (f' · {t["effort"]}' if t.get("effort") else "")


def preset_menu(default=None):
    """The launch's first screen: a saved preset launches at once; "edit" and "manual" lead on.
    None (the manual menus) off a console or with no saved preset."""
    saved = [n for n in load_presets() if n in PROVIDERS and saved_bundle(n)]
    if not saved:
        return None  # nothing saved yet: straight to the manual menus
    width = max(map(len, saved or [""]))
    rows = [(n, f"{n:<{width}}  {_target_text(saved_bundle(n)['model'])}  "
                + catalog._color(f"sub {_target_text(saved_bundle(n)['subagent'])}  fallback {_target_text(saved_bundle(n)['fallback'])}", "2"))
            for n in saved]
    rows += [("edit", "edit a preset…"), ("manual", "select manually…")]
    default = default if default in saved else load_state().get("provider")
    return catalog._arrow_menu("Preset", rows, default if default in saved else rows[0][0], name="Preset", back=True)


def edit_preset():
    """Main model, effort, fallback and subagent menus for one provider, saved as its preset (auto/tools kept)."""
    name = provider_menu(load_state().get("provider"), "preset")
    if name not in PROVIDERS:
        return
    cur = saved_bundle(name) or {}
    main_t = cur.get("model") or {}
    model = model_menu(name, main_t.get("model"))
    effort = effort_menu(main_t.get("effort") or default_effort(name, model) or "")
    fallback = fallback_menu(name, preset_fallback(name)) if name in FALLBACK_ROUTES else None
    sub_t = cur.get("subagent") or {}
    router = None
    if sum(n in load_agents() for n in ROUTER_LEVELS) > 1:
        mode = catalog._arrow_menu("Subagent", [("router", "router  levels by difficulty"), ("one", "one model")],
                                   "router" if is_router_target(sub_t) or not sub_t else "one", name="Subagent", back=True)
        if mode == "router":
            agents = load_agents()
            if is_router_target(sub_t):  # start from the preset's own levels (this launch's copy: CCX_LAUNCH_ID)
                agents = preset_profiles(sub_t, agents)
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    save_agents(agents)
            router_menu(agents)
            router = default_router_target(load_agents(), sub_t.get("levels") if is_router_target(sub_t) else None)
    if not router:
        sub_p = provider_menu(sub_t.get("provider") if not is_router_target(sub_t) and sub_t.get("provider") else name, "subagent")
        sub_m = model_menu(sub_p, sub_t.get("model") if sub_t.get("provider") == sub_p else None, "subagent")
        sub_e = effort_menu(sub_t.get("effort") or default_effort(sub_p, sub_m) or "")
    bundle = preset_bundle({"provider": name, "model": model, "effort": effort or None})
    target = lambda p, m, e: {"provider": p, "model": m, **({"effort": e} if e else {})}
    saved = load_presets()
    saved[name] = {"model": model, **({"effort": effort} if effort else {}), "settings": {
        "model": target(name, model, effort), "subagent": router or target(sub_p, sub_m, sub_e),
        "fallback": fallback or {"provider": "off"}, "auto": bundle["auto"], "tools": bundle["tools"]}}
    save_presets(saved)
    print(f"Preset saved: {name}")


def provider_menu(default=None, role="", skip="", only=None):
    """skip: label of a leading "skip" row (returned as "skip"); only: the providers offered."""
    state = load_state()
    if default not in PROVIDERS and not (skip and default == "skip"):
        default = state.get("provider") if not role and state.get("provider") in PROVIDERS else "official"
    names = sorted(only or PROVIDERS)
    width = max(map(len, names))
    tag = "  " + catalog._color("default", "2")
    picked = catalog._arrow_menu(f"{role.capitalize()} provider" if role else "Provider", [
        *([("skip", f"{'skip':<{width}}  {skip}")] if skip else []),
        *((n, f"{n:<{width}}  {PROVIDERS[n]['label']}{tag if n == default else ''}") for n in names)], default, back=True)
    if picked is None:  # not a console: type it
        try:
            picked = input(f"Provider ({', '.join(names)}) [default: {default}]: ").strip().lower() or default
        except EOFError:
            picked = default
    return picked


def model_menu(name, default=None, role=""):
    state = load_state()
    ensure_key(name)
    with catalog._loading(f"{name} models"):
        models = get_models(name)
        catalog._models_dev()
        catalog._artificial_analysis()
    ids = [m["id"] for m in models]
    if not ids:
        die(f"No {name} models available; check its login or network and retry.")
    catalog._refresh_missing_intelligence(ids)  # new AA scores show on the next menu
    if default not in ids:
        default = state.get("model") if not role and state.get("provider") == name and state.get("model") in ids else ids[0]
    facts = {"official": "claude", "commandcode-cli": "commandcode"}.get(name, name)
    rows = []
    for m in models:
        info = catalog._model_facts(facts, m["id"])
        if type(m.get("context")) is int and m["context"] > 0:  # the provider's own figure wins
            info = {**info, "limit": {**(info.get("limit") or {}), "context": m["context"]}}
        rows.append((m["id"], info))
    paid_by = "; your subscription pays" if name in ("official", "opencode-go", "openai-oauth", "commandcode-cli") else ""
    return catalog._pick_model(PROVIDERS[name]["label"], rows, default, role, paid_by, back=True)


def effort_menu(default=None):
    """Starting effort for the session ("" = Claude's default); /effort changes it later."""
    return catalog._prompt_effort(EFFORTS, load_state().get("effort", "") if default is None else default,
                                 back=True)


TOKENS_RE = r"\d+(\.\d+)?[km]"


def parse_pct(value):
    """An --autocompact value: a percentage 1-100 (int), a token window like "100k" or "1m" (str),
    or "" for "default"/"inherit" (a subagent then inherits its parent's limit)."""
    value = str(value).strip().lower().removesuffix("%")
    if value in ("default", "inherit", ""):
        return ""
    if re.fullmatch(TOKENS_RE, value) and compact_tokens(value) >= 1000:
        return value
    if not re.fullmatch(r"\d{1,3}", value) or not 1 <= int(value) <= 100:
        die(f"Auto-compact must be a percentage from 1 to 100, a token window like 100k, or 'inherit', not '{value}'.")
    return int(value)


def compact_tokens(value):
    """Tokens of a "100k"/"1.5m" window."""
    return int(float(value[:-1]) * (1000 if value[-1] == "k" else 1_000_000))


def compact_text(value, unset="inherit"):
    return unset if not value else f"{value}%" if type(value) is int else value


def compact_launch(env, args, value):
    """Apply an auto-compact limit to a Claude launch: a percentage through Claude's env override,
    a token window through --settings. Either stays in CCX_AUTOCOMPACT for its workers to inherit."""
    env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
    env.pop("CCX_AUTOCOMPACT", None)
    if type(value) is int:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(value)
    elif value:
        args += ["--settings", json.dumps({"autoCompactWindow": compact_tokens(value)})]
    if value:
        env["CCX_AUTOCOMPACT"] = str(value)


def parent_compact(context):
    """The limit a subagent without its own inherits: its parent's ccx limit, else the parent model's
    autoCompactWindow (or the global one) in Claude's settings."""
    own = os.environ.get("CCX_AUTOCOMPACT") or os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")
    if own:
        return int(own) if own.isdigit() else own
    try:
        with open(Path.home() / ".claude" / "settings.json", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return None
    model = None
    source = (context or {}).get("source", "")
    if source and Path(source).is_file():
        with open(source, "rb") as f:
            f.seek(max(0, Path(source).stat().st_size - 400_000))
            found = re.findall(rb'"model":"([^"]+)"', f.read())
        model = found[-1].decode() if found else None
    window = ((settings.get("modelSettings") or {}).get(model) or {}).get("autoCompactWindow") or settings.get("autoCompactWindow")
    return f"{window // 1000}k" if isinstance(window, int) and window >= 1000 else None


def autocompact_menu(default, role=""):
    """Auto-compact threshold as a percentage of the context window ("" = Claude's default)."""
    title = f"{role.capitalize()} auto-compact" if role else "Auto-compact"
    pcts = sorted({*AUTOCOMPACTS, *([default] if type(default) is int else [])})
    rows = ([("default", "inherit  the parent session's limit" if role == "subagent" else "default  Claude's own threshold")]
            + [(t, f"{t}  tokens") for t in COMPACT_TOKENS] + [(str(p), f"{p}%  of the context window") for p in pcts])
    picked = catalog._arrow_menu(title, rows, str(default or "default"), name=title, back=True)
    # Off a console the remembered pick stands, as for effort.
    return default if picked is None else parse_pct(picked)


def load_fallbacks():
    try:
        with open(FALLBACKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_fallback(launch, provider=None, model=None, effort=None):
    """Sets (or with no provider clears) a launch's fallback route; entries expire after 30 days."""
    data = {k: v for k, v in load_fallbacks().items() if time.time() - v.get("at", 0) < 30 * 86400}
    if provider:
        data[launch] = {"provider": provider, "model": model, **({"effort": effort} if effort else {}), "at": time.time()}
    else:
        data.pop(launch, None)
    write_json(FALLBACKS_FILE, data, indent=1)


def load_routes():
    try:
        with open(ROUTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sync_claude_effort(model, effort):
    """Claude Code's "thinking with <effort>" label reads its own settings, so mirror the /model effort there."""
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return  # never create or clobber an unreadable settings file
    entry = settings.setdefault("modelSettings", {}).setdefault(model, {})
    if entry.get("effortLevel") != effort:
        entry["effortLevel"] = effort
        write_json(CLAUDE_SETTINGS, settings, indent=2, ensure_ascii=False)


def set_route(launch, provider=None, model=None, effort=None):
    """Sets (or with no provider clears) this launch's provider/model override, and its effort."""
    data = {k: v for k, v in load_routes().items() if time.time() - v.get("at", 0) < 86400}
    helpers = data.get(launch, {}).get("helpers")
    if provider:
        effort = effort or default_effort(provider, model)
        data[launch] = {"provider": provider, "model": model, **({"effort": effort} if effort else {}), "at": time.time()}
        if helpers:
            data[launch]["helpers"] = helpers
        if provider == "official" and model and effort in ("low", "medium", "high", "xhigh"):
            sync_claude_effort(model, effort)
    elif helpers:
        data[launch] = {"helpers": helpers, "at": time.time()}
    else:
        data.pop(launch, None)
    write_json(ROUTES_FILE, data, indent=1)


def helper_command(words):
    """ccx helper <auto|tools> [inherit | <provider> <model> [effort]]; picker selections per launch."""
    if not words or words[0] not in ("auto", "tools"):
        die("Usage: ccx helper <auto|tools> [inherit | <provider> <model> [effort]]")
    kind, rest = words[0], words[1:]
    launch = os.environ.get("CCX_LAUNCH_ID")
    if not launch or not on_adapter():
        die("Helper routing needs a ccx adapter session; restart with ccx before choosing helper models.")
    data = load_routes()
    row = dict(data.get(launch) or {})
    helpers = dict(row.get("helpers") or {})
    if rest == ["inherit"]:
        helpers.pop(kind, None)
    elif len(rest) in (2, 3) and rest[0] in route_providers():
        provider, model = rest[:2]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", model):
            die("Invalid helper model id.")
        effort = route_effort(rest[2:])
        if effort and effort not in ("low", "medium", "high", "xhigh", "max", "off", "none", "minimal"):
            die("Invalid helper effort.")
        levels = model_efforts(provider, model) if effort else None
        if levels is not None and effort and effort not in levels:
            die(f"{provider}/{model} does not support effort {effort}.")
        helpers[kind] = {"provider": provider, "model": model, **({"effort": effort} if effort else {})}
    elif rest:
        die("Usage: ccx helper <auto|tools> [inherit | <provider> <model> [effort]]")
    if rest:
        row["helpers"], row["at"] = helpers, time.time()
        data[launch] = row
        write_json(ROUTES_FILE, data, indent=1)
    print_target("Helper", helpers.get(kind), "inherit session provider")


def on_adapter():
    """This session reaches the adapter: an adapter route, or `ccx official` passed through it."""
    return (os.environ.get("CCX_MAIN_PROVIDER") in FALLBACK_ROUTES
            or os.environ.get("ANTHROPIC_BASE_URL", "").endswith(f":{ADAPTER_PORT}/official"))


def route_providers():
    """The providers the adapter can switch this session to; Claude itself only on a `ccx official` session."""
    official = os.environ.get("ANTHROPIC_BASE_URL", "").endswith(f":{ADAPTER_PORT}/official")
    return [n for n in PROVIDERS if official or n != "official"]


def route_command(words):
    """ccx route [<provider> <model> [effort] | off | routes | models <provider>]: this session's model route."""
    routes = route_providers()
    if words == ["routes"]:
        for n in routes:
            print(f"{n}\t{PROVIDERS[n]['label']}")
        return
    if len(words) == 2 and words[0] == "models":
        if words[1] not in routes:
            die(f"Unknown adapter provider '{words[1]}'. Valid: {', '.join(routes)}")
        ensure_key(words[1])
        for m in get_models(words[1]):
            print(m["id"])
        return
    launch = os.environ.get("CCX_LAUNCH_ID")
    if not launch:
        die(f"This session has no ccx launch id; /model's Model tab needs a session started by ccx on one of: {', '.join(FALLBACK_ROUTES)}.")
    if not on_adapter():
        # The adapter reads x-ccx-launch; a direct Anthropic route never reaches it.
        die(f"This session connects to {os.environ.get('CCX_MAIN_PROVIDER') or 'its provider'} directly; "
            f"/model's Model tab works only on adapter routes: {', '.join(FALLBACK_ROUTES)}.")
    if words == ["off"]:
        set_route(launch)
    elif len(words) in (2, 3) and words[0].lower() in routes:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", words[1]):
            die("Invalid model id.")
        set_route(launch, words[0].lower(), words[1], route_effort(words[2:]))
    elif words:
        die(f"Usage: ccx route [<provider> <model> [effort] | off]; providers: {', '.join(routes)}")
    target = load_routes().get(launch)
    print_target("Route", target if target and target.get("model") else None, "launch default")


def route_effort(rest):
    """The optional effort word of `ccx route`/`ccx fallback`; `default` (or none) sends Claude Code's own."""
    effort = rest[0].lower() if rest else "default"
    if not re.fullmatch(r"[a-z0-9]+", effort):
        die("Invalid effort.")
    return None if effort == "default" else effort


def print_target(name, target, unset):
    print(f"{name}: {target['provider']}/{target['model']}" if target else f"{name}: {unset}")
    if target and target.get("effort"):
        print(f"Effort: {target['effort']}")


def preset_fallback(name):
    """The saved preset's fallback route for this provider, or None."""
    target = (saved_bundle(name) or {}).get("fallback") or {}
    if target.get("provider") in FALLBACK_ROUTES and target["provider"] != name and target.get("model"):
        return {"provider": target["provider"], "model": target["model"]}
    return None


def fallback_menu(name, current=None):
    """The fallback route for this launch: none unless picked (or the saved preset has one)."""
    enabled = catalog._arrow_menu("Add a fallback model?", [("no", "No" + ("" if current else " (default)")),
                                  ("yes", "Yes" + (" (preset)" if current else ""))], "yes" if current else "no", back=True)
    if enabled != "yes":
        return None
    provider = provider_menu((current or {}).get("provider") or "skip", "fallback",
                             "no fallback (default)", [n for n in FALLBACK_ROUTES if n != name])
    if provider in (None, "skip"):
        return None
    return {"provider": provider, "model": model_menu(provider, (current or {}).get("model"), "fallback")}


def fallback_command(words):
    """ccx fallback [<provider> <model> | off | routes | models <provider>]: this session's fallback route.
    routes and models print rows for the /fallback picker."""
    if words == ["routes"]:
        for n in FALLBACK_ROUTES:
            if n != os.environ.get("CCX_MAIN_PROVIDER"):
                print(f"{n}\t{PROVIDERS[n]['label']}")
        return
    if len(words) == 2 and words[0] == "models":
        if words[1] not in FALLBACK_ROUTES:
            die(f"Unknown fallback provider '{words[1]}'. Valid: {', '.join(FALLBACK_ROUTES)}")
        for m in get_models(words[1]):
            print(m["id"])
        return
    if words[:1] == ["subagent"]:
        if len(words) < 2:
            for name, profile in load_agents().items():
                target = profile.get("fallback")
                print(f"{name}: " + (f"{target['provider']}/{target['model']}" if target else "off"))
            return
        name, rest = words[1], words[2:]
        profiles = load_agents()
        if name not in profiles:
            die(f"No subagent '{name}'.")
        if rest == ["off"]:
            profiles[name].pop("fallback", None)
        elif len(rest) in (2, 3) and (rest[0] in FALLBACK_ROUTES or rest[0] == "official"):
            # official (Claude) can't be proxied mid-request; relay_job reruns a failed worker on it instead.
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", rest[1]):
                die("Invalid fallback model id.")
            profiles[name]["fallback"] = {"provider": rest[0], "model": rest[1], **({"effort": rest[2]} if len(rest) == 3 else {})}
        elif rest:
            die("Usage: ccx fallback subagent <name> [off | <provider> <model> [effort]]")
        if rest:
            save_agents(profiles)
        print_target("Fallback", profiles[name].get("fallback"), "off")
        return
    launch = os.environ.get("CCX_LAUNCH_ID")
    if not launch:
        die(f"This session has no ccx launch id; fallback needs a session started by ccx on one of: {', '.join(FALLBACK_ROUTES)}.")
    if not on_adapter():
        die(f"This session connects to {os.environ.get('CCX_MAIN_PROVIDER') or 'its provider'} directly, so there is "
            f"nothing to fall back from; fallback works on adapter routes: {', '.join(FALLBACK_ROUTES)}.")
    if words == ["off"]:
        set_fallback(launch)
    elif len(words) in (2, 3) and words[0].lower() in FALLBACK_ROUTES:
        set_fallback(launch, words[0].lower(), words[1], route_effort(words[2:]))
    elif words:
        die(f"Usage: ccx fallback [<provider> <model> [effort] | off]; providers: {', '.join(FALLBACK_ROUTES)}")
    print_target("Fallback", load_fallbacks().get(launch), "off")


def take_option(args, flag):
    """Remove `flag value` or `flag=value` from args and return the value (None if absent)."""
    for i, arg in enumerate(args):
        if arg == flag:
            if i + 1 == len(args):
                die(f"{flag} needs a value.")
            value = args[i + 1]
            del args[i:i + 2]
            return value
        if arg.startswith(flag + "="):
            del args[i]
            return arg.split("=", 1)[1]
    return None


# ---------------- apply ----------------

def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def sh_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def write_apply(env, delete, run_claude, claude_args=(), model=None):
    if os.name != "nt":
        # Sourced by the shell's ccx function. A launch runs in a subshell, so the provider
        # variables end with it and a later plain `claude` opens default Claude.
        lines = [f"unset {k}" for k in delete] + [f"export {k}={sh_quote(v)}" for k, v in env.items()]
        if run_claude:
            command = " ".join(["claude", *map(sh_quote, (["--model", model] if model else []) + list(claude_args))])
            lines = ["(", *lines, "printf 'Starting Claude Code...\\n'", command, ")"]
        with open(APPLY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return
    lines = [f'Remove-Item "Env:{k}" -ErrorAction SilentlyContinue' for k in delete]
    lines += [f"Set-Item 'Env:{k}' {ps_quote(v)}" for k, v in env.items()]
    if run_claude:
        # Scope the provider env vars to this launch, so a later plain `claude` opens default Claude.
        names = list(dict.fromkeys([*delete, *env]))
        saved = "; ".join(f"'{k}' = [Environment]::GetEnvironmentVariable('{k}')" for k in names)
        command = " ".join(["claude", *map(ps_quote, (["--model", model] if model else []) + list(claude_args))])
        lines = [f"$ccSaved = @{{{saved}}}", "try {", *lines,
                 'Write-Host "Starting Claude Code..." -ForegroundColor Gray',
                 "Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue",
                 command,
                 "} finally { foreach ($k in $ccSaved.Keys) { [Environment]::SetEnvironmentVariable($k, $ccSaved[$k]) } }"]
    with open(APPLY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def picker_models(name, model_id, models=None):
    """This provider's recent picks then its live models, for the /model picker.

    A live list is authoritative: a remembered or fallback id that is no longer usable
    (plan lock, retired model) stays out, and the live spelling wins over a stale casing.
    """
    live = {m["id"].casefold(): m["id"] for m in models or ()}
    ids = [*load_state().get("recent", {}).get(name, []),
           *(m["id"] for m in models or ()),
           *(i for i, _ in FALLBACK_MODELS.get(name, []))]
    # Command Code's Claude and other models live behind different base URLs.
    same_route = lambda m: name != "commandcode" or m.startswith("claude-") == model_id.startswith("claude-")
    picks = []
    for mid in dict.fromkeys(ids):
        if mid.casefold() == (model_id or "").casefold() or not same_route(mid):
            continue
        if live:
            if mid.casefold() not in live:
                continue
            mid = live[mid.casefold()]
        picks.append(mid)
    return picks


def go_messages(model_id):
    """True when OpenCode Go serves this model over Anthropic /messages (models.dev's npm)."""
    m = ((catalog._models_dev().get("opencode-go") or {}).get("models") or {}).get(model_id) or {}
    npm = (m.get("provider") or {}).get("npm")
    if npm:
        return npm == "@ai-sdk/anthropic"
    # shortcut: models.dev unreachable or the model unlisted; these families spoke /messages on 2026-10-20.
    return model_id.startswith(("claude-", "minimax-", "qwen"))


def route_env(name, model_id, passthrough=False):
    """Environment that points Claude Code at this provider and model; passthrough: an interactive
    official launch goes through the adapter (workers connect directly)."""
    p = PROVIDERS[name]
    ensure_key(name)
    if name != "official" and (not isinstance(model_id, str) or not model_id.strip()):
        die("A nonempty model ID is required.")
    # Command Code serves Claude over /messages and everything else over /chat/completions;
    # OpenCode Go likewise answers /messages only for some models.
    fmt = ("openai" if (name == "commandcode" and not model_id.startswith("claude-"))
           or (name == "opencode-go" and not go_messages(model_id)) else p["format"])
    env = {}
    if name == "official":
        # The normal claude.ai login, through the adapter, which hands it to Anthropic untouched until
        # /model routes the launch elsewhere. CCX_OFFICIAL_DIRECT=1, or no adapter, connects directly.
        if passthrough and os.environ.get("CCX_OFFICIAL_DIRECT") != "1":
            try:
                ensure_anthropic_proxy()
                env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{ADAPTER_PORT}/official"
                # The adapter passes it to Anthropic, so Claude Code keeps its first-party behaviour
                # (Haiku agent summaries, request classes); the adapter strips the extras on reroutes.
                env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] = "1"
            except (SystemExit, Exception) as exc:
                print(f"Adapter unavailable ({exc}); connecting to Anthropic directly.", file=sys.stderr)
    elif fmt == "anthropic":
        token = ""
        if p.get("key"):
            key = auth_key(p["key"])
            if not key:
                die(f"No key for {name}. Use commandcode auth login or CMD_API_KEY for Command Code; otherwise configure this provider in opencode or its API_KEY environment variable.")
            token = key
        env["ANTHROPIC_BASE_URL"] = p["base"]
        # A bearer token, unlike ANTHROPIC_API_KEY, avoids Claude's "Both claude.ai and
        # ANTHROPIC_API_KEY set" warning. x-api-key gateways also get the key as a header;
        # Claude then sends exactly one x-api-key, which OpenCode accepts beside the bearer.
        env["ANTHROPIC_AUTH_TOKEN"] = token
        if p.get("header") == "x-api-key":
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-api-key: {token}"
        if name == "opencode-go":
            # Go answers 400 MissingSessionID without it; it routes a session's turns together.
            env["ANTHROPIC_CUSTOM_HEADERS"] += f"\nx-opencode-session: {uuid.uuid4().hex}"
        if name == "opencode-zen":
            warn("opencode-zen bills the account balance (402 when empty); free models answer only the OpenCode client.")
        if name == "openrouter" and not model_id.startswith("anthropic/"):
            warn("Non-Claude models are experimental in Claude Code; usage is billed to OpenRouter, not your ChatGPT subscription.")
    else:
        key = "" if not p["key"] or p["key"] in ("oauth", "cli") else auth_key(p["key"])
        if name == "openai-oauth":
            ensure_oauth_proxy()
        elif name == "commandcode-cli":
            pass  # the adapter posts to /alpha/generate on the CLI's login (CCX_CMDC_TEXT=1: one cmdc run per request)
        elif not key and p["key"] and name != "lmstudio":  # LM Studio runs keyless unless auth is on
            die(f"No API key configured for {name}.")
        ensure_anthropic_proxy()
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{ADAPTER_PORT}/{name}"
        env["ANTHROPIC_AUTH_TOKEN"] = key or name  # the adapter reads bearer or x-api-key
        env["ENABLE_TOOL_SEARCH"] = "false"

    models = get_models(name) if name != "official" else []
    if name in ("opencode-go", "opencode-zen", "commandcode", "commandcode-cli"):
        # After the credentials: a missing key must fail without fetching the catalog.
        # Only typed ids can miss: the menu lists usable models only.
        known = {m["id"].casefold() for m in models}
        if known and model_id.casefold() not in known:
            warn(f"'{model_id}' is not in {name}'s usable model list - Claude Code may reject it.")

    if name != "official":
        # Gateways cannot perform Anthropic's server-side safety checks. Keep Claude's
        # existing client-side classifier and avoid the billing notice.
        env["CLAUDE_CODE_AUTO_MODE_SERVER"] = "0"
        # claude.ai connectors never load over a gateway; skip their "disabled" notice.
        env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        # Slow reasoning models and one cmdc run per request outlast Claude's 10-minute
        # request timeout; match the adapter's own 30-minute CLI limit.
        env["API_TIMEOUT_MS"] = "1800000"
        for v in ["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"]:
            env[v] = model_id
        env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"] = env["ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"] = model_id
        # ponytail: CLAUDE_CODE_MAX_CONTEXT_TOKENS stays the chosen model's; a /model pick
        # with a smaller window can overflow before auto-compaction.
        for slot, other in zip(PICKER_SLOTS, picker_models(name, model_id, models)):
            env[slot] = env[slot + "_NAME"] = other
        # Let Claude Code list the gateway's own models in /model (the adapter serves
        # /<route>/v1/models). Not on the direct Command Code gateway: it advertises
        # Claude models the plan locks, and its list cannot be filtered here.
        if name != "commandcode":
            env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        # Claude Code keeps /effort off for model ids it does not know; the adapter
        # forwards it as reasoning_effort, Anthropic-format gateways get it as sent.
        env["CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"] = "1"
        if name == "commandcode" and fmt == "anthropic":
            # Command Code rejects mid-conversation system roles; retain reasoning controls.
            for slot in ["ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", *PICKER_SLOTS]:
                if slot in env:
                    env[slot + "_SUPPORTED_CAPABILITIES"] = "thinking,adaptive_thinking,interleaved_thinking,effort,xhigh_effort,max_effort"
        context = next((m.get("context") for m in models if m["id"] == model_id), None)
        if not (type(context) is int and context > 0):  # the menu's models.dev figure
            facts = {"commandcode-cli": "commandcode"}.get(name, name)
            context = (catalog._models_dev_facts(facts, model_id).get("limit") or {}).get("context")
        if type(context) is int and context > 0:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = context
    return env


def apply(name, model_id=None, no_run=False, claude_args=(), effort=None, autocompact=None, fallback=None, launch=None):
    env = route_env(name, model_id, passthrough=True)
    # Every launch gets an id: subagent profiles and the optional route override are per session.
    env["CCX_LAUNCH_ID"] = launch = launch or os.environ.get("CCX_LAUNCH_ID") or uuid.uuid4().hex
    if name in FALLBACK_ROUTES or env.get("ANTHROPIC_BASE_URL", "").endswith("/official"):
        # Names this launch to the adapter, so /fallback and /model can change it mid-session.
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(filter(None, [env.get("ANTHROPIC_CUSTOM_HEADERS"), f"x-ccx-launch: {launch}"]))
        if fallback:
            set_fallback(launch, fallback["provider"], fallback["model"])
    env["CCX_MAIN_PROVIDER"] = name
    env["CLAUDE_CODE_FORK_SUBAGENT"] = "0"
    base = env.get("ANTHROPIC_BASE_URL", "")
    if not is_resume(claude_args):  # a resume takes its old session's settings back (link_session)
        seed_launch_from_preset(name, launch, adapter=name in FALLBACK_ROUTES or base.endswith(f":{ADAPTER_PORT}/official"),
                                providers=[n for n in PROVIDERS if n != "official" or base.endswith(f":{ADAPTER_PORT}/official")])
    if autocompact:
        claude_args = [*claude_args]
        compact_launch(env, claude_args, autocompact)
    run_claude = not no_run
    explicit_effort = any(a == "--effort" or a.startswith("--effort=") for a in claude_args)
    if effort is None and not explicit_effort:
        effort = default_effort(name, model_id)
    if effort and run_claude and not explicit_effort:
        claude_args = ["--effort", effort, *claude_args]

    state = load_state()
    recent = state.get("recent", {})
    if model_id:
        recent[name] = list(dict.fromkeys([model_id, *recent.get(name, [])]))[:8]
    save_state({**state, "provider": name, "model": model_id or "", "recent": recent, **({} if effort is None else {"effort": effort}),
                **({} if autocompact is None else {"autocompact": autocompact})})
    # Clear every switcher variable this route does not set, so no earlier choice leaks in.
    write_apply(env, [v for v in CCX_VARS + OAI_VARS if v not in env], run_claude, claude_args, model_id)
    print()
    print("Switched: " + (f"{name}/{model_id}" if model_id else name))
    print(f"Effective: ANTHROPIC_BASE_URL={env.get('ANTHROPIC_BASE_URL', '')}  "
          f"model={model_id or ''}  OPENAI_BASE_URL={env.get('OPENAI_BASE_URL', '')}  "
          f"context={env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS', 'Claude default')}  "
          f"effort={effort or 'Claude default'} (/effort changes it)  "
          f"autocompact={compact_text(autocompact, 'Claude default')}  "
          f"fallback={fallback['provider'] + '/' + fallback['model'] if fallback else 'off'} (/fallback changes it)")


# ---------------- subagents ----------------
# A Claude Code subagent shares its session's environment, so it cannot reach another
# provider or compact at another threshold. Each profile in agents.json instead runs as
# its own headless `claude -p`; `ccx agents` writes a native subagent that relays to it.

_STARTUP_LAUNCH = None  # the launch whose startup prompt runs in this process
_AGENT_OVERRIDE = None  # relay_job: the fallback profile a failed worker reruns on


def session_launch():
    """The launch a subagent edit belongs to: inside its Claude session or its startup prompt only.
    apply.ps1 leaves CCX_LAUNCH_ID in the terminal, and a later plain `ccx agents` edits agents.json."""
    launch = os.environ.get("CCX_LAUNCH_ID", "")
    if launch and (os.environ.get("CLAUDECODE") == "1" or launch == _STARTUP_LAUNCH):
        return launch
    # A Claude session not started by ccx still gets its own subagent setup, keyed by its session id.
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "") if os.environ.get("CLAUDECODE") == "1" else ""
    return f"claude-{sid}" if sid else ""


def _session_agents(launch=None):
    """This launch's subagent overrides, or None when it has not edited any."""
    launch = launch or session_launch()
    if not launch:
        return None
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            entry = json.load(f).get(launch)
    except (OSError, ValueError):
        return None
    own = entry.get("agents") if isinstance(entry, dict) and isinstance(entry.get("agents"), dict) else None
    if own is None:
        return None
    # Router levels added after this session saved its profiles still reach it, as set globally: the
    # adapter resolves a live worker's profile here, and a session without them failed every level call.
    try:
        with open(AGENTS_FILE, encoding="utf-8") as f:
            shared = json.load(f)
    except (OSError, ValueError):
        shared = {}
    return {**own, **{n: a for n, a in shared.items() if n in ROUTER_LEVELS and n not in own and isinstance(a, dict)}}


def _latest_session_agents():
    """The subagents the most recent launch picked, so the next startup prompt offers them again."""
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            entries = [e for e in json.load(f).values() if isinstance(e, dict) and isinstance(e.get("agents"), dict)]
    except (OSError, ValueError, AttributeError):
        return None
    return max(entries, key=lambda e: e.get("at", 0))["agents"] if entries else None


def link_session(sid):
    """Tags this launch's subagents with the Claude session id; a resume (new launch) takes them back."""
    launch = session_launch()
    if not (launch and sid):
        return
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    def kept(e):
        return isinstance(e, dict) and (isinstance(e.get("agents"), dict) or e.get("off") or "router" in e)
    entry = data.get(launch)
    if kept(entry):
        if sid in entry.setdefault("sessions", []):
            return
        entry["sessions"].append(sid)
    else:
        old = [e for e in data.values() if kept(e) and sid in e.get("sessions", ())]
        if not old:
            return
        last = max(old, key=lambda e: e.get("at", 0))
        data[launch] = {**{k: last[k] for k in ("agents", "off", "router") if k in last}, "sessions": [sid], "at": time.time()}
    write_json(SESSION_AGENTS_FILE, data, indent=1)


def subagents_off(launch=None):
    """True when this session turned subagents off: it works on the main model alone."""
    launch = launch or session_launch()
    if not launch:
        return False
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            entry = json.load(f).get(launch)
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(entry, dict) and bool(entry.get("off"))


def _set_session_flag(key, value, command):
    """One flag in this session's entry; None removes it. The profiles stay as they were."""
    launch = session_launch()
    if not launch:
        die(f"ccx agents {command} works only inside a Claude session.")
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    entry = data.get(launch) if isinstance(data.get(launch), dict) else {}
    entry = {k: v for k, v in entry.items() if k != key}
    data[launch] = {**entry, **({key: value} if value is not None else {}), "at": time.time()}
    write_json(SESSION_AGENTS_FILE, data, indent=1)


def set_subagents_off(off):
    """ccx agents off|on: this session only; its profiles stay as they were for when it turns them back on."""
    _set_session_flag("off", True if off else None, "on|off")
    print("Subagents off for this session: the main model does all the work." if off
          else "Subagents on for this session.")


# The router's levels: with the router on, the main model sends each task to the level that fits its
# difficulty instead of the default `sub` profile; off, it gets `sub` (and any other profile) only.
ROUTER_LEVELS = ("very-easy", "easy", "mid", "hard", "very-hard")


def router_on(launch=None):
    """This session's router switch: on unless the session turned it off (new sessions store off)."""
    launch = launch or session_launch()
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            entry = json.load(f).get(launch) if launch else None
    except (OSError, ValueError, AttributeError):
        entry = None
    # on unless this session turned it off: the router is the default subagent mode
    return not (isinstance(entry, dict) and entry.get("router") is False)


def set_router(on):
    _set_session_flag("router", bool(on), "router on|off")
    print(router_text())


def router_text():
    levels = [n for n in ROUTER_LEVELS if n in load_agents()]
    if not levels:
        return f"Router: no level profiles ({', '.join(ROUTER_LEVELS)}); add them with `ccx agents set <level> ...`."
    return (f"Router on: tasks go to {', '.join('ccx-' + n for n in levels)} by difficulty." if router_on()
            else "Router off: tasks go to ccx-sub.")


# Where each level's difficulty range ends by default: most work is easy, so the strong (costly)
# model only takes what is really hard. Very easy 0-30, easy 31-75, mid 76-89, hard 90-95, very hard
# 96-100 (main models score high: at 60/80 real tasks scored 66-85 and Haiku got none).
LEVEL_TOPS = {"very-easy": 30, "easy": 75, "mid": 89, "hard": 95}


def level_ranges(profiles):
    """{level: (low, high)} on the 0-100 difficulty scale, in level order. Each level runs up to its
    `upto` (else LEVEL_TOPS); the next starts one above, and the last reaches 100."""
    levels = [n for n in ROUTER_LEVELS if n in profiles]
    ranges, low = {}, 0
    for i, name in enumerate(levels):
        default = LEVEL_TOPS.get(name, round(100 * (i + 1) / len(levels)))
        high = 100 if i == len(levels) - 1 else profiles[name].get("upto", default)
        high = max(low, min(int(high), 100))
        ranges[name] = (low, high)
        low = min(high + 1, 100)
    return ranges


def range_text(ranges):
    return ", ".join(f"ccx-{n} {a}-{b}" for n, (a, b) in ranges.items())


def set_ranges(cuts):
    """ccx agents router ranges <top> ...: where each level's difficulty range ends, one top per level but the last."""
    agents = load_agents()
    levels = [n for n in ROUTER_LEVELS if n in agents]
    try:
        tops = [int(c) for c in cuts]
    except ValueError:
        die(f"Usage: ccx agents router ranges {' '.join(f'<top of {n}>' for n in levels[:-1])} (whole numbers, 0-100)")
    if len(tops) != len(levels) - 1 or not all(a < b for a, b in zip([-1, *tops], [*tops, 100])):
        die(f"Give {len(levels) - 1} rising tops between 0 and 99, one per level but the last "
            f"({', '.join(levels[:-1])}); the last level always reaches 100.")
    for name, top in zip(levels, tops):
        agents[name] = {**agents[name], "upto": top}
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        save_agents(agents)
    print("Difficulty ranges: " + range_text(level_ranges(agents)))


def router_log():
    return os.path.join(CFG_DIR, "router-log.jsonl")


def route_score(description):
    """The main model's difficulty score from a router call's "[35] ..." description, or None."""
    m = re.match(r"\s*\[(\d{1,3})\]", str(description or ""))
    return int(m.group(1)) if m and int(m.group(1)) <= 100 else None


def level_for(score, ranges):
    return next((n for n, (low, high) in ranges.items() if low <= score <= high), list(ranges)[-1])


def listed_types(transcript_path):
    """The subagent types this Claude Code session knows (its transcript's agent listings), or None
    when unreadable: profiles added after it started stay unknown until /reload-plugins or a restart."""
    known = None
    try:
        with open(transcript_path, "rb") as handle:
            for line in handle:
                if b'"agent_listing_delta"' not in line:
                    continue
                listing = json.loads(line).get("attachment") or {}
                known = (known or set()) | set(listing.get("addedTypes") or [])
                known -= set(listing.get("removedTypes") or [])
    except (OSError, TypeError, ValueError, AttributeError):
        return None
    return known


def usable_level(level, ranges, known):
    """`level`, or the nearest level this session knows (the stronger on a tie) when it does not know it yet."""
    order = list(ranges)
    usable = [n for n in order if known is None or {"ccx-" + n, f"ccx-{n}-live"} & known]
    if not usable or level in usable:
        return level
    at = order.index(level)
    return min(usable, key=lambda n: (abs(order.index(n) - at), -order.index(n)))


def log_route(level, description):
    """Keep the main model's score for a router call: `router calibrate` reads them."""
    score = route_score(description)
    if score is None:
        return
    try:
        with open(router_log(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": round(time.time()), "launch": session_launch(), "level": level,
                                "score": score}) + "\n")
    except OSError:
        pass


CALIBRATE_MIN = 20  # scored calls needed before shares mean anything
CALIBRATE_LAST = 500  # recent calls only: the score habits of older main models fade out


def calibrate_ranges(shares):
    """ccx agents router calibrate <easy %> <mid %>: tops chosen so those shares of the logged scores
    fall in each level (RouteLLM-style: pick how much goes to the strong model, derive the cutoff)."""
    levels = [n for n in ROUTER_LEVELS if n in load_agents()]
    usage = f"Usage: ccx agents router calibrate {' '.join(f'<{n} %>' for n in levels[:-1])} (whole numbers, sum under 100)"
    try:
        pcts = [int(x) for x in shares]
    except ValueError:
        die(usage)
    if len(levels) < 2 or len(pcts) != len(levels) - 1 or min(pcts) <= 0 or sum(pcts) >= 100:
        die(usage)
    scores = []
    try:
        with open(router_log(), encoding="utf-8") as f:
            for line in f:
                try:
                    scores.append(int(json.loads(line)["score"]))
                except (ValueError, KeyError, TypeError):
                    pass
    except OSError:
        pass
    scores = sorted(scores[-CALIBRATE_LAST:])
    if len(scores) < CALIBRATE_MIN:
        die(f"Only {len(scores)} scored router calls logged; calibrate needs {CALIBRATE_MIN}. "
            "Use the router a while (ccx agents router on), or set tops by hand with `router ranges`.")
    tops, cum, prev = [], 0, -1
    for i, pct in enumerate(pcts):
        cum += pct
        q = scores[max(0, -(-len(scores) * cum // 100) - 1)]
        prev = min(max(q, prev + 1), 99 - (len(pcts) - 1 - i))  # rising, and room left for the levels above
        tops.append(prev)
    set_ranges([str(t) for t in tops])
    low, got = -1, []
    for n, top in zip(levels, [*tops, 100]):
        got.append(f"{n} {round(100 * sum(low < x <= top for x in scores) / len(scores))}%")
        low = top
    print(f"From {len(scores)} logged scores: " + ", ".join(got) + " (ties keep shares inexact).")


def offered_agents():
    """The profiles the main model may call now: the router's levels or `sub`, never both."""
    profiles = load_agents()
    if not any(n in ROUTER_LEVELS for n in profiles):
        return profiles
    on = router_on()
    kept = {n: a for n, a in profiles.items() if (n != "sub" if on else n not in ROUTER_LEVELS)}
    return kept or profiles


def all_agent_names():
    """Profiles that some session or the global file still uses; none may be deleted."""
    names = set()
    try:
        with open(AGENTS_FILE, encoding="utf-8") as f:
            names.update(json.load(f))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
            for entry in json.load(f).values():
                if isinstance(entry, dict) and isinstance(entry.get("agents"), dict):
                    names.update(entry["agents"])
    except (OSError, ValueError, AttributeError):
        pass
    return names


def load_agents(session=True):
    if session:
        overrides = _session_agents()
        if overrides is not None:
            return check_agents(overrides)
    try:
        with open(AGENTS_FILE, encoding="utf-8") as f:
            agents = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        die(f"{AGENTS_FILE} is not valid JSON: {e}")
    return check_agents(agents)


def check_agents(agents):
    for agent, a in agents.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", agent):
            die(f"Subagent name '{agent}' must use lowercase letters, digits and hyphens.")
        if a.get("provider", "official") not in PROVIDERS:
            die(f"Subagent '{agent}': unknown provider '{a.get('provider')}'.")
        if a.get("effort") not in (None, *EFFORTS):
            die(f"Subagent '{agent}': effort must be one of {', '.join(EFFORTS)}.")
        upto = a.get("upto")
        if upto is not None and not (type(upto) is int and 0 <= upto <= 100):
            die(f"Subagent '{agent}': upto (its difficulty range's top) must be a whole number from 0 to 100.")
        pct = a.get("autocompact")
        if pct is not None and not ((type(pct) is int and 1 <= pct <= 100)
                                    or (isinstance(pct, str) and re.fullmatch(TOKENS_RE, pct))):
            die(f"Subagent '{agent}': autocompact must be a percentage from 1 to 100 or a token window like 100k.")
        model = a.get("model")
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:\-@]*", model)):
            die(f"Subagent '{agent}': invalid model id '{model}'.")
        if not isinstance(a.get("args", []), list) or not all(isinstance(x, str) for x in a.get("args", [])):
            die(f"Subagent '{agent}': args must be a list of Claude arguments.")
    tops = [agents[n]["upto"] for n in ROUTER_LEVELS[:-1] if n in agents and agents[n].get("upto") is not None]
    if any(a >= b for a, b in zip(tops, tops[1:])) or any(t >= 100 for t in tops):
        die("Router levels' upto values must rise level by level and stay below 100; fix them with "
            "`ccx agents router ranges <top> ...`.")
    return agents


def agent_summary(a):
    return (f"{a.get('provider', 'official')}/{a.get('model') or 'Claude default'}, effort {a.get('effort') or 'default'}, "
            f"autocompact {compact_text(a.get('autocompact'))}")


def agent_options(args):
    """Leading --provider/--model/--effort/--autocompact/--description options, and the rest."""
    args, opts = list(args), {}
    while args and args[0].split("=", 1)[0] in AGENT_FLAGS:
        flag = args[0].split("=", 1)[0]
        opts[flag[2:]] = take_option(args, flag)
    return opts, args


def with_options(a, opts):
    """Profile `a` with option values applied; "default" removes model, effort or autocompact."""
    a = dict(a)
    for key, value in opts.items():
        if key == "autocompact":
            value = parse_pct(value)
        if value in ("", "default") and key != "description":
            a.pop(key, None)
        else:
            a[key] = value.lower() if key in ("provider", "effort") else value
    if "provider" in opts and "model" not in opts:
        a.pop("model", None)  # another provider's model id would not resolve
    return a


def agent_models(name=None, provider=None):
    """Every model the profile's provider (or `provider`) offers, marking the profile's current pick."""
    agents = load_agents()
    if name is None:  # the slash command with no subagent named: list the profiles
        if not agents:
            print(f"No subagent profiles in {AGENTS_FILE}.")
        for n, a in agents.items():
            print(f"{n}: {agent_summary(a)}")
        return
    a = agents.get(name)
    if a is None:
        die(f"No subagent '{name}' in {AGENTS_FILE}.")
    current = a.get("model")
    print(f"{name}: {agent_summary(a)}")
    if provider is not None and provider not in PROVIDERS:
        die(f"Unknown provider '{provider}'.")
    for m in get_models(provider or a.get("provider", "official")):
        print(f"  {'*' if m['id'].casefold() == (current or '').casefold() else ' '} {m['id']}")


def save_agents(agents):
    check_agents(agents)
    os.makedirs(CFG_DIR, exist_ok=True)
    launch = session_launch()
    if launch:
        # The startup prompt and the in-session slash commands edit only this launch.
        try:
            with open(SESSION_AGENTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data = {k: v for k, v in data.items() if time.time() - (v.get("at", 0) if isinstance(v, dict) else 0) < 30 * 86400}
        entry = data.get(launch) if isinstance(data.get(launch), dict) else {}
        data[launch] = {**entry, "agents": agents, "at": time.time()}
        write_json(SESSION_AGENTS_FILE, data, indent=1)
    else:
        write_json(AGENTS_FILE, agents, indent=2)
    sync_agents()


def subagents_menu():
    """Edit subagent profiles from the startup prompt; Enter on "done" keeps them."""
    agents = load_agents()
    drafts = {}
    selected = "done"
    while True:
        rows = ([("done", "done  keep these subagents")] + [(n, f"{n}  {agent_summary(a)}") for n, a in agents.items()]
                + [("add", "add  a new subagent")])
        picked = catalog._arrow_menu("Subagents", rows, selected, name="Subagents", back=True)
        if picked in (None, "done"):
            return
        selected = picked
        adding = picked == "add"
        if adding:
            picked = input("New subagent name (lowercase letters, digits, hyphens): ").strip().lower()
            if not picked:
                continue
        while True:
            if not adding:
                try:
                    action = catalog._arrow_menu(picked, [("edit", "edit"), ("remove", "remove")],
                                                 "edit", name=picked, back=True)
                except catalog.MenuBack:
                    break
                if action == "remove":
                    del agents[picked]
                    drafts.pop(picked, None)
                    selected = "done"
                    save_agents(agents)
                    break
            try:
                edit_agent(agents, picked, draft=drafts.setdefault(picked, dict(agents.get(picked, {}))))
            except catalog.MenuBack:
                if not adding:
                    continue
            else:
                selected = "done"
            break


def edit_agent(agents, agent, skip="", draft=None):
    """Ask the subagent's provider, model, effort and auto-compact, then save."""
    a = dict(agents.get(agent, {}))
    draft = dict(a) if draft is None else draft
    provider, model_id = draft.get("provider", "official"), draft.get("model")
    effort, pct = draft.get("effort", ""), draft.get("autocompact")
    step = 0
    while step < 4:
        try:
            if step == 0:
                picked = provider_menu(provider, "subagent", skip)
                if picked == "skip":
                    return
                if picked not in PROVIDERS:
                    die(f"Unknown provider '{picked}'.")
                if picked != provider:
                    model_id = None
                provider = picked
            elif step == 1:
                model_id = model_menu(provider, model_id, "subagent")
            elif step == 2:
                effort = catalog._prompt_effort(EFFORTS, effort, "subagent", back=True)
            else:
                pct = autocompact_menu(pct, "subagent")
            draft.update(provider=provider, model=model_id, effort=effort, autocompact=pct)
            step += 1
        except catalog.MenuBack:
            if step == 0:
                raise
            step -= 1
    agents[agent] = with_options(a, {"provider": provider, "model": model_id or "default",
                                     "effort": effort or "default", "autocompact": pct or "default"})
    save_agents(agents)


def startup_subagent(navigation=None, provider=None):
    """Startup prompt after the main session's: one subagent's provider, model, effort, auto-compact.

    With several profiles it first asks which; `ccx agents` adds, removes and edits the rest."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return  # a piped run keeps its profiles, as the effort menu keeps its pick
    agents = load_agents()
    last = (preset_agents(provider) or _latest_session_agents()) if _session_agents() is None else None
    if last and last != agents:
        # Start from the last launch's picks; "keep" then keeps them for this launch too.
        agents = check_agents(last)
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):  # the relay listing would break the menu
            save_agents(agents)
    navigation = {} if navigation is None else navigation
    drafts = navigation.setdefault("drafts", {})
    if sum(n in agents for n in ROUTER_LEVELS) > 1:
        return router_menu(agents, drafts, toggle=True)
    agent = navigation.get("agent")
    if agent not in agents:
        agent = next(iter(agents), "sub")
    while True:
        if len(agents) > 1:
            agent = catalog._arrow_menu("Subagent", [(n, f"{n}  {agent_summary(a)}") for n, a in agents.items()],
                                        agent, name="Subagent", back=True)
        navigation["agent"] = agent
        try:
            edit_agent(agents, agent, skip=f"keep {agent_summary(agents[agent]) if agent in agents else 'no subagent'}",
                       draft=drafts.setdefault(agent, dict(agents.get(agent, {}))))
        except catalog.MenuBack:
            if len(agents) > 1:
                continue
            raise
        return


def router_menu(agents, drafts=None, toggle=False):
    """The router's full editor in the terminal: each level's route, the ranges, best picks and (at startup)
    router on/off. Enter on "done" keeps what is shown."""
    drafts = {} if drafts is None else drafts
    @contextlib.contextmanager
    def quiet():  # the commands' own printouts would break the menu
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            yield
    selected = "done"
    while True:
        ranges = level_ranges(agents)
        on = router_on() if toggle else True
        rows = [("done", "done  keep these subagents")]
        if toggle:
            rows.append(("toggle", f"router  {'on: tasks go by difficulty score' if on else 'off: tasks go to ccx-sub'}"))
        rows += [(n, f"{n} {a}-{b}  {agent_summary(agents[n])}") for n, (a, b) in ranges.items()]
        rows += [(n, f"{n}  {agent_summary(a)}") for n, a in agents.items() if n not in ranges and (n == "sub" or toggle)]
        rows += [("ranges", "ranges  " + ", ".join(f"{a}-{b}" for a, b in ranges.values())),
                 ("best", "best picks  set every level from AA score and price")]
        selected = catalog._arrow_menu("Router", rows, selected, name="Router", back=True)
        if selected == "done":
            return
        if selected == "toggle":
            with quiet():
                set_router(not on)
        elif selected == "ranges":
            levels = list(ranges)
            answer = input(f"Tops for {', '.join(levels[:-1])} (rising, 0-99; {levels[-1]} reaches 100) "
                           f"[{' '.join(str(b) for _, b in list(ranges.values())[:-1])}]: ").split()
            try:
                tops = [int(t) for t in answer]
            except ValueError:
                tops = []
            if len(tops) == len(levels) - 1 and all(a < b for a, b in zip([-1, *tops], [*tops, 100])):
                with quiet():
                    set_ranges(tops)
                agents.update(load_agents())
            elif answer:
                print(f"Kept the ranges: give {len(levels) - 1} rising whole numbers.")
        elif selected == "best":
            provider = provider_menu((agents.get("sub") or {}).get("provider") or "official", "best picks")
            if provider in PROVIDERS:
                try:
                    with quiet():
                        set_best_levels(provider)
                except SystemExit:
                    print(f"No {provider} model has both an AA score and a price; set the levels by hand.")
                agents.update(load_agents())
                drafts.clear()
        else:
            try:
                edit_agent(agents, selected, skip=f"keep {agent_summary(agents[selected])}",
                           draft=drafts.setdefault(selected, dict(agents[selected])))
            except catalog.MenuBack:
                pass
            agents.update(load_agents())


def _context_entries(path, seen=None):
    """Materialize native fork references without changing either source transcript."""
    seen = set() if seen is None else seen
    path = Path(path).resolve()
    if path in seen:
        raise ValueError("cyclic worker context")
    seen.add(path)
    with path.open(encoding="utf-8") as handle:
        entries = (json.loads(line) for line in handle if line.strip())
        first = next(entries, None)
        if not isinstance(first, dict):
            raise ValueError("empty worker context")
        parent_id, cutoff = first.get("parentSessionId"), first.get("parentLastUuid")
        if first.get("type") == "fork-context-ref":
            uuid.UUID(parent_id)
            parent = path.parents[2] / (parent_id + ".jsonl")
            found = False
            for entry in _context_entries(parent, seen):
                yield entry
                if entry.get("uuid") == cutoff:
                    found = True
                    break
            if not found:
                raise ValueError("previous fork's parent context cutoff is unavailable")
        else:
            entries = itertools.chain([first], entries)
            parent_id, cutoff = first.get("sessionId"), None
        linked = False
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") == "fork-context-ref":
                raise ValueError("invalid worker context record")
            entry = dict(entry)
            if entry.get("message"):
                entry["isSidechain"] = False
                entry.pop("agentId", None)
                if parent_id:
                    entry["sessionId"] = parent_id
                if cutoff and not linked:
                    entry["parentUuid"] = cutoff
                linked = True
            yield entry


@contextlib.contextmanager
def _restorable_context(source):
    path = Path(source)
    if not path.is_absolute():
        uuid.UUID(source)
        yield source
        return
    # Always snapshot the source: a live parent may append records during restoration.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ccx-worker-context-") as directory:
        restored = Path(directory) / "context.jsonl"
        with restored.open("w", encoding="utf-8") as handle:
            for entry in _context_tail(list(_context_entries(path))):
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        yield str(restored)


CONTEXT_BUDGET = 50_000  # tokens of parent conversation a worker inherits; every step of the worker re-sends them
# Tools a headless worker never needs; their schemas are a third of its 28k baseline, re-sent every step.
WORKER_UNUSED_TOOLS = ("Artifact,ArtifactComments,ArtifactData,Workflow,ScheduleWakeup,CronCreate,CronDelete,CronList,RemoteTrigger,"
                       "DesignSync,SendFeedback,ReportFindings,EnterPlanMode,ExitPlanMode,EnterWorktree,ExitWorktree,NotebookEdit,"
                       "PushNotification,AskUserQuestion")
# Caveman's and ponytail's rules for every worker: their hooks do not all reach a headless or in-process one.
WORKER_RULES = """Save tokens. Write terse fragments: no articles or filler, no recap; keep code, paths, numbers, errors exact.
Before each batch of tool calls write one short line: what you check or change and why. The person follows your work by these lines.
Make the smallest change that fully solves the task: reuse what exists, add nothing unasked.
Where the project has .codegraph/, call codegraph_explore before Read or Grep, and do not re-read what it returned."""


def worker_mcp():
    """--mcp-config value: the user's codegraph without its watcher (the main session keeps the index fresh)."""
    try:
        with open(Path.home() / ".claude.json", encoding="utf-8") as f:
            server = json.load(f)["mcpServers"]["codegraph"]
        return json.dumps({"mcpServers": {"codegraph": {**server, "args": [*server.get("args", []), "--no-watch"]}}}, separators=(",", ":"))
    except (OSError, ValueError, KeyError, TypeError):
        return '{"mcpServers":{}}'


def _is_prompt(entry):
    """A user turn the person typed: a safe place to start a conversation (no tool_result without its call)."""
    message = entry.get("message") or {}
    if entry.get("type") != "user" or entry.get("isMeta") or message.get("role") != "user":
        return False
    content = message.get("content")
    return isinstance(content, str) or (isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") == "text" for b in content))


def _step_cut(entries, start, budget):
    """Within a long unattended turn from `start`: the oldest step (an assistant message after a user one,
    so no tool_result loses its call) leaving at most `budget`, or None when the turn fits."""
    size, cut = 0, None
    for i in range(len(entries) - 1, start, -1):
        message = entries[i].get("message")
        if not message:
            continue
        size += len(json.dumps(message.get("content"), ensure_ascii=False)) // 4
        if size > budget:
            return cut
        previous = next((e for e in reversed(entries[start:i]) if e.get("message")), None)
        if entries[i].get("type") == "assistant" and previous and previous.get("type") == "user":
            cut = i
    return None


def _context_tail(entries, budget=CONTEXT_BUDGET):
    """The newest messages that fit `budget` (about 4 characters a token), cut at a typed prompt,
    or that prompt and the newest steps of its turn when the turn alone is over budget."""
    size, start = 0, None
    for i in range(len(entries) - 1, -1, -1):
        message = entries[i].get("message")
        if message:
            size += len(json.dumps(message.get("content"), ensure_ascii=False)) // 4
        if _is_prompt(entries[i]):
            if size > budget and start is not None:
                break
            start = i
    if start is None:
        return entries  # no prompt to cut at
    cut = _step_cut(entries, start, budget)
    if not start and cut is None:
        return entries  # it all fits
    tail = [dict(entry) for entry in ([entries[start], *entries[cut:]] if cut else entries[start:])]
    if cut:
        tail[1]["parentUuid"] = entries[start]["uuid"]
    # Key points of the older history: the parent's latest compaction summary from before the cut, as the root.
    summary = next((dict(e) for e in reversed(entries[:start]) if e.get("isCompactSummary") and e.get("uuid")), None)
    tail[0]["parentUuid"] = summary["uuid"] if summary else None
    if summary:
        summary["parentUuid"] = None
        tail.insert(0, summary)
    return tail


def run_agent(agent, prompt, opts=None, require_context=False):
    require_context = require_context or bool(os.environ.get("CLAUDECODE") and os.environ.get("CCX_AGENT_CHILD") != "1")
    if require_context and opts:
        die("Delegated workers use their configured profile; per-run route overrides are not allowed.")
    a = load_agents().get(agent)
    if a is None:
        die(f"No subagent '{agent}' in {AGENTS_FILE}.")
    if _AGENT_OVERRIDE:
        a = {**{k: v for k, v in a.items() if k != "fallback"}, **_AGENT_OVERRIDE}
    a = check_agents({agent: with_options(a, opts or {})})[agent]  # this run only
    if not prompt.strip():
        die("The subagent task is empty.")
    if require_context and not prompt.startswith(CONTEXT_PREFIX):
        die("The relay omitted its context envelope; refusing a fresh-session fallback.")
    context = None
    if prompt.startswith(CONTEXT_PREFIX):
        header, separator, prompt = prompt.partition("\n")
        try:
            context = json.loads(header[len(CONTEXT_PREFIX):])
            if not separator or not isinstance(context, dict):
                raise ValueError("missing task or context")
            source = context["source"]
            uuid.UUID(context["token"])
            mode = context["permission_mode"]
            if mode not in ("default", "manual", "acceptEdits", "auto", "dontAsk", "plan", "bypassPermissions"):
                raise ValueError("invalid permission mode")
            if not (isinstance(source, str) and source and (
                    (Path(source).is_absolute() and Path(source).is_file() and Path(source).suffix == ".jsonl")
                    or str(uuid.UUID(source)) == source.lower())):
                raise ValueError("context transcript is unavailable")
        except (ValueError, KeyError, TypeError) as exc:
            die(f"Invalid subagent context: {exc}; refusing a fresh-session fallback.")
        if not prompt.strip():
            die("The subagent task is empty.")
    name, model_id = a.get("provider", "official"), a.get("model")
    route = route_env(name, model_id)
    effort = a.get("effort")
    efforts = model_efforts(name, model_id) if model_id else None
    if effort and efforts is not None and effort not in efforts:
        # Some subscription models reject --effort (MiMo answers with exit 1 otherwise).
        warn(f"{model_id} has no '{effort}' reasoning effort; running at its default.")
        effort = None
    # Drop the parent session's route and compaction threshold, and the marker that makes
    # Claude refuse to start inside another Claude Code session.
    env = {k: v for k, v in os.environ.items()
           if k not in (*CCX_VARS, *OAI_VARS, "CLAUDECODE", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")}
    env.update({k: str(v) for k, v in route.items()})
    if route.get("ANTHROPIC_BASE_URL", "").startswith(f"http://127.0.0.1:{ADAPTER_PORT}/"):
        # The local adapter rereads this profile's effort per request, so effort changes reach running workers.
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(filter(None, [route.get("ANTHROPIC_CUSTOM_HEADERS"), f"x-ccx-worker-profile: {agent}", f"x-ccx-launch: agent-{agent}"]))
    env.update(CCX_AGENT_CHILD="1", CCX_WORKER_PROFILE=agent, CCX_MAIN_PROVIDER=name,
               CLAUDE_CODE_FORK_SUBAGENT="0")
    compact = a.get("autocompact") or parent_compact(context)
    # Only codegraph, unwatched: each worker otherwise starts every MCP server, and a codegraph that indexes and watches the repo.
    command = ["claude", "-p", "--strict-mcp-config", "--mcp-config", worker_mcp(), "--disallowedTools", WORKER_UNUSED_TOOLS,
               "--append-system-prompt", WORKER_RULES,
               *(["--model", model_id] if model_id else []), *(["--effort", effort] if effort else []), *a.get("args", [])]
    compact_launch(env, command, compact)
    if context:
        conflicts = {"--resume", "-r", "--continue", "-c", "--session-id", "--fork-session"}
        if any(arg.split("=", 1)[0] in conflicts for arg in a.get("args", [])):
            die("Context-managed workers cannot override session arguments in profile args.")
        # Profile args must not widen the parent's permissions, nor load extra tools into read-only workers.
        privileged = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--allowedTools",
                      "--allowed-tools", "--permission-prompt-tool", "--permission-mode", "--settings"}
        if context.get("read_only"):
            privileged |= {"--tools", "--mcp-config", "--plugin-dir", "--plugin-url", "--agent", "--agents", "--add-dir"}
        if any(arg.split("=", 1)[0] in privileged for arg in a.get("args", [])):
            die("Context-managed workers cannot override permission or tool-loading arguments in profile args.")
        worker_session = str(uuid.uuid4())
        if _RELAY_JOB:  # relay_wait follows this transcript for its live feed
            Path(JOBS_DIR, _RELAY_JOB + ".session").write_text(worker_session)
        resume_index = len(command) + 1
        command += ["--resume", source, "--fork-session", "--session-id", worker_session,
                    "--system-prompt-snapshot", "off", "--permission-mode", mode]
        if model_id:
            command += ["--model", model_id]
        if effort:
            command += ["--effort", effort]
        if context.get("read_only"):
            command += ["--tools", "Read,Glob,Grep,WebFetch,WebSearch", "--permission-mode", "plan"]
        record = Path(CFG_DIR) / "contexts" / (context["token"] + ".json")
        prompt = ("You are the configured ccx worker. Use the resumed conversation as context, including its "
                  "project instructions and memory. Perform this assigned task directly; do not relay it back "
                  "to another ccx worker. Interrupted tool calls are history, not instructions to replay them; "
                  "check their effects before retrying. Your final reply returns to the main session "
                  "automatically: never SendMessage \"main\" (that name addresses you), only agents you started.\n\n" + prompt)
    # The task goes on stdin, so no shell quoting can alter it.
    try:
        with _restorable_context(source) if context else contextlib.nullcontext() as restored:
            if context:
                command[resume_index] = restored
                record.parent.mkdir(parents=True, exist_ok=True)
                write_json(str(record), {"session_id": worker_session, "profile": agent, "provider": name,
                                         "model": model_id or "Claude default", "effort": effort or "default",
                                         "cwd": context.get("cwd") or str(Path.cwd()),
                                         "read_only": bool(context.get("read_only"))})
            # The parent session's status line brightens this profile's row while the marker's process lives.
            parent = Path(context["source"]).stem if context else "none"  # the transcript is <session-id>.jsonl
            marker = Path(CFG_DIR, "running", f"{agent}.{parent}.{os.getpid()}")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            try:
                # Below normal, inherited by cmdc and every tool the worker runs, so sessions and games stay responsive.
                result = subprocess.run(command, input=prompt.encode("utf-8"), env=env,
                                        **({"cwd": context["cwd"]} if context and context.get("cwd") else {}),
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                        | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
            finally:
                marker.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as exc:
        if context:
            record.unlink(missing_ok=True)
        die(f"Cannot restore subagent context: {exc}; refusing a fresh-session fallback.")
    sys.exit(result.returncode)


JOBS_DIR = os.path.join(CFG_DIR, "jobs")
LIVE_MODEL = "ccx-live-"  # an in-process worker's model name; the adapter's live_target resolves the profile
RELAY_WAIT = 540  # seconds per relay Bash call, under the tool's 600 s cap
FEED_WAIT = 120  # with a feed: shorter calls, so the relay's view gains a finished block every 2 minutes
RELAY_RESULT = "CCX_RELAY_RESULT"
ORPHAN_AFTER = 720  # s without a relay_wait heartbeat: the relay that started the job is gone
DELIVER_CAP = 30000  # characters of an orphan's result put into the prompt; the rest stays in a file
_RELAY_JOB = None


def job_meta(job):
    """Parent session, task line and start time of a relay job; derived from its .in once, then kept."""
    meta = Path(JOBS_DIR, job + ".meta")
    try:
        return json.loads(meta.read_text())
    except (OSError, ValueError):
        pass
    source = Path(JOBS_DIR, job + ".in")
    try:
        text, started = source.read_bytes().decode("utf-8", "replace"), source.stat().st_mtime
    except OSError:
        return {}
    header, _, task = text.partition("\n")
    try:
        context = json.loads(header[len(CONTEXT_PREFIX):]) if header.startswith(CONTEXT_PREFIX) else {}
        parent, token = Path(context["source"]).stem, str(uuid.UUID(context["token"]))
    except (ValueError, KeyError, TypeError):
        parent, token = "", ""
    info = {"parent": parent, "token": token, "task": _feed_text(task, 80), "started": started}
    try:
        meta.write_text(json.dumps(info))
    except OSError:
        pass
    return info


def orphan_jobs(parent):
    """This session's relay jobs whose relay stopped waiting: still running, or finished and unread."""
    found = []
    for out in sorted(Path(JOBS_DIR).glob("*.out")) if parent and os.path.isdir(JOBS_DIR) else []:
        job, meta = out.stem, job_meta(out.stem)
        if not re.fullmatch(r"[0-9a-f-]{36}", job) or meta.get("parent") != parent:
            continue
        wait = Path(JOBS_DIR, job + ".wait")
        try:
            beat = wait.stat().st_mtime
        except OSError:
            beat = meta.get("started") or 0
        if time.time() - beat < ORPHAN_AFTER:
            continue
        session = ""
        try:
            session = Path(JOBS_DIR, job + ".session").read_text().strip()
        except OSError:
            try:  # jobs started before .session existed: the worker's context record names it
                session = json.loads(Path(CFG_DIR, "contexts", meta.get("token", "") + ".json").read_text())["session_id"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        found.append({"job": job, **meta, "done": Path(JOBS_DIR, job + ".rc").exists(), "session": session})
    return found


def fallback_jobs(parent):
    """{subagent name: its fallback route} for this session's relay workers still running on the fallback."""
    found = {}
    for marker in sorted(Path(JOBS_DIR).glob("*.fallback")) if parent and os.path.isdir(JOBS_DIR) else []:
        job = marker.stem
        if Path(JOBS_DIR, job + ".rc").exists() or job_meta(job).get("parent") != parent:
            continue
        try:
            route = json.loads(marker.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(route, dict) and route.get("agent"):
            found[route["agent"]] = route
    return found


def running_jobs(parent):
    """Count of this session's relay jobs still running (an .out without its .rc)."""
    return sum(1 for out in (Path(JOBS_DIR).glob("*.out") if parent and os.path.isdir(JOBS_DIR) else [])
               if not Path(JOBS_DIR, out.stem + ".rc").exists() and job_meta(out.stem).get("parent") == parent)


def _close_job(job):
    for name in (".out", ".rc", ".feed", ".session", ".meta", ".wait", ".nag", ".fallback"):
        try:
            Path(JOBS_DIR, job + name).unlink(missing_ok=True)
        except OSError:
            pass


def deliver_orphans(parent):
    """Hook text for the main session: finished orphan results (each delivered once) and the ones still running."""
    notes = []
    for item in orphan_jobs(parent):
        if not item["done"]:
            notes.append(f"- ccx worker job {item['job']} ({item['task']}) is still running after its relay stopped "
                         "waiting. Its result reaches you on a later prompt when it ends; do not start the task again.")
            continue
        try:
            text = Path(JOBS_DIR, item["job"] + ".out").read_bytes().decode("utf-8", "replace")
            code = Path(JOBS_DIR, item["job"] + ".rc").read_text().strip() or "1"
        except OSError:
            continue
        kept = ""
        if len(text) > DELIVER_CAP:
            full = Path(JOBS_DIR, item["job"] + ".result.txt")
            full.write_text(text, encoding="utf-8")
            text, kept = text[-DELIVER_CAP:], f" Only its last {DELIVER_CAP} characters are below; the whole result is in {full}."
        notes.append(f"- ccx worker job {item['job']} ({item['task']}) finished with exit code {code} after its relay "
                     f"stopped waiting.{kept} Its result:\n{text.strip()}")
        _close_job(item["job"])
    return ("Uncollected ccx worker results for this session:\n" + "\n".join(notes)) if notes else ""


def _feed_text(value, width=160):
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _feed_result(content):
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    lines = [line for line in str(content or "").splitlines() if line.strip()]
    if not lines:
        return "  ⎿  (no output)"
    more = f" … +{len(lines) - 1} lines" if len(lines) > 1 else ""
    return f"  ⎿  {_feed_text(lines[0], 140)}{more}"


def _feed_tool(block):
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    main = next((args[k] for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt")
                 if args.get(k)), "")
    return f"● {block.get('name', 'tool')}({_feed_text(main, 120)})"


def _feed_stamp(entry):
    try:
        return time.mktime(time.strptime(entry["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, TypeError, ValueError):
        return None


def feed_lines(entries, state):
    """The worker's transcript entries as Claude Code's own transcript lines; state carries the
    last timestamp (for "Thought for Ns") and the message ids already shown."""
    out, shown = [], set(state.setdefault("shown", []))
    for entry in entries:
        stamp, message = _feed_stamp(entry), entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                key = f"{message.get('id')}:{block.get('type')}:{block.get('id') or len(block.get('text') or block.get('thinking') or '')}"
                if key in shown:
                    continue  # one streamed message is written as several entries
                shown.add(key)
                if block.get("type") == "thinking":
                    took = f" for {int(stamp - state['last'])}s" if stamp and state.get("last") else ""
                    thought = _feed_text(block.get("thinking") or "", 200)
                    out.append(f"✻ Thought{took}" + (f"\n  ∴ {thought}" if thought else ""))
                elif block.get("type") == "text" and (block.get("text") or "").strip():
                    out.append("● " + _feed_text(block["text"], 400))
                elif block.get("type") == "tool_use":
                    out.append(_feed_tool(block))
        elif entry.get("type") == "user" and isinstance(content, list):
            out += [_feed_result(b.get("content")) for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if stamp and entry.get("type") in ("user", "assistant"):
            state["last"] = stamp
    state["shown"] = list(shown)[-500:]
    return out


def _feed(job, state, quiet):
    """Prints the worker's new transcript lines; a lone "✻ Thinking…" when it has waited on the model a while."""
    try:
        session = Path(JOBS_DIR, job + ".session").read_text().strip()
        uuid.UUID(session)
        path = next(Path.home().joinpath(".claude", "projects").glob(f"*/{session}.jsonl"))
        if state.get("session") != session:
            # a fallback rerun resumes into a new transcript: read it from the start, but only what is newer than
            # the last line shown (its resumed copy of the old history was already relayed)
            if state.get("session"):
                state["offset"], state["after"] = 0, state.get("last") or 0
            state["session"] = session
        if state.get("offset", 0) > path.stat().st_size:  # read past the end: a transcript swapped before "session" was kept
            state["offset"], state["after"] = 0, state.get("last") or 0
        with path.open("rb") as handle:
            handle.seek(state.get("offset", 0))
            data = handle.read()
    except (OSError, ValueError, StopIteration):
        return False
    complete = data[:data.rfind(b"\n") + 1]  # a line still being written waits for the next pass
    state["offset"] = state.get("offset", 0) + len(complete)
    entries = []
    for line in complete.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if state.get("after"):
        entries = [e for e in entries if (_feed_stamp(e) or 0) > state["after"]]
    lines = feed_lines(entries, state)
    if lines:
        state["thinking"] = False
        print("\n".join(lines), flush=True)
    elif quiet >= 15 and not state.get("thinking"):
        state["thinking"] = True
        print("✻ Thinking…", flush=True)
    return bool(lines)


def relay_start(agent, prompt, feed=False):
    """Run the worker as a detached job so no single relay call hits the Bash timeout."""
    job = str(uuid.uuid4())
    os.makedirs(JOBS_DIR, exist_ok=True)
    if feed:
        # Relays written before the feed would paste its lines into their reply; only new ones ask for it.
        Path(JOBS_DIR, job + ".feed").write_text("{}")
    for stale in Path(JOBS_DIR).glob("*"):
        try:
            if time.time() - stale.stat().st_mtime > 86400:
                stale.unlink()
        except OSError:
            pass
    Path(JOBS_DIR, job + ".in").write_bytes(prompt)
    job_meta(job)  # .in is gone once the worker ends; the status line and hook still need its parent
    with open(os.path.join(JOBS_DIR, job + ".out"), "wb") as out:
        subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "agent-job", agent, job],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    relay_wait(job)


def relay_job(agent, job):
    """The detached half: run the worker, then record its exit code for relay_wait."""
    if not re.fullmatch(r"[0-9a-f-]{36}", job):
        die("Invalid job id.")
    global _RELAY_JOB
    _RELAY_JOB = job
    code = 1
    try:
        prompt = Path(JOBS_DIR, job + ".in").read_bytes().decode("utf-8")
        try:
            run_agent(agent, prompt, require_context=True)
        except SystemExit as exc:
            fallback = (load_agents().get(agent) or {}).get("fallback")
            if not fallback or fallback.get("provider") != "official" or exc.code in (0, None):
                raise
            print(f"[ccx] worker failed; rerunning once on its fallback {fallback['provider']}/{fallback['model']}", flush=True)
            global _AGENT_OVERRIDE
            _AGENT_OVERRIDE = {k: v for k, v in fallback.items() if k in ("provider", "model", "effort")}
            try:  # the status line names the route the worker now runs on (fallback_jobs)
                Path(JOBS_DIR, job + ".fallback").write_text(json.dumps({"agent": agent, **_AGENT_OVERRIDE}))
            except OSError:
                pass
            run_agent(agent, prompt, require_context=True)
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout.flush()
        Path(JOBS_DIR, job + ".in").unlink(missing_ok=True)
        Path(JOBS_DIR, job + ".rc").write_text(str(code))


def relay_wait(job):
    """Print the job's output once it ends, or a pending line telling the relay to wait again."""
    if not re.fullmatch(r"[0-9a-f-]{36}", job):
        die("Invalid job id.")
    rc, out = Path(JOBS_DIR, job + ".rc"), Path(JOBS_DIR, job + ".out")
    if not out.exists():
        die(f"No relay job {job}.")
    if not rc.exists() and not Path(JOBS_DIR, job + ".in").exists() and time.time() - out.stat().st_mtime > 60:
        # delivered already: an earlier wait cleaned up while the worker still held .out open, so .out
        # stayed behind and every later wait read "pending" forever (2026-10-30, job 48dad47a).
        # .in lives until the worker ends (.meta is written lazily, so it can't tell)
        sys.stdout.write(out.read_bytes().decode("utf-8", errors="replace"))
        for done in (out, *(Path(JOBS_DIR, job + name) for name in (".feed", ".wait", ".nag", ".fallback"))):
            try:
                done.unlink(missing_ok=True)
            except OSError:
                pass
        return
    feed = Path(JOBS_DIR, job + ".feed")
    state = None
    if feed.exists():
        try:
            state = json.loads(feed.read_text() or "{}")
        except ValueError:
            state = {}
    deadline, quiet = time.time() + (FEED_WAIT if state is not None else RELAY_WAIT), 0
    beat = Path(JOBS_DIR, job + ".wait")  # a relay is waiting: orphan_jobs leaves the job alone
    beat.touch()
    while not rc.exists() and time.time() < deadline:
        time.sleep(1)
        beat.touch()
        if state is not None:
            quiet = 0 if _feed(job, state, quiet) else quiet + 1
    if state is not None:
        _feed(job, state, 0)  # the lines written just before the worker ended
        feed.write_text(json.dumps(state))
    if not rc.exists():
        print(f"CCX_RELAY_PENDING {job}")
        return
    if state is not None:
        print(RELAY_RESULT)
    sys.stdout.write(out.read_bytes().decode("utf-8", errors="replace"))
    code = int(rc.read_text() or 1)
    for done in (out, rc, feed, *(Path(JOBS_DIR, job + name) for name in (".session", ".meta", ".wait", ".nag", ".fallback"))):
        try:
            done.unlink(missing_ok=True)
        except OSError:
            pass  # the job process may still hold its output open for a moment; relay_start sweeps it
    sys.exit(code)


def sync_agents():
    """Write one native relay subagent per profile and list them."""
    agents = load_agents()
    # The relay file is shared by every session, so its text comes from the shared profile: one
    # session's route must never relabel another session's picker. Session-only names still get
    # a relay, and each session reads its own settings from its overlay at call time.
    written = {**agents, **load_agents(session=False)}

    def label(a):
        route = f"{a.get('provider', 'official')}/{a.get('model') or 'Claude default'}"
        return route, f"effort {a.get('effort') or 'default'}, autocompact {compact_text(a.get('autocompact'))}"

    os.makedirs(AGENTS_DIR, exist_ok=True)
    for file in Path(AGENTS_DIR).glob("cc-*.md"):
        # Relays and twins written before the ccx rename; they call the old ~/.config/cc copy.
        text = file.read_text(encoding="utf-8", errors="replace")
        if re.search(r'cc-switch\.py" agent(?:-relay)? |model: cc-live-', text):
            file.unlink()
    for file in Path(AGENTS_DIR).glob("ccx-*.md"):
        # Only relays (and their in-process twins) this command wrote, for profiles no session or the global file uses.
        text = file.read_text(encoding="utf-8", errors="replace")
        live = file.stem.endswith("-live") and f"model: {LIVE_MODEL}" in text
        if (file.stem[4:-5] if live else file.stem[4:]) not in all_agent_names() and (live or re.search(r'ccx-switch\.py" agent(?:-relay)? ', text)):
            file.unlink()
    for agent, a in written.items():
        # No route here: each session may run this profile elsewhere, and its context hook names that route.
        description = (a.get("description") or "General task") + f" Runs the task on this session's ccx-{agent} route; inherits the supplied conversation context and project memory. Use this configured worker for delegation."
        body = f"""---
name: ccx-{agent}
description: {json.dumps(description)}
model: haiku
tools: Bash
---
You relay one task to another Claude Code process. Do not do the task yourself.

Run this with the Bash tool and a 600000 ms timeout, putting the entire prompt you were given,
including its leading CCX_CONTEXT_V1 line, complete and unchanged, between the two markers.
That line supplies the previous conversation and worker context; never omit or rewrite it:

{PYTHON} "$HOME/.config/ccx/ccx-switch.py" agent-relay --feed {agent} <<'CCX_TASK_END'
<the complete prompt, including its context line>
CCX_TASK_END

The output shows the worker's progress live, then ends with `CCX_RELAY_PENDING <job>` while the
worker is still running: run `{PYTHON} "$HOME/.config/ccx/ccx-switch.py" agent-wait <job>` with a
600000 ms timeout, and repeat until the output no longer ends with that line. Never start the
task a second time. Do not comment on the progress lines between calls.

When it finishes, reply with everything after the `{RELAY_RESULT}` line verbatim, and nothing
before it (the progress lines are for the user's view only). If it fails, reply with the error
text; do not retry.
"""
        with open(os.path.join(AGENTS_DIR, f"ccx-{agent}.md"), "w", encoding="utf-8") as f:
            f.write(body)
        # The in-process twin: agent_hook sends ccx-{agent} here when the session runs through the adapter,
        # which resolves the profile from the model name, so its tab shows the worker's whole conversation.
        live = f"""---
name: ccx-{agent}-live
description: {json.dumps(f"In-process ccx-{agent} worker; ccx picks it for ccx-{agent} itself. Do not call it directly.")}
model: {LIVE_MODEL}{agent}
disallowedTools: {WORKER_UNUSED_TOOLS}
---
You are a worker subagent. Do the task you are given completely with your tools, then reply with a
concise report of what you did, what you found and anything left undone: the parent sees only that reply.

{WORKER_RULES}
"""
        twin = Path(AGENTS_DIR, f"ccx-{agent}-live.md")
        if not twin.is_file() or twin.read_text(encoding="utf-8") != live:  # a rewrite would hide it from running sessions (known_live)
            twin.write_text(live, encoding="utf-8")
        shown_route, shown_settings = label(agents.get(agent, a))
        print(f"  ccx-{agent}: {shown_route}, {shown_settings}")
    # Relays read agents.json on every call, so only a new or removed profile needs a reload.
    source = SESSION_AGENTS_FILE if session_launch() else AGENTS_FILE
    print(f"{len(agents)} subagent(s) in {AGENTS_DIR} from {source}. Model, effort and autocompact apply to the "
          "next worker call; running local-adapter workers take a new effort on their next request. "
          "New or removed profiles appear after a Claude Code restart.")


def _agent_files(data, agent_id):
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", agent_id):
        raise ValueError("invalid previous worker id")
    transcript = Path(data["transcript_path"])
    folder = transcript.parent.parent if transcript.parent.name == "subagents" else transcript.with_suffix("")
    return folder / "subagents" / f"agent-{agent_id}"


def _recorded_context(transcript, strict=False):
    """Find the latest ccx worker without loading the conversation into memory."""
    context = None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                entry = json.loads(line)
                if entry.get("type") != "user":
                    continue
                content = (entry.get("message") or {}).get("content")
                texts = [content] if isinstance(content, str) else [
                    c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
                for text in texts:
                    if text.startswith(CONTEXT_PREFIX):
                        context = json.loads(text.split("\n", 1)[0][len(CONTEXT_PREFIX):])
        if context is None:
            return {}  # a legacy relay did not record an external worker session
        uuid.UUID(context["token"])
        path = Path(CFG_DIR) / "contexts" / (context["token"] + ".json")
        with path.open(encoding="utf-8") as saved:
            record = json.load(saved)
        if not isinstance(record, dict):
            raise ValueError("invalid saved worker context")
        uuid.UUID(record["session_id"])
    except (OSError, ValueError, KeyError, TypeError):
        if strict:
            raise ValueError("latest worker context is unavailable") from None
        return {}
    return record


def _rewrite(data, original, updated, deny):
    """PreToolUse answer that turns `original` into `updated`. Auto mode reviews the call as submitted and refuses a
    hook-rewritten one, so there the call is denied once with the exact input to resubmit; the resubmitted call is
    already `updated` and passes through untouched (auto mode's review then covers what runs)."""
    if updated == original:
        return {}
    if data.get("permission_mode") != "auto":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}
    changes = []
    if updated.get("subagent_type") != original.get("subagent_type"):
        changes.append(f'subagent_type "{updated.get("subagent_type")}"')
    if "resume" in original and "resume" not in updated:
        changes.append("no resume field")
    if updated.get("prompt") != original.get("prompt"):
        p = updated.get("prompt") or ""
        if p.startswith(CONTEXT_PREFIX):
            changes.append("a prompt whose first line is exactly:\n" + p.partition("\n")[0]
                           + "\nfollowed by a newline and the unchanged task text")
        else:
            changes.append("the task text alone (drop the " + CONTEXT_PREFIX.strip() + " first line)")
    return deny("ccx: auto mode cannot review a hook-rewritten Agent call. This is expected, not a failure: resubmit "
                "the same Agent call right now, before any other text, with " + "; ".join(changes)
                + ". Keep every other field as it was.")


def agent_hook(data):
    """Enforce configured worker types without bypassing native permission rules."""
    result = _agent_decision(data)
    call = data.get("tool_input") or {}
    final = (result.get("hookSpecificOutput") or {}).get("updatedInput") or call
    level = str(final.get("subagent_type") or "").removeprefix("ccx-").removesuffix("-live")
    # only calls that run: auto mode's deny-and-resubmit would log every call twice
    if (level in ROUTER_LEVELS and not call.get("resume") and os.environ.get("CCX_AGENT_CHILD") != "1"
            and (result.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny"):
        log_route(level, call.get("description"))
    return result


def _agent_decision(data):
    if os.environ.get("CCX_AGENT_CHILD") == "1" or data.get("tool_name") not in ("Agent", "Task"):
        return {}

    def deny(reason):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": reason}}

    if subagents_off():
        return deny("Subagents are off for this session. Do the work yourself in the main session; "
                    "do not call Agent or Task again. The user can turn them back on in /model's Subagent tab.")
    try:
        profiles = offered_agents()
        if not profiles:
            return deny("No configured ccx subagents. Configure `ccx agents`; native delegation is blocked.")
        choices = ", ".join("ccx-" + name for name in profiles)
        original = data.get("tool_input") or {}
        kind = original.get("subagent_type") or "general-purpose"
        if kind.endswith("-live") and kind[:-5] in {"ccx-" + name for name in profiles}:
            kind = kind[:-5]
        ranges = level_ranges(profiles)
        if len(ranges) > 1 and kind[4:] in ranges and kind.startswith("ccx-") and not original.get("resume"):
            score = route_score(original.get("description"))
            if score is None:
                return deny('Router on: start the description with the task\'s difficulty from 0 to 100 in brackets, '
                            f'like "[35] rename helper", and resubmit; ccx picks the level ({range_text(ranges)}).')
            kind = "ccx-" + usable_level(level_for(score, ranges), ranges, listed_types(data.get("transcript_path")))
        def known_live():
            # Claude Code reads agent files at startup and on /reload-plugins, and records each change to the
            # types it knows in the transcript: a twin written after that is unknown to it.
            known = False
            try:
                with open(data["transcript_path"], "rb") as handle:
                    for line in handle:
                        if b'"agent_listing_delta"' in line and kind.encode() + b"-live" in line:
                            listing = json.loads(line).get("attachment") or {}
                            known = (kind + "-live" in listing.get("addedTypes", [])
                                     or (known and kind + "-live" not in listing.get("removedTypes", [])))
            except (OSError, KeyError, TypeError, ValueError):
                return False
            return known

        def resumes_live():
            # only an in-process worker resumes in-process; older workers go through the relay's handoff
            try:
                with _agent_files(data, original["resume"]).with_suffix(".meta.json").open(encoding="utf-8") as handle:
                    return json.load(handle).get("agentType") == kind + "-live"
            except (OSError, ValueError, KeyError, TypeError):
                return False
        if (kind in {"ccx-" + name for name in profiles} and os.environ.get("ANTHROPIC_BASE_URL", "").startswith(f"http://127.0.0.1:{ADAPTER_PORT}/")
                and known_live() and (not original.get("resume") or resumes_live())):
            # Through the adapter the worker runs in-process (a full native tab); it starts from the task
            # prompt alone, without the copied conversation the relay gives an external worker.
            prompt = original.get("prompt") or ""
            if prompt.startswith(CONTEXT_PREFIX):
                prompt = prompt.partition("\n")[2]
            return _rewrite(data, original, {**original, "subagent_type": kind + "-live", "prompt": prompt}, deny)
        if kind not in {"ccx-" + name for name in profiles}:
            return deny(f"Use configured subagent {choices} instead of {kind}. Resubmit the complete task "
                        "with that subagent_type. For an old worker keep its resume id: ccx will hand off its "
                        "transcript into the configured worker. Do not retry a native type. Specialized "
                        "read-only/custom agents must keep their original restrictions; use main-session "
                        "read-only tools when no matching configured worker exists.")
        context = {"token": str(uuid.uuid4()), "source": data.get("transcript_path"),
                   "permission_mode": data.get("permission_mode") or "default"}
        updated = {**original, "subagent_type": kind}  # a direct ccx-*-live call outside the adapter relays instead
        if original.get("resume"):
            path = _agent_files(data, original["resume"])
            with path.with_suffix(".meta.json").open(encoding="utf-8") as handle:
                previous = json.load(handle).get("agentType")
            if previous not in ("fork", "general-purpose", "claude", "Explore", "Plan", kind):
                return deny("Cannot migrate a custom agent without its instructions and memory. "
                            "Give the configured worker a self-contained task and its original agent definition.")
            history = path.with_suffix(".jsonl")
            if previous == kind:
                saved = _recorded_context(history, strict=True)
                if saved and saved.get("profile") != kind[4:]:
                    raise ValueError("previous worker belongs to another profile")
                context["source"] = saved.get("session_id") or str(history)
                context["read_only"] = bool(saved.get("read_only"))
                if saved.get("cwd"):
                    context["cwd"] = saved["cwd"]
            else:
                context["source"] = str(history)
                context["read_only"] = previous in ("Explore", "Plan")
                updated.pop("resume", None)  # fresh relay; the real worker resumes an isolated copy of the old context
            if not history.is_file():
                raise ValueError("previous worker transcript is unavailable")
            if not context.get("cwd"):
                with history.open(encoding="utf-8") as handle:
                    for line in handle:
                        entry = json.loads(line)
                        if entry.get("cwd"):
                            context["cwd"] = entry["cwd"]
                            break
        if not context.get("source"):
            raise ValueError("previous conversation transcript is unavailable")
        if context["permission_mode"] == "plan":
            context["read_only"] = True
        # The relay model retypes this line and tends to collapse JSON's doubled backslashes; Windows takes "/".
        for key in ("source", "cwd"):
            if isinstance(context.get(key), str):
                context[key] = context[key].replace("\\", "/")
        prompt = original.get("prompt") or "Continue the assigned task using the previous worker context."
        # A retried call may already carry an envelope. Always rebuild it from current hook fields; only its token
        # (a fresh name for the context record) may be the caller's, so a resubmitted call passes unchanged.
        if prompt.startswith(CONTEXT_PREFIX):
            header, _, prompt = prompt.partition("\n")
            try:
                given = json.loads(header[len(CONTEXT_PREFIX):])
                if (isinstance(given, dict) and set(given) == set(context)
                        and all(given[k] == context[k] for k in context if k != "token")):
                    context["token"] = str(uuid.UUID(given["token"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        updated["prompt"] = CONTEXT_PREFIX + json.dumps(context, separators=(",", ":")) + "\n" + prompt
        return _rewrite(data, original, updated, deny)
    except (Exception, SystemExit):
        return deny("Previous worker context or ccx configuration could not be verified. Native delegation "
                    "is blocked; give a configured ccx worker a self-contained task after checking its context.")


SLASH_COMMANDS = {"fallback": "fallback"}  # answered by `ccx <subcommand> <args>` as typed
SLASH_FLAGS = {}  # /subagent-autocompact went into /model's Compact column


def hook_command(data):
    """Answer /subagent-* here, before expansion, so they change settings without a model turn."""
    name = str(data.get("command_name") or "").split(":")[-1].lstrip("/")
    if name not in SLASH_FLAGS and name not in SLASH_COMMANDS:
        return {}
    flag, values = SLASH_FLAGS.get(name, ("", ""))
    words, note = str(data.get("command_args") or "").split(), ""
    if name in SLASH_COMMANDS:
        args = [SLASH_COMMANDS[name], *words]
    elif len(words) > 2:
        args, note = ["agents", "models"], f"Usage: /{name} <subagent> {values}"
    elif words and words[0].lower() not in load_agents():
        # Never create a profile by accident.
        args, note = ["agents", "models"], f"No subagent '{words[0]}'."
    elif len(words) == 2:
        args = ["agents", "set", words[0], flag, words[1]]
    else:
        # The subagent-picker mod answers these first with a menu; this only runs without it.
        args = ["agents", "models", *words[:1]]
        note = f"Usage: /{name} {words[0] if words else '<subagent>'} {values}"
    result = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = "\n".join(filter(None, [note, result.stdout.strip(), result.stderr.strip()]))
    return {"decision": "block", "reason": text or f"/{name} produced no output.",
            "hookSpecificOutput": {"hookEventName": "UserPromptExpansion"}}


def subagent_stop(data):
    """Keep a ccx relay alive when its worker outlived the first agent-relay call.

    A weak relay model sometimes treats CCX_RELAY_PENDING as its final answer. One
    blocked stop per job hands it the agent-wait command without looping forever."""
    kind = data.get("agent_type")
    path = data.get("agent_transcript_path")
    if not isinstance(kind, str) or not kind.startswith("ccx-") or not isinstance(path, str):
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for job in reversed(re.findall(r"CCX_RELAY_PENDING ([0-9a-f-]{36})", text)):
        out, rc = Path(JOBS_DIR, job + ".out"), Path(JOBS_DIR, job + ".rc")
        if rc.exists() or not out.exists():
            continue  # finished, or already collected and cleaned
        nag = Path(JOBS_DIR, job + ".nag")
        if nag.exists():
            return {}  # one nudge per job; never trap the relay in a loop
        try:
            nag.touch()
        except OSError:
            pass
        return {"decision": "block",
                "reason": (f"The ccx worker job {job} is still running. Do not finish. Run this with the Bash tool "
                           f"and a 600000 ms timeout:\n\n{PYTHON} \"$HOME/.config/ccx/ccx-switch.py\" agent-wait {job}\n\n"
                           "Repeat until the output no longer ends with CCX_RELAY_PENDING. Never start the task again.")}
    return {}


REVIEW_RULE = ("Review every ccx worker's result before you rely on it or report it: at least at a glance, check its "
               "changes (git diff/status or the files it names) and claims against the task, and fix or redo what is wrong.")


def review_hook(data):
    """PostToolUse on Agent: the main model reviews what a ccx worker did before it builds on it."""
    if os.environ.get("CCX_AGENT_CHILD") == "1" or not str((data.get("tool_input") or {}).get("subagent_type", "")).startswith("ccx-"):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": REVIEW_RULE}}


CTX_SENT_DIR = os.path.join(CFG_DIR, "ctx-sent")


def _context_once(data, text, orphans):
    """Send the standing ccx rules at SessionStart (also after /clear and compaction) and on a prompt only
    when they changed, so they are not re-added to the context on every message."""
    sid = data.get("session_id")
    if isinstance(sid, str) and re.fullmatch(r"[\w-]+", sid):
        mark = os.path.join(CTX_SENT_DIR, sid)
        try:
            with open(mark, encoding="utf-8") as f:
                same = f.read() == text
        except OSError:
            same = False
        if same and data.get("hook_event_name") == "UserPromptSubmit":
            text = ""
        else:
            try:
                os.makedirs(CTX_SENT_DIR, exist_ok=True)
                with open(mark, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass
    text = "\n\n".join(t for t in (text, orphans) if t)
    return {"hookSpecificOutput": {"hookEventName": data.get("hook_event_name", "UserPromptSubmit"),
            "additionalContext": text}} if text else {}


def hook_context(data):
    if os.environ.get("CCX_AGENT_CHILD") == "1":
        return {}
    if isinstance(data.get("session_id"), str):
        link_session(data["session_id"])
    orphans = deliver_orphans(data.get("session_id") or "") if isinstance(data.get("session_id"), str) else ""
    if subagents_off():
        return _context_once(data, "Subagents are off for this session: do all work in the main session "
                             "with your own tools. Do not call Agent or Task; every such call is blocked.", orphans)
    profiles = offered_agents()
    choices = "; ".join(f"ccx-{name}: {agent_summary(a)}" + (f" ({a['description']})" if a.get("description") else "")
                        for name, a in profiles.items())
    ranges = level_ranges(profiles)
    if len(ranges) > 1:  # the router: the main model scores each task and the score picks the level
        choices += (". Rate each task's difficulty from 0 (trivial) to 100 (hardest) and start the call's description "
                    "with it in brackets, like \"[35] rename helper\"; ccx runs the call on the level whose range holds "
                    f"the score ({range_text(ranges)}), whichever ccx level you name. Most delegated work is routine "
                    "and belongs low: keep the top range for work a strong model alone gets right. Split mixed work "
                    "into separate calls; when a worker's result fails your review, rerun that task once with a "
                    "score in the next range up before fixing it yourself")
        if (data.get("permission_mode") == "auto"
                and os.environ.get("ANTHROPIC_BASE_URL", "").startswith(f"http://127.0.0.1:{ADAPTER_PORT}/")):
            # auto mode denies a call the hook rewrites, and models often stop there: name the final type up front
            choices += (". Auto mode: name the in-process twin of the level that holds your score directly (for "
                        "\"[35] ...\" use ccx-easy-live, i.e. ccx-<level>-live); any other call is denied once and "
                        "must be resubmitted at once as the deny says")
    elif len(profiles) > 1:  # several profiles act as a router: the main model picks per task
        choices += (". Route each task to the cheapest profile whose description fits its difficulty; "
                    "split mixed work into separate calls")
    return _context_once(data, "Delegate only through configured ccx subagents: " + (choices or "none configured") +
            ". Native fork/general-purpose/custom worker calls are blocked. Use a ccx-* subagent_type and "
            "a self-contained task; ccx copies only the recent conversation and project memory. " + REVIEW_RULE + " To continue an old worker, "
            "use a configured ccx-* type with its resume id; do not restart it as a native fork.", orphans)


def main():
    global _STARTUP_LAUNCH
    args = sys.argv[1:]
    if args[:1] in (["hook-agent"], ["hook-context"], ["hook-command"], ["hook-review"]):
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict):
                raise ValueError("hook input must be an object")
            handler = {"hook-agent": agent_hook, "hook-command": hook_command, "hook-review": review_hook}.get(args[0], hook_context)
            print(json.dumps(handler(data)))
        except (Exception, SystemExit):
            if args[0] == "hook-agent":
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                      "permissionDecision": "deny", "permissionDecisionReason": "ccx routing input is invalid; native fallback blocked."}}))
        return
    if args[:2] == ["agent-relay", "--feed"] and len(args) == 3:
        return relay_start(args[2], sys.stdin.buffer.read(), feed=True)
    if args[:1] == ["agent-relay"] and len(args) == 2:
        return relay_start(args[1], sys.stdin.buffer.read())
    if args[:1] == ["agent-wait"] and len(args) == 2:
        return relay_wait(args[1])
    if args[:1] == ["agent-job"] and len(args) == 3:
        return relay_job(args[1], args[2])
    if args[:1] in (["agent"], ["agent-relay"]) and len(args) >= 2:
        # Before the apply-file cleanup: a relay must not touch a terminal's pending launch.
        opts, task = agent_options(args[2:])
        return run_agent(args[1], " ".join(task) or sys.stdin.buffer.read().decode("utf-8"), opts,
                         require_context=args[0] == "agent-relay")
    if args[:1] == ["models-refresh"] and len(args) == 2 and args[1] in PROVIDERS:
        with contextlib.redirect_stderr(open(os.devnull, "w")):
            models, live = fetch_models(args[1])
        if live:
            _save_models(args[1], models)
        return
    if args[:1] == ["fallback"]:
        return fallback_command(args[1:])
    if args[:1] == ["route"]:
        return route_command(args[1:])
    if args[:1] == ["helper"]:
        return helper_command(args[1:])
    if args[:1] == ["presets"]:
        return presets_command(args[1:])
    if args[:1] == ["providers"]:
        return providers_command(args[1:])
    if args[:1] == ["models"]:
        return models_command(args[1:])
    if args[:1] == ["facts"]:
        return model_facts_command(args[1:])
    if args[:1] == ["gui"]:
        return gui_command(args[1:])
    if args[:1] == ["hook-subagent-stop"]:
        try:
            data = json.load(sys.stdin)
            print(json.dumps(subagent_stop(data if isinstance(data, dict) else {})))
        except (Exception, SystemExit):
            pass
        return
    if args[:2] == ["agents", "set"] and len(args) >= 3:
        opts, rest = agent_options(args[3:])
        if rest or not opts:
            die(f"Usage: ccx agents set <name> {' '.join(f + ' <value>' for f in AGENT_FLAGS)}")
        agents = load_agents()
        agents[args[2].lower()] = with_options(agents.get(args[2].lower(), {}), opts)
        return save_agents(agents)
    if args[:2] == ["agents", "models"] and len(args) in (2, 3, 4):
        return agent_models(args[2].lower() if len(args) >= 3 else None, *args[3:])
    if args[:3] == ["agents", "router", "ranges"]:
        if len(args) == 3:
            return print("Difficulty ranges: " + (range_text(level_ranges(load_agents())) or "no router levels"))
        return set_ranges(args[3:])
    if args[:3] == ["agents", "router", "calibrate"]:
        return calibrate_ranges(args[3:])
    if args[:3] == ["agents", "router", "best"] and len(args) <= 4:
        return set_best_levels(args[3] if len(args) == 4 else None)
    if args[:2] == ["agents", "router"] and len(args) <= 3:
        if len(args) == 2:
            return print(router_text())
        if args[2] not in ("on", "off"):
            die("Usage: ccx agents router [on|off]")
        return set_router(args[2] == "on")
    if args in (["agents", "off"], ["agents", "on"]):
        return set_subagents_off(args[1] == "off")
    if args == ["agents", "providers"]:
        for n, p in PROVIDERS.items():
            print(f"{n}	{p['label']}")
        return
    if args[:2] == ["agents", "remove"] and len(args) == 3:
        agents = load_agents()
        if agents.pop(args[2].lower(), None) is None:
            die(f"No subagent '{args[2]}' in {AGENTS_FILE}.")
        return save_agents(agents)
    if args == ["agents"]:
        quit_on_ctrl_c()
        try:
            subagents_menu()
        except (KeyboardInterrupt, catalog.MenuBack):
            print("No change.")
            return
        return sync_agents()
    quit_on_ctrl_c()
    for f in (APPLY_FILE,):
        if os.path.exists(f):
            os.remove(f)
    no_run, pos = False, []
    while args:
        if args[0].lower() in ("-norun", "--norun"):
            no_run = True
            args.pop(0)
        # PowerShell drops the profile's `--` after `ccx plain`, so all of plain's words go to Claude.
        elif args[0].startswith("-") or len(pos) >= 2 or pos == ["plain"]:
            break
        else:
            pos.append(args.pop(0))
    claude_args = args[1:] if args[:1] == ["--"] else args
    autocompact = take_option(claude_args, "--autocompact")  # ccx's own, not Claude's
    autocompact = None if autocompact is None else parse_pct(autocompact)
    if no_run and claude_args:
        die("-NoRun cannot be combined with Claude launch arguments.")
    if not pos and claude_args in (["--help"], ["-h"]):
        print("Usage: ccx [provider] [model] [-NoRun] [--autocompact <1-100|100k|default>] [Claude arguments]")
        print("ccx --resume [session-id-or-name] | ccx --continue | ccx status")
        print("ccx agents (edit menu, then write subagents) | ccx agents set <name> [--provider P] [--model M]")
        print("    [--effort E|default] [--autocompact N|default] [--description D] | ccx agents models <name> [provider]")
        print("    | ccx agents remove <name> | ccx agents router [on|off|best [provider]|ranges [easy-top mid-top]]")
        print("ccx agent <name> [same options, this run only] [task, else stdin]")
        print("Every launch, resume included, asks for the provider and model unless both are given.")
        return

    if pos and pos[0] == "status":
        state = load_state()
        print("Current session:")
        print(f"  ANTHROPIC_BASE_URL : {os.environ.get('ANTHROPIC_BASE_URL', '')}")
        print(f"  ANTHROPIC_MODEL    : {os.environ.get('ANTHROPIC_MODEL', '')}")
        print(f"  Context tokens     : {os.environ.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS', 'Claude default')}")
        print(f"  OPENAI_BASE_URL    : {os.environ.get('OPENAI_BASE_URL', '')}")
        print(f"  Auto-compact       : {os.environ.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE', 'Claude default')}")
        if state:
            print(f"  Last chosen         : {state.get('provider')} / {state.get('model')}")
        return

    if pos == ["plain"]:
        # Plain `claude` (the PowerShell profile's function): the claude.ai login with no menus, through
        # the adapter, so /model can still move the session to another provider in place.
        resuming = is_resume(claude_args)
        model = take_option(list(claude_args), "--model")
        preset = ((saved_bundle("official") or {}).get("model") or {}).get("model")
        if not resuming:
            # A fresh launch id: the terminal may still hold an earlier launch's, whose settings are not this session's.
            _STARTUP_LAUNCH = os.environ["CCX_LAUNCH_ID"] = uuid.uuid4().hex
            if model is None and preset:
                model, claude_args = preset, ["--model", preset, *claude_args]  # the saved Claude preset's model
        # Fresh default Opus launches must not inherit a gateway's persisted effort; the saved preset's wins.
        effort = (default_effort("official", "claude-opus-5-5" if model in (None, "opus") else model)
                  or ("medium" if model in (None, "opus", "claude-opus-5-5") else None)) if not resuming else None
        apply("official", claude_args=claude_args, effort=effort)
        return

    try:
        name = pos[0].lower() if pos else None
        if name is not None and name not in PROVIDERS:
            die(f"Unknown provider '{name}'. Valid: {', '.join(sorted(PROVIDERS))}, status")
        model_id = pos[1] if len(pos) > 1 else None
        # Asked with the menus only; a typed `ccx <provider> <model>` passes --effort itself.
        menus = len(pos) < 2 and not no_run
        effort = None
        # `claude` runs `ccx official`: it opens on the presets too, with the cursor on that provider's
        steps = (["preset"] if menus else []) + ([] if pos else ["provider"]) + (["model"] if len(pos) < 2 else [])
        if menus and not any(a == "--effort" or a.startswith("--effort=") for a in claude_args):
            steps.append("effort")
        if menus and autocompact is None:
            steps.append("autocompact")
        launch = _STARTUP_LAUNCH = uuid.uuid4().hex
        os.environ["CCX_LAUNCH_ID"] = launch  # the startup subagent prompt edits this launch only
        fallback, backing = None, False
        if menus:
            steps.append("fallback")
        if menus:
            steps.append("subagent")
        subagent_navigation = {}
        step = 0
        while step < len(steps):
            try:
                if steps[step] == "preset":
                    picked = preset_menu(name)
                    if picked is None and backing:
                        raise catalog.MenuBack  # no preset screen was shown: Esc leaves the launch
                    if picked == "edit":
                        try:
                            edit_preset()
                        except catalog.MenuBack:
                            pass  # Esc inside the editor: back to the preset list
                        continue
                    if picked not in (None, "manual"):
                        bundle = saved_bundle(picked)
                        name, model_id = picked, bundle["model"]["model"]
                        effort = bundle["model"].get("effort") or default_effort(name, model_id)
                        fallback = preset_fallback(name)
                        if autocompact is None:
                            autocompact = load_state().get("autocompact") or None
                        break  # subagents/helpers come from the preset in apply()
                elif steps[step] == "provider":
                    picked = provider_menu(name)
                    if picked not in PROVIDERS:
                        die(f"Unknown provider '{picked}'. Valid: {', '.join(sorted(PROVIDERS))}, status")
                    if picked != name:
                        model_id = None
                    name = picked
                elif steps[step] == "model":
                    default = model_id or ((saved_bundle(name) or {}).get("model") or {}).get("model")
                    model_id = model_menu(name, default) if default else model_menu(name)
                elif steps[step] == "effort":
                    effort = effort_menu(effort or default_effort(name, model_id) or "")
                elif steps[step] == "autocompact":
                    autocompact = autocompact_menu(load_state().get("autocompact", "")
                                                  if autocompact is None else autocompact)
                elif steps[step] == "fallback":
                    if name not in FALLBACK_ROUTES:  # only adapter routes can fall back
                        step += -1 if backing else 1
                        continue
                    fallback = fallback_menu(name, fallback or preset_fallback(name))
                else:
                    startup_subagent(subagent_navigation, name)
                backing = False
                step += 1
            except catalog.MenuBack:
                if step == 0:
                    raise
                backing = True
                step -= 1
    except (KeyboardInterrupt, catalog.MenuBack):
        print("No change.")  # a cancel is not a failure: the PowerShell wrapper must not throw
        return

    if not menus and fallback is None and name in FALLBACK_ROUTES:
        fallback = preset_fallback(name)
    apply(name, model_id, no_run, claude_args=claude_args, effort=effort, autocompact=autocompact, fallback=fallback)


if __name__ == "__main__":
    main()
