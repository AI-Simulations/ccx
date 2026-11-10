"""Claude Code status line: model, effort, context, plan quota or API spend, cache, API time.

Every field is optional so third-party providers that omit data still render.
"""
import colorsys
import json
import os
import re
import subprocess
import sys
import time
import zlib
from datetime import datetime

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, CYAN, MAGENTA = "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[35m"
BLUE, ORANGE = "\033[34m", "\033[38;5;208m"
SEP = f" {DIM}│{RESET} "


def k(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def rgb(hue, sat=0.6, val=0.9):
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360, sat, val))
    return f"\033[38;2;{r};{g};{b}m"


def color(pct):
    # Smooth green (0%) -> yellow -> red (100%).
    return rgb(120 * (1 - min(max(pct, 0), 100) / 100))


def bar(pct, width=10):
    # One cell per full 10% used, each in the gradient color of its own position; the bar replaces the % number.
    filled = int(min(max(pct, 0), 100) * width // 100)
    cells = "".join(f"{color((i + 0.5) * 100 / width)}▰" for i in range(filled))
    # an empty bar draws its outlines dim green, so "unused" reads at a glance instead of a dim gap
    return f"{cells}{DIM}{'' if filled else color(0)}{'▱' * (width - filled)}{RESET}"


def span(seconds):
    seconds = max(int(seconds), 0)
    d, h, m = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    return f"{d}d{h}h" if d else f"{h}h{m:02d}m" if h else f"{m}m"


def last_usage(path):
    # Fallback when context_window is absent: newest assistant usage in the transcript's tail
    # (not the whole file: transcripts reach 70 MB and this runs every 2 s).
    for tail in (1 << 20, 8 << 20):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - tail))
                lines = f.read().splitlines()
        except (OSError, TypeError):
            return {}
        for line in reversed(lines):
            if b'"usage"' not in line:
                continue
            try:
                usage = json.loads(line).get("message", {}).get("usage")
            except (ValueError, AttributeError):
                continue
            if isinstance(usage, dict):
                return usage
        if tail >= size:
            break
    return {}


def new_lines(path, offset):
    """Complete lines after `offset`, streamed rather than read at once (a first scan covers whole
    transcripts); a line still being written waits for the next render."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    return
                yield line
    except OSError:
        return


STATE_DIR = os.path.expanduser("~/.claude/statusline")
# Reasoning steps, coolest to hottest. The five named levels are the ramp's anchors; the ladder
# a provider publishes for one model (it may add `off` or `ultra`) is scaled onto that ramp.
EFFORT_HUES = {"low": 200, "medium": 140, "high": 50, "xhigh": 25, "max": 350}  # cool -> hot
EFFORT_ORDER = ["off", "low", "medium", "high", "xhigh", "max", "ultra"]
EFFORT_RAMP = [EFFORT_HUES[level] for level in ("low", "medium", "high", "xhigh", "max")]
CLAUDE_HUE = 15  # Anthropic clay
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d", "month": "mo", "seven_day_opus": "7d opus", "seven_day_sonnet": "7d sonnet"}
CLAUDE_ALIASES =("claude", "opus", "sonnet", "haiku", "fable", "mythos")


def effort_hue(fraction):
    """0 = the coolest hue, 1 = the hottest, sampled along the low -> max ramp."""
    t = min(max(float(fraction), 0.0), 1.0) * (len(EFFORT_RAMP) - 1)
    i = min(int(t), len(EFFORT_RAMP) - 2)
    step = t - i
    a = colorsys.hsv_to_rgb(EFFORT_RAMP[i] / 360, 1, 1)
    b = colorsys.hsv_to_rgb(EFFORT_RAMP[i + 1] / 360, 1, 1)
    r, g, blue = (a[c] + (b[c] - a[c]) * step for c in range(3))
    return colorsys.rgb_to_hsv(r, g, blue)[0] * 360


def effort_ladder(levels):
    """The model's reasoning steps in order, or the usual Claude ladder when it publishes none."""
    if levels == []:
        return []  # the model takes no adjustable reasoning effort
    if not isinstance(levels, (list, tuple)):
        return list(EFFORT_HUES)  # an unreadable cache must not invent a ladder
    names = [name for name in levels if isinstance(name, str)]
    if not names:
        return list(EFFORT_HUES)
    rank = {name: i for i, name in enumerate(EFFORT_ORDER)}
    return [name for _, name in sorted(enumerate(names), key=lambda pair: (rank.get(pair[1], len(EFFORT_ORDER)), pair[0]))]


def effort_indicator(effort, dim=False, label=True, levels=None):
    """One circle per step up the model's own reasoning ladder, in that ladder's own colour.

    `levels` is the list the provider publishes for this model: None keeps the usual Claude
    ladder, and [] renders `default`, since the model has no adjustable reasoning effort.
    """
    if levels == []:
        return DIM + "default" + RESET
    ladder = effort_ladder(levels)
    if not effort or effort not in ladder:
        level, hue = 0, 280
    else:
        rank = ladder.index(effort)
        level, hue = rank + 1, effort_hue(rank / (len(ladder) - 1) if len(ladder) > 1 else 1.0)
    # An effort off the ladder has no circles: name it rather than show nothing.
    text = (effort + " " if label and level else "") + ("●" * level or effort or "")
    return rgb(hue, val=0.55 if dim else 0.9) + text + RESET


def load_json(name):
    try:
        with open(os.path.join(STATE_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(name, data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def default_effort(provider, model_id):
    """The level a profile with no effort runs at (the picker's EFFORT_INFO): cmdc's per-model or global
    config, Codex's default_reasoning_level, Claude Code's own settings; None when unknown."""
    if not model_id:
        return None
    if provider in ("commandcode", "commandcode-cli"):
        cfg = read_json(os.path.expanduser("~/.commandcode/config.json"))
        return (cfg.get("reasoningEffort") or {}).get(model_id) or cfg.get("effort")
    if provider == "openai-oauth":
        rows = read_json(os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                                      "models_cache.json")).get("models") or ()
        return next((r.get("default_reasoning_level") for r in rows
                     if isinstance(r, dict) and str(r.get("slug", "")).casefold() == str(model_id).casefold()), None)
    if provider == "official":
        cfg = read_json(os.path.expanduser("~/.claude/settings.json"))
        return ((cfg.get("modelSettings") or {}).get(model_id) or {}).get("effortLevel") or cfg.get("effortLevel")
    return None


def effort_levels(provider, model_id):
    """The reasoning efforts the provider publishes for this model; None when it publishes none.

    Local reads only: Codex's model cache for the OpenAI route, the copy ccx-switch.py keeps of
    Command Code's catalog for its two. The status line never waits on a network call.
    """
    if not model_id:
        return None
    key = str(model_id).casefold()
    if provider == "openai-oauth":
        rows = read_json(os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                                      "models_cache.json")).get("models") or ()
        for row in rows:
            if isinstance(row, dict) and str(row.get("slug", "")).casefold() == key:
                levels = row.get("supported_reasoning_levels")
                if not isinstance(levels, list):
                    return None
                return [e["effort"] for e in levels if isinstance(e, dict) and isinstance(e.get("effort"), str)]
        return None
    if provider in ("commandcode", "commandcode-cli"):
        models = read_json(os.path.expanduser("~/.config/ccx/efforts.json")).get("models") or {}
        for mid, levels in models.items():
            if str(mid).casefold() == key:
                return levels if isinstance(levels, list) else None
    return None


def claude_json():
    try:
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def credentials():
    # ~/.claude/.credentials.json carries the claude.ai login; a Console login instead stores
    # the key it created in ~/.claude.json.
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def subscription_login(creds):
    """True when a claude.ai login is stored, which pays for the session instead of an API key."""
    oauth = creds.get("claudeAiOauth")
    return isinstance(oauth, dict) and bool(oauth.get("accessToken") or oauth.get("refreshToken"))


def api_billing(d, stored, creds):
    """True when the session bills the Anthropic API per token instead of a claude.ai plan.

    ANTHROPIC_API_KEY outranks every login once you approve it (-p always uses it); below that a
    claude.ai login beats a stored Console key. Claude Code sends rate_limits only to subscribers,
    so the field's absence is what separates a keyed session from a planned one.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        # You approve or decline each key once; a declined key leaves the session on your plan.
        return key[-20:] not in ((stored.get("customApiKeyResponses") or {}).get("rejected") or [])
    if subscription_login(creds):
        return False
    return bool(stored.get("primaryApiKey")) and "rate_limits" not in d


def plan_name(account):
    plan = (account.get("organizationType") or "").removeprefix("claude_").replace("_", " ").title()
    # Tier looks like "default_claude_max_5x"; Max 5x is now plain Max and Max 20x is Ultra.
    mult = re.search(r"_(\d+x)$", account.get("organizationRateLimitTier") or "")
    if plan == "Max" and mult:
        plan = "Ultra" if mult.group(1) == "20x" else plan
    elif plan and mult:
        plan += " " + mult.group(1)
    if plan and account.get("hasExtraUsageEnabled"):
        plan += " +extra"
    return plan or None


def quota_source():
    # ccx sets ANTHROPIC_BASE_URL per provider; the status line inherits it.
    url = os.environ.get("ANTHROPIC_BASE_URL", "").lower().rstrip("/")
    # `ccx official` passes the claude.ai login through the adapter's /official route
    if not url or "anthropic.com" in url or url.endswith("/official"):
        return "anthropic"
    if "commandcode" in url:  # API route and the cmdc adapter route share one account
        return "commandcode"
    if url.endswith("/openai-oauth"):
        return "openai"
    if "opencode.ai/zen/go" in url or url.endswith("/opencode-go"):  # direct, or via the adapter
        return "opencode-go"
    if "openrouter.ai" in url or url.endswith("/openrouter"):
        return "openrouter"
    if "charm.land" in url or url.endswith("/hyper"):
        return "hyper"
    return None


def fetch_json(url, headers):
    import urllib.request  # here, not at the top: http/email/ssl cost ~10 MB on every 2 s render
    req = urllib.request.Request(url, headers={"User-Agent": "ccx-statusline", **headers})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def load_ccx_switch():
    """The ccx-switch.py this status line belongs to: the checkout's sibling first, else the install."""
    import importlib.util
    sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccx-switch.py")
    source = sibling if os.path.isfile(sibling) else os.path.expanduser("~/.config/ccx/ccx-switch.py")
    spec = importlib.util.spec_from_file_location("ccx_switch", source)
    ccx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccx)
    return ccx


def ccx_key(provider):
    """Use the launcher's credential precedence for providers picked after launch."""
    try:
        return load_ccx_switch().auth_key(provider)
    except (OSError, ImportError):
        return ""


def commandcode_quota():
    key = os.environ.get("CMD_API_KEY") or ccx_key("commandcode")
    if not key:
        with open(os.path.expanduser("~/.commandcode/auth.json"), encoding="utf-8-sig") as f:
            key = json.load(f)["apiKey"]
    d = fetch_json("https://api.commandcode.ai/alpha/billing/credits", {"Authorization": "Bearer " + key})
    windows = d.get("windowLimits") or {}
    limits = {}
    for src, name in (("fiveHour", "five_hour"), ("weekly", "seven_day")):
        w = windows.get(src)
        if isinstance(w, dict) and w.get("cap"):
            limits[name] = {"used_percentage": round(w["used"] * 100 / w["cap"]), "resets_at": w["resetAt"] / 1000}
    credits = d.get("credits") or {}
    left = sum(credits.get(f) or 0 for f in ("monthlyCredits", "purchasedCredits", "freeCredits"))
    return {"plan": "Command Code", "rate_limits": limits, "extra": f"${left:.2f} left"}


def openai_quota():
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    with open(os.path.join(home, "auth.json"), encoding="utf-8-sig") as f:
        tokens = json.load(f)["tokens"]
    d = fetch_json("https://chatgpt.com/backend-api/wham/usage", {
        "Authorization": "Bearer " + tokens["access_token"], "ChatGPT-Account-Id": tokens.get("account_id") or ""})
    limits = {}
    for w in ((d.get("rate_limit") or {}).get(k) for k in ("primary_window", "secondary_window")):
        if isinstance(w, dict) and w.get("used_percent") is not None:
            secs = w.get("limit_window_seconds") or 0
            name = {18000: "five_hour", 604800: "seven_day"}.get(secs, f"{secs // 3600}h")
            limits[name] = {"used_percentage": w["used_percent"], "resets_at": w.get("reset_at")}
    return {"plan": f"ChatGPT {(d.get('plan_type') or '').title().replace('Prolite', 'Pro Lite')}".strip(), "rate_limits": limits}


def opencode_go_quota():
    # ccx hands the Go key to Claude Code as ANTHROPIC_AUTH_TOKEN; a ccx subagent on Go while the
    # session is elsewhere reads ccx's own source instead. 403 without a Go subscription.
    key = os.environ.get("OPENCODE_GO_API_KEY") or ccx_key("opencode-go")
    if not key and quota_source() == "opencode-go":
        key = os.environ["ANTHROPIC_AUTH_TOKEN"]
    if not key:
        with open(os.path.expanduser("~/.local/share/opencode/auth.json"), encoding="utf-8") as f:
            key = json.load(f)["opencode-go"]["key"]
    d = fetch_json("https://opencode.ai/zen/go/v1/usage", {"Authorization": "Bearer " + key})
    limits = {}
    for src, name in (("rolling", "five_hour"), ("weekly", "seven_day"), ("monthly", "month")):
        w = (d.get("usage") or {}).get(src)
        if isinstance(w, dict) and w.get("percent") is not None:
            resets = datetime.fromisoformat(w["resetsAt"].replace("Z", "+00:00")).timestamp() if w.get("resetsAt") else None
            limits[name] = {"used_percentage": round(w["percent"]), "resets_at": resets}
    return {"plan": "OpenCode Go", "rate_limits": limits}


def hyper_quota():
    # Crush keeps Hyper's hourly OAuth token fresh while it runs; an expired one just fails here.
    key = os.environ.get("HYPER_API_KEY")
    if not key:
        with open(os.path.expanduser("~/AppData/Local/crush/crush.json"), encoding="utf-8") as f:
            key = json.load(f)["providers"]["hyper"]["api_key"]
    d = fetch_json("https://hyper.charm.land/api/v1/credits", {"Authorization": "Bearer " + key})
    if d.get("balance") is not None:
        extra = f"◆ {d['balance']:,} hypercredits"
    elif d.get("balance_usd") is not None:
        extra = f"${float(d['balance_usd']):.2f} left"
    else:
        raise ValueError("no balance")
    return {"plan": "Hyper", "rate_limits": {}, "extra": extra}


def openrouter_quota():
    key = os.environ.get("OPENROUTER_API_KEY") or ccx_key("openrouter")
    if not key and quota_source() == "openrouter":
        key = os.environ["ANTHROPIC_AUTH_TOKEN"]
    if not key:
        with open(os.path.expanduser("~/.local/share/opencode/auth.json"), encoding="utf-8") as f:
            key = json.load(f)["openrouter"]["key"]
    d = fetch_json("https://openrouter.ai/api/v1/key", {"Authorization": "Bearer " + key})["data"]
    left = d.get("limit_remaining")
    extra = f"${left:.2f} left" if left is not None else f"${d.get('usage') or 0:.2f} used"
    return {"plan": "OpenRouter", "rate_limits": {}, "extra": extra}


QUOTA_FETCHERS = {"commandcode": commandcode_quota, "openai": openai_quota,
                  "opencode-go": opencode_go_quota, "hyper": hyper_quota, "openrouter": openrouter_quota}
QUOTA_TTL = 10  # the usage endpoints are plain account lookups: no model tokens
QUOTA_RETRY = 60  # after a failed fetch
# ccx provider names (~/.config/ccx/agents.json) that share a quota source; the rest have no quota API.
CCX_QUOTA_SOURCES = {"official": "anthropic", "commandcode": "commandcode", "commandcode-cli": "commandcode",
                    "openai-oauth": "openai", "opencode-go": "opencode-go", "openrouter": "openrouter"}


ROUTER_LEVELS = ("very-easy", "easy", "mid", "hard", "very-hard")  # ccx-switch.py's router levels
LEVEL_TOPS = {"very-easy": 30, "easy": 75, "mid": 89, "hard": 95}  # ccx-switch.py's default range tops


def level_ranges(profiles):
    """ccx-switch.py's level_ranges: {level: (low, high)} on the 0-100 difficulty scale."""
    levels = [n for n in ROUTER_LEVELS if n in profiles]
    ranges, low = {}, 0
    for i, name in enumerate(levels):
        high = 100 if i == len(levels) - 1 else (profiles[name] or {}).get("upto", LEVEL_TOPS.get(name, 50))
        high = max(low, min(int(high), 100))
        ranges[name] = (low, high)
        low = min(high + 1, 100)
    return ranges


def ccx_agents(session_id=None, offered=True):
    """The subagent profiles this session offers (ccx-switch's offered_agents): its own, or the shared defaults."""
    try:
        with open(os.path.expanduser("~/.config/ccx/agents.json"), encoding="utf-8") as f:
            agents = json.load(f)
    except (OSError, ValueError):
        return {}
    # ccx-switch's session_launch: a Claude session not started by ccx keys its profiles by session id
    launch = os.environ.get("CCX_LAUNCH_ID") or (f"claude-{session_id}" if session_id else "")
    entry = {}
    if launch:
        try:
            with open(os.path.expanduser("~/.config/ccx/session-agents.json"), encoding="utf-8") as f:
                entry = json.load(f).get(launch) or {}
        except (OSError, ValueError, AttributeError):
            entry = {}
    shared = agents if isinstance(agents, dict) else {}
    if isinstance(entry, dict) and isinstance(entry.get("agents"), dict):
        own = entry["agents"]
        agents = {**own, **{n: a for n, a in shared.items() if n in ROUTER_LEVELS and n not in own}}
    else:
        agents, entry = shared, entry if isinstance(entry, dict) else {}
    if offered and any(n in ROUTER_LEVELS for n in agents):
        on = entry.get("router") is not False
        kept = {n: a for n, a in agents.items() if (n != "sub" if on else n not in ROUTER_LEVELS)}
        agents = kept or agents
    return agents


def subagent_sources(session_id=None):
    """{quota source: [ccx subagent names]} for ccx's own-process subagents."""
    agents = ccx_agents(session_id)
    found = {}
    for name, a in agents.items() if isinstance(agents, dict) else ():
        source = CCX_QUOTA_SOURCES.get((a if isinstance(a, dict) else {}).get("provider", "official"))
        if source:
            found.setdefault(source, []).append(name)
    return found


def ccx_profiles(session_id=None):
    return {name: profile for name, profile in ccx_agents(session_id).items() if isinstance(profile, dict)}


def ccx_orphans(session_id):
    """This session's relay jobs nobody waits on any more (ccx-switch's orphan_jobs), with the worker's tokens."""
    if not isinstance(session_id, str) or not session_id:
        return []
    try:
        ccx = load_ccx_switch()
        return [{**item, "tokens": ccx._worker_tokens(ccx._worker_path({"session_id": item["session"]})) if item["session"] else ""}
                for item in ccx.orphan_jobs(session_id)]
    except Exception:  # a status line never fails over an optional row
        return []


def ccx_fallback_jobs(session_id):
    """{subagent name: route} for this session's workers that ccx moved onto their fallback (ccx-switch's fallback_jobs)."""
    if not isinstance(session_id, str) or not session_id:
        return {}
    try:
        return load_ccx_switch().fallback_jobs(session_id)
    except Exception:  # a status line never fails over an optional row
        return {}


def ccx_running_jobs(session_id):
    """Count of this session's relay workers still running (ccx-switch's running_jobs)."""
    if not isinstance(session_id, str) or not session_id:
        return 0
    try:
        return load_ccx_switch().running_jobs(session_id)
    except Exception:  # a status line never fails over an optional row
        return 0


def ccx_routing_enabled():
    hooks = (user_settings().get("hooks") or {}).get("PreToolUse") or []
    return any("ccx-switch.py" in hook.get("command", "") and "hook-agent" in hook.get("command", "")
               for group in hooks if isinstance(group, dict)
               for hook in group.get("hooks") or [] if isinstance(hook, dict))


def ccx_main_provider(source):
    if os.environ.get("CCX_MAIN_PROVIDER"):
        return os.environ["CCX_MAIN_PROVIDER"]
    url = os.environ.get("ANTHROPIC_BASE_URL", "").lower().rstrip("/")
    for provider in ("commandcode-cli", "commandcode", "nvidia", "minimax", "openrouter", "opencode-zen", "gemini", "ollama-cloud", "lmstudio"):
        if provider in url:
            return provider
    return {"anthropic": "official", "openai": "openai-oauth", "opencode-go": "opencode-go", "hyper": "hyper"}.get(
        source if source is not None else quota_source())


def provider_quota(source, now):
    """Returns (quota, stale_since), refreshing it in the background so no render waits on the network."""
    name = f"quota-{source}.json"
    saved = load_json(name)
    if not saved.get("at"):
        if now - saved.get("tried", 0) >= QUOTA_RETRY:
            return fetch_quota(source, now)
    elif (now - saved["at"] >= QUOTA_TTL and now - saved.get("tried", 0) >= QUOTA_RETRY
          and now - saved.get("claimed", 0) >= QUOTA_TTL):
        save_json(name, {**saved, "claimed": now})  # other sessions' renders leave this refresh to us
        try:
            subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "--quota", source], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass
    return saved, saved.get("at") if saved.get("tried", 0) > saved.get("at", 0) else None


def fetch_quota(source, now):
    """Reads and saves the quota; a failed fetch keeps the last good result."""
    name = f"quota-{source}.json"
    saved = load_json(name)
    try:
        q = {**QUOTA_FETCHERS[source](), "at": now}
        resets = [w.get('resets_at') for w in (q.get('rate_limits') or {}).values()
                  if isinstance(w, dict) and isinstance(w.get('resets_at'), (int, float))
                  and isinstance(w.get('used_percentage'), (int, float)) and w['used_percentage'] >= 99
                  and w['resets_at'] > now]
        if resets:
            q['last_reset_at'] = max(resets)
    except Exception as e:
        import urllib.error
        # Keep why it failed: an expired key otherwise just makes the bars vanish.
        body = e.read().decode("utf-8", "replace") if isinstance(e, urllib.error.HTTPError) else ""
        error = ("key expired" if "api_key_expired" in body else f"auth failed (HTTP {e.code})" if getattr(e, "code", None) in (401, 403)
                 else "no credentials" if isinstance(e, (OSError, KeyError)) and not isinstance(e, urllib.error.URLError) else "usage unavailable")
        save_json(name, {**saved, "tried": now, "error": error})  # don't retry (and block 3s) every render
        return {**saved, "error": error}, saved.get("at")
    save_json(name, q)
    return q, None


def is_third_party(model_id):
    return bool(model_id) and not model_id.lower().startswith(CLAUDE_ALIASES)



def user_settings():
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def compact_limit(model_id, size):
    """Tokens at which this session autocompacts: the ccx percentage override, else the settings window."""
    pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")
    if pct.isdigit():
        return size * int(pct) // 100
    s = user_settings()
    window = (s.get("modelSettings", {}).get(model_id) or {}).get("autoCompactWindow") or s.get("autoCompactWindow")
    return min(window, size) if isinstance(window, int) else None


def settings_effort(model_id):
    s = user_settings()
    return (s.get("modelSettings", {}).get(model_id) or {}).get("effortLevel") or s.get("effortLevel")


def settings_ultracode():
    return user_settings().get("ultracode") is True


def last_prompt(path, tail=262_144):
    # Newest typed user prompt (tool results also arrive as "user" lines, as lists without plain text).
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - tail))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        # Only prompts you typed; skips task notifications, skill expansions and tool results.
        if entry.get("type") != "user" or (entry.get("origin") or {}).get("kind") != "human":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        if isinstance(content, str) and content.strip():
            return content
    return ""


def keyword_ultracode(path):
    # Typing "ultracode" in a prompt turns it on for that turn only; the payload doesn't report it.
    return re.search(r"\bultracode\b", last_prompt(path), re.I) is not None


def quota_parts(plan, extra, limits, cached_at, now):
    parts = []
    if plan and (limits or extra):
        parts.append(f"{BOLD}{plan.replace('ChatGPT Prolite', 'ChatGPT Pro Lite')}{RESET}" + (f" {DIM}{extra}{RESET}" if extra else ""))
    for key, lim in (limits if isinstance(limits, dict) else {}).items():
        if not isinstance(lim, dict) or lim.get("used_percentage") is None:
            continue
        label = WINDOW_LABELS.get(key, key.replace("_", " "))
        pct, resets = lim["used_percentage"], lim.get("resets_at")
        if cached_at and resets and resets < now:
            pct, resets = 0, None  # window rolled over since the cache was written
        # A window replayed from the cache is the last figure Claude Code sent, not a live one: its label is
        # dim (a "~" prefix read as "about 7d").
        seg = f"{DIM + label + RESET if cached_at else label} {bar(pct)}"
        if resets:
            seg += f" {DIM}{span(resets - now)}{RESET}"
        parts.append(seg)
    return parts


# Providers keep prompt caches warm for minutes after a reply without publishing an expiry.
# The status line assumes their usual window, counted from the last transcript write.
CACHE_WINDOWS = {'openai-oauth': 300, 'commandcode': 300, 'commandcode-cli': 300,
                 'opencode-go': 300, 'opencode-zen': 300, 'openrouter': 300}


def cache_state(d, provider, source, model_id, now):
    """(used%, seconds left) for a warm prompt cache, else None.

    Anthropic carries Claude Code's own expiry and ttl. Other routes publish none, so their
    countdown starts at the last transcript write and runs for that provider's usual window;
    an expired or unknown cache shows no figure rather than a wrong one."""
    cache = d.get('prompt_cache') or {}
    if source == 'anthropic' and not is_third_party(model_id):
        expires = cache.get('expires_at')
        if cache.get('warm') and isinstance(expires, (int, float)) and expires > now:
            ttl = 300 if cache.get('ttl') == '5m' else 3600
            return round(100 - (expires - now) * 100 / ttl), expires - now
        return None
    ttl = CACHE_WINDOWS.get(provider)
    if not ttl:
        return None
    try:
        last = os.path.getmtime(d.get('transcript_path'))
    except (OSError, TypeError):
        return None
    left = ttl - (now - last)
    if left <= 0:
        return None
    return max(0, min(99, round(100 - left * 100 / ttl))), left


# Anthropic list prices per MTok: (model id prefix, input, output, cache read). Longest prefix first.
# Cache writes bill 1.25x input (5m) or 2x (1h); fast mode doubles everything.
PRICES = [("claude-fable-5-1", 10, 50, 0.25), ("claude-mythos-5-1", 10, 50, 0.25),
          ("claude-fable-5", 10, 50, 1.0), ("claude-mythos-5", 10, 50, 1.0),
          ("claude-opus-5-5", 4, 20, 0.2), ("claude-opus-5", 5, 25, 0.5), ("claude-opus-4-8", 5, 25, 0.5),
          ("claude-opus-4-7", 5, 25, 0.5), ("claude-opus-4-6", 5, 25, 0.5), ("claude-opus-4-5", 5, 25, 0.5),
          ("claude-sonnet-5", 2, 10, 0.2), ("claude-sonnet-4", 3, 15, 0.3),
          ("claude-haiku-5-5", 0.1, 0.5, 0.01), ("claude-haiku-4-5", 1, 5, 0.1),
          # OpenAI list prices (models.dev, 2026-11); cache writes bill 1.25x input there too.
          ("gpt-6.1-sol", 2, 10, 0.1), ("gpt-6.1-terra", 1, 5, 0.05), ("gpt-6-astra", 10, 50, 1),
          ("gpt-6-sol", 2, 10, 0.2), ("gpt-6-luna", 0.1, 0.5, 0.01), ("gpt-5.6-sol", 4, 20, 0.4),
          ("gpt-5.6-terra", 2, 12, 0.2), ("gpt-5.6-luna", 0.2, 1.2, 0.02)]


PRICES_VERSION = 4  # bump on any price change: saved transcript totals are recomputed


_MODELS_DEV = None


def models_dev_price(model_id):
    """(id, input, output, cache read) from the models.dev copy opencode keeps fresh, for a Claude or GPT
    model newer than PRICES; None when it is not listed there either."""
    global _MODELS_DEV
    vendor = "anthropic" if model_id.startswith("claude-") else "openai" if model_id.startswith("gpt-") else None
    if not vendor:
        return None
    if _MODELS_DEV is None:
        _MODELS_DEV = read_json(os.path.expanduser("~/.cache/opencode/models.json"))
    cost = (((_MODELS_DEV.get(vendor) or {}).get("models") or {}).get(model_id) or {}).get("cost") or {}
    if not isinstance(cost.get("input"), (int, float)) or not isinstance(cost.get("output"), (int, float)):
        return None
    return model_id, cost["input"], cost["output"], cost.get("cache_read", cost["input"] / 10)


def usage_cost(model_id, usage):
    """USD for one reply at list price, or None for a model without a known price."""
    # A route may prefix the vendor (OpenRouter's `anthropic/claude-opus-5-5`); the model is still Anthropic's.
    model_id = str(model_id or "").rsplit("/", 1)[-1]
    price = next((p for p in PRICES if model_id.startswith(p[0])), None) or models_dev_price(model_id)
    if not price:
        return None
    _, inp, out, read = price
    prompt = sum(usage.get(f) or 0 for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if price[0] == "claude-haiku-5-5" and prompt > 100_000:
        # Haiku 5.5's long-prompt tier: past 100K the whole request bills $0.50 / $2.50, not just the excess.
        # The cache read keeps its 0.1x of input there (derived: the tier publishes no cache price of its own).
        inp, out, read = 0.5, 2.5, 0.05
    if price[0].startswith("gpt-") and prompt > 272_000:
        # OpenAI's long-context tier: past 272K the whole request bills 2x input/cache, 1.5x output.
        inp, out, read = inp * 2, out * 1.5, read * 2
    split = usage.get("cache_creation") or {}
    long = split.get("ephemeral_1h_input_tokens") or 0
    short = (usage.get("cache_creation_input_tokens") or 0) - long
    usd = ((usage.get("input_tokens") or 0) * inp + short * inp * 1.25 + long * inp * 2
           + (usage.get("cache_read_input_tokens") or 0) * read + (usage.get("output_tokens") or 0) * out) / 1e6
    return usd * 2 if usage.get("speed") == "fast" else usd


def transcript_cost(path):
    """{usd, sent, read, priced} for one transcript, scanned incrementally (transcripts reach 70 MB)."""
    total = {"usd": 0.0, "sent": 0, "read": 0, "priced": False}
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return total
    name = f"cost-{zlib.crc32(os.path.abspath(path).encode()):08x}.json"
    saved = load_json(name)
    if saved.get("path") != path or saved.get("offset", 0) > size or saved.get("prices") != PRICES_VERSION:
        saved = {}  # new or rewritten transcript, or totals made with older prices
    total.update(saved.get("total") or {})
    offset, last = saved.get("offset", 0), saved.get("last")
    if offset == size:
        return total
    read = 0
    for line in new_lines(path, offset):
        read += len(line)
        if b'"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
            message = entry.get("message") or {}
        except (ValueError, AttributeError):
            continue
        usage = message.get("usage")
        # one reply spans several consecutive lines carrying the same usage; sidechains live in their own files
        if not isinstance(usage, dict) or message.get("id") == last or entry.get("isSidechain") and "subagents" not in path:
            continue
        last = message.get("id")
        total["ctx"] = sum(usage.get(f) or 0 for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        total["sent"] += sum(usage.get(f) or 0 for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        total["read"] += usage.get("cache_read_input_tokens") or 0
        usd = usage_cost(message.get("model"), usage)
        if usd is not None:
            total["usd"] += usd
            total["priced"] = True
    save_json(name, {"path": path, "offset": offset + read, "last": last, "total": total, "prices": PRICES_VERSION})
    return total


def subagent_costs(transcript_path):
    """{agent name: cost totals} for this session's subagents (Claude Code's <session>/subagents/ files);
    `ctx` is the newest transcript's last context size."""
    found = {}
    base = os.path.splitext(transcript_path or "")[0]
    if not base or not os.path.isdir(base):
        return found
    for root, _, files in os.walk(base):
        for file in files:
            if not (file.startswith("agent-") and file.endswith(".jsonl")):
                continue
            path = os.path.join(root, file)
            kind = read_json(path[:-len(".jsonl")] + ".meta.json").get("agentType") or "subagent"
            name = re.sub(r"-live$", "", re.sub(r"^ccx-", "", kind))  # ccx-sub-live is profile "sub"
            cost = transcript_cost(path)
            into = found.setdefault(name, {"usd": 0.0, "sent": 0, "read": 0, "priced": False, "ctx": 0, "at": 0})
            for key in ("usd", "sent", "read"):
                into[key] += cost[key]
            into["priced"] |= cost["priced"]
            at = os.path.getmtime(path)
            if cost.get("ctx") and at >= into["at"]:
                into["ctx"], into["at"] = cost["ctx"], at
    return found


def cost_text(cost, tint=None):
    """`$1.23 (87% cached)`, or '' when nothing was priced."""
    if not cost or not cost.get("priced"):
        return ""
    usd = cost["usd"]
    hit = f" {DIM}({cost['read'] * 100 // cost['sent']}% cached){RESET}" if cost.get("sent") else ""
    # green at $0 through yellow to red at $100 and above, like the mode row's total
    tint = tint or rgb(120 * (1 - min(max(usd, 0), 100) / 100))
    return f"{tint}{f'${usd:,.2f}' if usd >= 1 else f'${usd:.3f}'}{RESET}{hit}"


def cached_text(cost):
    """` 49% cached` (dim), the share of sent input tokens read from cache; '' before any reply."""
    return f" {DIM}{cost['read'] * 100 // cost['sent']}% cached{RESET}" if cost and cost.get("sent") else ""


SPEND_KEEP = 8 * 86400  # replies older than the longest quota window plus a day are dropped
SPEND_BUCKET = 300  # spend.json sums each source's API-price spend per 5 minutes
WINDOW_SECONDS = {"five_hour": 18000, "seven_day": 604800}
# Quota per plan relative to the base plan (Anthropic: Pro; OpenAI: Plus), from the published 5x/20x tiers.
PLAN_MULTS = {"anthropic": {"Pro": 1, "Max": 5, "Ultra": 20}, "openai": {"Plus": 1, "Pro Lite": 5, "Pro": 20}}


def reply_source(model_id):
    """Which subscription quota a reply drew on, judged by its model id."""
    mid = str(model_id or "")
    return "anthropic" if mid.startswith("claude-") else "openai" if mid.startswith("gpt-") else None


def index_spend(now):
    """Background job: API-price spend of every Claude Code reply in the last 8 days, deduplicated by
    message id (a resumed or forked session repeats its parent's replies), saved as 5-minute buckets."""
    import glob
    index = load_json("spend-index.json")
    if index.get("v") != 2:
        index = {}  # rows before v2 carry no token counts: rebuild once
    files, replies = index.get("files") or {}, index.get("replies") or {}
    root = os.path.expanduser("~/.claude/projects")
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            size, mtime = os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            continue
        if mtime < now - SPEND_KEEP:
            continue
        offset = (files.get(path) or {}).get("offset", 0)
        if offset > size:
            offset = 0  # rewritten transcript
        if offset == size:
            continue
        read = 0
        for line in new_lines(path, offset):
            read += len(line)
            if b'"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
                message = entry.get("message") or {}
                at = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError, KeyError, TypeError):
                continue
            usage, source = message.get("usage"), reply_source(message.get("model"))
            if not isinstance(usage, dict) or not source or not message.get("id") or at < now - SPEND_KEEP:
                continue
            usd = usage_cost(message.get("model"), usage)
            if usd is not None:
                sent = sum(usage.get(f) or 0 for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                # the last line of a reply carries its final usage
                replies[message["id"]] = [at, source, usd, sent, usage.get("cache_read_input_tokens") or 0]
        files[path] = {"offset": offset + read}
    replies = {mid: r for mid, r in replies.items() if r[0] >= now - SPEND_KEEP}
    buckets = {}
    for at, source, *row in replies.values():  # bucket: [usd, sent input tokens, cache-read tokens]
        slot = buckets.setdefault(source, {})
        key = str(int(at // SPEND_BUCKET * SPEND_BUCKET))
        slot[key] = [a + b for a, b in zip(slot.get(key, [0, 0, 0]), row)]
    save_json("spend-index.json", {"v": 2, "files": {p: v for p, v in files.items() if os.path.exists(p)}, "replies": replies})
    save_json("spend.json", {"at": now, "buckets": buckets})


def spend_buckets(now):
    """{source: {bucket start: [usd, sent, read]}}, refreshed by a background `--spend` run at most once a minute."""
    saved = load_json("spend.json")
    if now - saved.get("at", 0) >= 60 and now - saved.get("claimed", 0) >= 300:
        save_json("spend.json", {**saved, "claimed": now})  # the first scan reads a week of transcripts
        try:
            subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "--spend"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass
    return saved.get("buckets") or {}


def money(usd):
    """`$0.54`, `$62`, `$1.5k`."""
    return f"${usd / 1000:.1f}k" if usd >= 1000 else f"${usd:.0f}" if usd >= 10 else f"${usd:.2f}"


def weekly_value(source, limits, buckets, now):
    """(this 7-day window's Claude Code spend at API list price, what a full window is worth, share of sent
    input tokens read from cache), or None.

    The full window is the spend divided by the window's used share.
    shortcut: usage outside Claude Code (claude.ai chat, Codex CLI, other PCs) also fills the quota, so the
    full value reads low when there is any; per-machine token logs would be the upgrade."""
    lim = (limits or {}).get("seven_day")
    if not isinstance(lim, dict) or not isinstance(lim.get("resets_at"), (int, float)) or lim["resets_at"] < now:
        return None
    start = lim["resets_at"] - WINDOW_SECONDS["seven_day"]
    rows = [row if isinstance(row, list) else [row, 0, 0]  # a spend.json from before token counts
            for at, row in (buckets.get(source) or {}).items() if float(at) + SPEND_BUCKET > start]
    spent, sent, read = (sum(col) for col in zip(*rows)) if rows else (0, 0, 0)
    pct = lim.get("used_percentage") or 0
    # below 2% the integer share is mostly rounding
    return spent, (spent * 100 / pct if spent and pct >= 2 else None), (read * 100 // sent if sent else None)


def provider_week(source, now):
    """(plan label, week text): Claude's and ChatGPT's `7d $1,523/$13.6k 98% cached │ Ultra` (spend / estimated
    full-week value), else the quota the provider reports itself (`CmdC $4.05 left`, `Go 7d 23% used`)."""
    if source in ("anthropic", "openai"):
        if source == "anthropic":
            account = claude_json().get("oauthAccount") or {}
            saved = load_json("quota.json")
            if not account.get("accountUuid") or saved.get("account") != account["accountUuid"]:
                return "Claude", ""
            plan = plan_name(account) or "Claude"
        else:
            saved, _ = provider_quota("openai", now)  # refreshes itself in the background like the status line's
            plan = re.sub(r"^ChatGPT ", "", saved.get("plan") or "ChatGPT").replace("Prolite", "Pro Lite")
        limits = saved.get("rate_limits") or {}
        week = weekly_value(source, limits, spend_buckets(now), now)
        if week is None:
            return plan, ""
        spent, full, cached = week
        pct = (limits.get("seven_day") or {}).get("used_percentage")
        week = f"${spent:,.0f}" if spent >= 100 else f"${spent:.2f}"
        week = f"7d {week} {pct}%" if full is None else f"7d {week}/{money(full)}"
        return plan, week + (f" {cached}% cached" if cached is not None else "") + f" │ {plan}"
    q, _ = provider_quota(source, now)
    label = {"commandcode": "CmdC", "opencode-go": "Go", "openrouter": "OpenRouter", "hyper": "Hyper"}.get(source, source)
    if q.get("extra"):
        return label, f"{label} {q['extra']}"
    for key in ("seven_day", "month", "five_hour"):
        pct = ((q.get("rate_limits") or {}).get(key) or {}).get("used_percentage")
        if pct is not None:
            return label, f"{label} {WINDOW_LABELS[key]} {pct}% used"
    return label, ""


def mode_row(session_id, now):
    """`≈ $2.03 (7d $1,523/$13.5k │ Ultra) · ≈ $0.54 (CmdC $4.05 left)`: per provider
    this session ran on (main and its ccx subagents), the session's API-price cost (from $0.00 at start), then the week's."""
    import glob
    if not re.fullmatch(r"[0-9a-f-]{36}", str(session_id or "")):
        return ""
    paths = glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"), "*", session_id + ".jsonl"))
    launch = os.environ.get("CCX_LAUNCH_ID")
    route = (load_json(os.path.expanduser("~/.config/ccx/routes.json")) if launch else {}).get(launch) or {}
    main = route.get("provider") if route.get("model") else ccx_main_provider(quota_source())
    profiles = ccx_profiles(session_id)
    # a fresh session (no transcript or reply yet) still shows $0.00 and the week on its main route
    spent = {CCX_QUOTA_SOURCES.get(main) or ("hyper" if main == "hyper" else main or "?"): [0.0, 0.0]}  # [total, subagents]
    runs = [(main, transcript_cost(paths[0]), False)] if paths else []
    # a native subagent (Explore, general-purpose) runs on the main route; a ccx profile on its own
    runs += [((profiles.get(name) or {}).get("provider", main) if name in profiles else main, cost, True)
             for name, cost in subagent_costs(paths[0]).items()] if paths else []
    for provider, cost, sub in runs:
        if cost.get("sent") or cost.get("priced"):
            key = CCX_QUOTA_SOURCES.get(provider) or ("hyper" if provider == "hyper" else provider or "?")
            row = spent.setdefault(key, [0.0, 0.0])
            row[0] += cost.get("usd", 0.0)
            row[1] += cost.get("usd", 0.0) if sub else 0.0
    parts = []
    for key, (usd, sub) in spent.items():
        label, week = provider_week(key, now) if key in QUOTA_FETCHERS or key == "anthropic" else (key, "")
        # the picker draws everything after the total dim, so the subagents' share reads as a dimmed aside
        parts.append(f"≈ ${usd:,.2f}{f' · agents {money(sub)}' if sub >= 0.005 else ''} ({week or label})")
    return " · ".join(parts)


def render(d):
    model = d.get("model") or {}
    effort = d.get("effort")
    # Ultracode: payload flag (top level or under effort), effort level "ultracode", or the settings key.
    ultra = d.get("ultracode") is True or (isinstance(effort, dict) and effort.get("ultracode") is True)
    if isinstance(effort, dict):
        effort = effort.get("level")
    effort = effort or settings_effort(model.get("id"))
    ultra = ultra or effort == "ultracode" or settings_ultracode() or keyword_ultracode(d.get("transcript_path"))
    if effort == "ultracode":
        effort = None
    # /model's Model tab switched this ccx session (ccx route): Claude Code still reports the launch model.
    launch = os.environ.get("CCX_LAUNCH_ID")
    route = (load_json(os.path.expanduser("~/.config/ccx/routes.json")) if launch else {}).get(launch)
    helper_picks = (route or {}).get("helpers") or {}
    route = route if route and route.get("model") else None
    if route:
        model = {"id": route["model"], "display_name": route["model"].rsplit("/", 1)[-1]}
        effort = route.get("effort") or effort
    source = quota_source()
    main_provider = route["provider"] if route else ccx_main_provider(source)
    if route:
        source = CCX_QUOTA_SOURCES.get(main_provider)
    mid = model.get("id") or ""
    # Claude keeps Anthropic's clay; other vendors get a stable hue from their name.
    hue = CLAUDE_HUE if not is_third_party(mid) else zlib.crc32(mid.split("/")[0].encode()) % 360
    label = model.get('display_name') or mid or '?'
    if main_provider == "official":
        label = re.sub(r'^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?$',
                       lambda m: m[1].title() + ' ' + m[2] + ('.' + m[3] if m[3] else ''), label)
    head = f"{rgb(hue, 0.45)}{BOLD}{label}{RESET}"
    if effort:
        # One circle per step up the model's own reasoning ladder, cool for low, hot for its top.
        head += " " + effort_indicator(effort, label=False, levels=effort_levels(main_provider, mid))
    if ultra:
        # Rainbow letters; the start hue drifts with the clock so each refresh shimmers.
        word, start = "ULTRACODE", time.time() * 40
        head += " " + BOLD + "".join(f"{rgb(start + i * 360 / len(word), 0.55, 1.0)}{ch}" for i, ch in enumerate(word)) + RESET
    parts = [head]

    cw = d.get("context_window") or {}
    usage = cw.get("current_usage") or last_usage(d.get("transcript_path"))
    ctx = sum(usage.get(f) or 0 for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    size = cw.get("context_window_size") or 200_000
    limit = compact_limit(model.get("id"), size)
    if ctx:
        pct = cw.get("used_percentage")
        pct = ctx * 100 // size if pct is None else pct
        parts.append(f"{bar(pct)} {DIM}{k(ctx)}/{k(size)}{RESET}"
                     + (f" {DIM}ac {k(limit)}{RESET}" if limit else "")
                     + cached_text(transcript_cost(d.get("transcript_path"))))

    now = time.time()
    stored, creds = claude_json(), credentials()
    extra, on_api = None, source == "anthropic" and api_billing(d, stored, creds)
    if source == "anthropic":
        # Claude's quota arrives with the first reply; replay the cache until then. An API key
        # bills the session per token, so neither the payload nor a cached copy describes it.
        limits, cached_at = ({} if on_api else d.get("rate_limits")), None
        limits = limits if isinstance(limits, dict) else {}
        account = stored.get("oauthAccount") or {}
        uuid = account.get("accountUuid")
        if limits:
            before = ((load_json("quota.json").get("rate_limits") or {}).get("seven_day") or {}).get("used_percentage")
            save_json("quota.json", {"rate_limits": limits, "at": now, "account": uuid})
            week = (limits.get("seven_day") or {}).get("used_percentage")
            if week != before:
                # each step of the weekly %, to fit per-model quota cost against transcript spend later
                try:
                    with open(os.path.join(STATE_DIR, "pct-log.jsonl"), "a", encoding="utf-8") as f:
                        f.write(json.dumps({"at": now, "account": uuid, "seven_day": week}) + "\n")
                except OSError:
                    pass
        elif not on_api:
            saved = load_json("quota.json")
            limits, cached_at = saved.get("rate_limits"), saved.get("at")
            # A cache written under another login is that account's quota, not this one's.
            if saved.get("account") != uuid or not isinstance(limits, dict) or not isinstance(cached_at, (int, float)):
                limits, cached_at = {}, None
        plan = None if on_api else plan_name(account)
    elif source:
        q, cached_at = provider_quota(source, now)
        limits, plan, extra = q.get("rate_limits") or {}, q.get("plan"), q.get("extra")
    else:
        # Adapter routes: the x-ratelimit-* windows ccx-anthropic-proxy saw on the last reply.
        q = load_json(f"ratelimit-{main_provider}.json")
        limits, cached_at, plan = q.get("rate_limits") or {}, q.get("at"), None
    shown = quota_parts(plan, extra, limits, cached_at, now)
    if source and source != 'anthropic' and q.get('error'):
        reset = q.get('last_reset_at')
        suffix = f" · reset in {span(reset - now)} (last known)" if isinstance(reset, (int, float)) and reset > now else ''
        parts += shown or [f"{YELLOW}{q['error']}{suffix}{RESET}"]
    else:
        parts += shown
    # The session's fallback route (ccx fallback); yellow while it served within the last 5 minutes.
    fallback = (load_json(os.path.expanduser("~/.config/ccx/fallbacks.json")) if launch else {}).get(launch)
    if fallback:
        served = load_json(f"fallback-{launch}.json").get("at") or 0
        label = f"fallback {fallback['provider']}/{fallback['model']}"
        parts.append(f"{YELLOW}↪ {label} active{RESET}" if now - served < 300 else f"{DIM}↪ {label}{RESET}")
    # Provider labels are omitted; the picker owns route selection.

    # Configured targets are visible even on official Claude and even on the same provider.
    # Live worker rows separately identify native workers and the real ccx relay/worker route.
    sub_lines = []
    if launch:
        helper_rows = []
        for kind in ("auto", "tools"):
            pick = helper_picks.get(kind)
            default = (load_json(os.path.expanduser("~/.config/ccx/helper-defaults.json")).get(kind) or {}).get(main_provider)
            target = pick or default
            provider = target.get("provider", main_provider) if target else main_provider
            helper_model = target.get("model") if target else model.get("id") or "provider default"
            helper_effort = target.get("effort") if target else None
            if kind == "tools" and not helper_effort:
                levels = effort_levels(provider, helper_model)
                helper_effort = "low" if provider == "official" or levels is not None and "low" in levels else effort
            if kind == "auto" and not target and provider == "openai-oauth":
                levels = effort_levels(provider, "gpt-6-luna")
                if levels and "low" in levels:
                    helper_model, helper_effort = "gpt-6-luna", "low"
            if provider == "official":
                helper_model = (re.sub(r'^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?$',
                    lambda m: m[1].title() + ' ' + m[2] + ('.' + m[3] if m[3] else ''), helper_model)
                    if target or kind == "tools" else "Claude helper default")
            dots = " " + effort_indicator(helper_effort, label=False, levels=effort_levels(provider, helper_model)) if helper_effort else ""
            helper_rows.append(f"{kind.title()}: {helper_model}{dots}{'' if pick else ' · inherit'}")
        sub_lines.append(f"{DIM}↳{RESET} " + SEP.join(helper_rows))
    child = os.environ.get("CCX_AGENT_CHILD") == "1"
    configured = not child and ccx_routing_enabled()
    if configured:
        # a continued session keeps writing the transcript its workers name as their parent, under a new session id
        moved = {**ccx_fallback_jobs(os.path.splitext(os.path.basename(d.get("transcript_path") or ""))[0]),
                 **ccx_fallback_jobs(d.get("session_id"))}
        # shortcut: jobs don't record their agent name, so any running worker lights every configured row
        active = bool(ccx_running_jobs(os.path.splitext(os.path.basename(d.get("transcript_path") or ""))[0])
                      or ccx_running_jobs(d.get("session_id")))
        sub_costs = subagent_costs(d.get("transcript_path"))
        rows = ccx_profiles(d.get("session_id"))
        offered = set(rows)
        # a profile the session no longer offers (sub under the router) keeps its row while it has runs to show
        every = ccx_agents(d.get("session_id"), offered=False)
        # ...and clears once those workers have been quiet for 10 minutes; their spend stays in the agents total
        rows.update({n: every[n] for n, used in sub_costs.items() if n not in rows and isinstance(every.get(n), dict)
                     and now - (used.get("at") or 0) < 600})
        ranges = level_ranges(rows)
        # Several worker rows fold into one compact line ("v-easy haiku ●● · mid sonnet ●●● $100"), so
        # Claude's own agent panel below keeps its room; a lone profile keeps its detailed row.
        folded = [] if len(rows) > 1 else None
        order = lambda kv: ROUTER_LEVELS.index(kv[0]) if kv[0] in ROUTER_LEVELS else len(ROUTER_LEVELS)
        for name, profile in sorted(rows.items(), key=order):  # levels easy to hard, other profiles after
            idle = name not in offered
            # a router level shows its slice of the 0-100 difficulty scale: "easy 0-60"
            label = f"{name} {ranges[name][0]}-{ranges[name][1]}" if name in ranges and not idle and len(ranges) > 1 else name
            # a worker that ccx reran on its fallback shows the route it really runs on, not the configured one
            profile = {**profile, **{k: v for k, v in moved.get(name, {}).items() if k in ("provider", "model", "effort")}}
            provider = profile.get("provider", "official")
            mid = profile.get('model') or 'Claude default'
            target = re.sub(r'^(?:gpt|claude)-', '', mid.rsplit('/', 1)[-1], flags=re.I)
            target = re.sub(r'^(opus|sonnet|haiku)-(\d+)-(\d+)', r'\1 \2.\3', target).replace('-', ' ')
            labels = {'openai-oauth': 'OAuth', 'official': 'Claude', 'nvidia': 'NIM',
                      'commandcode': 'CmdC', 'commandcode-cli': 'CmdC CLI', 'minimax': 'MiniMax',
                      'opencode-go': 'Go', 'opencode-zen': 'Zen', 'openrouter': 'OpenRouter',
                      'gemini': 'Gemini', 'ollama-cloud': 'Ollama', 'lmstudio': 'LM Studio'}
            route = '' if provider == main_provider else ' · ' + labels.get(provider, provider)  # same provider: no label
            effort = profile.get('effort') or default_effort(provider, profile.get('model'))
            dots = (effort_indicator(effort, dim=not active, label=False, levels=effort_levels(provider, mid))
                    if effort else DIM + 'default' + RESET)
            hue = CLAUDE_HUE if not is_third_party(mid) else zlib.crc32(mid.split('/')[0].encode()) % 360
            # a row kept only for its runs says so: its workers predate the current mode (e.g. sub before the router)
            head = (f"{DIM}↳ {name} (earlier):{RESET}" if idle
                    else f"↳ {label}:" if active else f"{DIM}↳ {label}:{RESET}")
            own = profile.get('autocompact')
            # A profile without its own limit inherits the main session's (ccx-switch parent_compact).
            ac = (f" {DIM}ac {own}{'%' if type(own) is int else ''}{RESET}" if own
                  else f" {DIM}ac {k(limit)}{RESET}" if limit else '')
            used = sub_costs.get(name) or {}
            if folded is not None:
                # shortest form that still tells the levels apart: "v-easy haiku · mid sonnet $100"
                short = name.replace("very-", "v-")
                folded.append(f"{DIM if not used.get('ctx') else ''}{short} {rgb(hue, 0.45, 0.9 if active else 0.6)}{target.split()[0]}{RESET} {dots}"
                              + (f" {DIM}{money(used['usd'])}{RESET}" if used.get("priced") else "")
                              + (f" {YELLOW}↪{RESET}" if name in moved else ""))
                continue
            seg = [f"{head} {rgb(hue, 0.45, 0.9 if active else 0.6)}{target}{RESET} {dots}{'' if used.get('ctx') else ac}{route}"
                   + (f" {YELLOW}↪ fallback active{RESET}" if name in moved else "")]
            if used.get("ctx"):
                # shortcut: the window is the main session's; a subagent model with another window misreads it
                seg.append(f"{DIM}{k(used['ctx'])}/{k(size)}{RESET}{ac}{cached_text(used)}"
                           + (f" {DIM}{money(used['usd'])}{RESET}" if used.get("priced") else ""))
            else:
                # every row has its usage cell, so rows line up and an unused level reads as such
                seg.append(f"{DIM}no runs yet{RESET}")
            sub_source = CCX_QUOTA_SOURCES.get(provider)
            if sub_source and sub_source != source:
                if sub_source == 'anthropic':
                    saved = load_json('quota.json')
                    account = stored.get('oauthAccount') or {}
                    q, sub_cached = {}, None
                    if saved.get('account') == account.get('accountUuid') and isinstance(saved.get('at'), (int, float)):
                        q, sub_cached = {'plan': plan_name(account), 'rate_limits': saved.get('rate_limits')}, saved['at']
                else:
                    q, sub_cached = provider_quota(sub_source, now)
                plan = (q.get('plan') or '').removeprefix('ChatGPT ')
                shown = quota_parts(plan, q.get('extra'), q.get('rate_limits') or {}, sub_cached, now)
                seg += shown or ([f"{YELLOW}{q['error']}{RESET}"] if q.get('error') else [])
            elif not sub_source and provider != main_provider:
                # Adapter routes: the x-ratelimit-* windows ccx-anthropic-proxy saw on its last reply.
                q = load_json(f"ratelimit-{provider}.json")
                seg += quota_parts(None, None, q.get('rate_limits') or {}, q.get('at'), now)
            sub_lines.append(SEP.join(seg))
        if folded:
            sub_lines.append(f"{DIM}↳{RESET} " + f"{DIM} · {RESET}".join(folded))
    for sub_source, names in ({} if configured or child or source == "anthropic" else subagent_sources(d.get("session_id"))).items():
        if sub_source == source:
            continue  # same account as the session, already shown
        if sub_source == "anthropic":
            # Only a Claude session receives this quota; replay its last copy for this login.
            saved = load_json("quota.json")
            account = stored.get("oauthAccount") or {}
            if saved.get("account") != account.get("accountUuid") or not isinstance(saved.get("at"), (int, float)):
                continue
            q, sub_cached = {"plan": plan_name(account), "rate_limits": saved.get("rate_limits")}, saved["at"]
        else:
            q, sub_cached = provider_quota(sub_source, now)
        seg = quota_parts(q.get("plan"), q.get("extra"), q.get("rate_limits") or {}, sub_cached, now)
        if seg:
            sub_lines.append(f"{DIM}↳ subagent {', '.join(names)}{RESET}" + SEP + SEP.join(seg))

    for item in [] if child else ccx_orphans(d.get("session_id")):
        state = (f"{BOLD}result unread{RESET} · reaches Claude with your next message" if item["done"]
                 else "running, relay gone")
        tokens = f" · {DIM}{item['tokens']}{RESET}" if item.get("tokens") else ""
        sub_lines.append(f"{DIM}↳ worker {item['job'][:8]}:{RESET} {state} · {item.get('task') or ''}{tokens}")

    # The session's API-price total sits on Claude Code's mode row (subagent-picker pins `--cost`);
    # here only an API-key session without a transcript yet shows Claude Code's own figure.
    cost = d.get("cost") or {}
    if is_third_party(model.get("id")) or d.get("transcript_path"):
        pass  # Claude Code prices unknown models at Opus rates, so its cost is fiction there.
    elif source == "anthropic" and (on_api or ("rate_limits" not in d and not subscription_login(creds) and cost.get("total_cost_usd"))):
        spent = cost.get("total_cost_usd") or 0  # an untouched keyed session reads $0.000
        parts.append(f"api {YELLOW}{f'${spent:,.2f}' if spent >= 1 else f'${spent:.3f}'}{RESET}")
    # Subagent spend shows per subagent row and in the mode row's `≈ … · agents …` total.

    state = cache_state(d, main_provider, source, model.get("id") or "", now)
    # Warm: reply within the countdown to reuse cached context (fast, cheap). Cold: next reply
    # re-processes the whole context. settings.json refreshInterval keeps the countdown ticking.
    if state:
        used, left = state
        parts.append(f"cache {color(used)}{BOLD if used >= 90 else ''}{span(left)}{RESET}")
    elif source == "anthropic" and not is_third_party(model.get("id")) and (d.get("prompt_cache") or {}).get("warm") is not None:
        parts.append(f"cache {color(100)}off{RESET}")
    return "\n".join([SEP.join(parts), *sub_lines])


if __name__ == "__main__":
    # A hung render (stdin, network, file lock) must never outlive its refresh: Claude Code starts a new one every 2 s.
    import threading
    last = {}  # the session id once stdin is read; Claude Code blanks the status line on an empty run

    def expire():
        # a render past its budget reprints this session's last good line rather than leaving it blank
        shown = load_json(f"last-{last['sid']}.json").get("line") if last.get("sid") else None
        if shown:
            print(shown, flush=True)
        os._exit(0)
    watchdog = threading.Timer(30 if sys.argv[1:2] == ["--quota"] else 8, expire)
    watchdog.daemon = True  # a normal render must not wait for it
    watchdog.start()
    if sys.argv[1:2] == ["--quota"]:
        fetch_quota(sys.argv[2], time.time())
        sys.exit()
    if sys.argv[1:2] == ["--spend"]:
        index_spend(time.time())
        sys.exit()
    if sys.argv[1:2] == ["--cost"]:
        sys.stdout.reconfigure(encoding="utf-8")
        print(mode_row(sys.argv[2] if len(sys.argv) > 2 else "", time.time()))
        sys.exit()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    sid = data.get("session_id") if isinstance(data, dict) else None
    last["sid"] = sid if isinstance(sid, str) and sid.replace("-", "").isalnum() else None
    try:
        line = render(data if isinstance(data, dict) else {})
        if last["sid"]:
            save_json(f"last-{last['sid']}.json", {"line": line})
    except Exception:  # a broken status line is worse than a bare one
        line = str((data.get("model") or {}).get("id") if isinstance(data, dict) and isinstance(data.get("model"), dict) else "?")
    print(line)
