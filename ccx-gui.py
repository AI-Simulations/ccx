"""ccx's model picker as its own dark window (stdlib Tkinter, no dependencies).

Tabs mirror the /model grid: Model, Subagent, Fallbacks, Auto, Tools and Presets. Every list has a
search field; the model lists group by family on demand. Apply runs the same ccx commands the
terminal menus run, so the session route, profiles and fallbacks stay one source of truth.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import tempfile
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont, simpledialog, ttk

CCX = Path(__file__).with_name('ccx-switch.py')
TABS = [('model', 'Model'), ('subagent', 'Subagent'), ('fallback', 'Fallbacks'),
        ('auto', 'Auto'), ('tools', 'Tools'), ('presets', 'Presets')]
TARGET_TABS = ('subagent', 'fallback')
ROUTER_LEVELS = ('very-easy', 'easy', 'mid', 'hard', 'very-hard')  # ccx-switch.py's router levels, in order
# the difficulty bar's colours, cool to hot
LEVEL_COLORS = {'very-easy': '#2f9e77', 'easy': '#3f8fd2', 'mid': '#c99a2e', 'hard': '#d9722e', 'very-hard': '#d6453d'}

# Dark by default, independent of the desktop's light/dark setting.
BG, PANEL, FIELD, HOVER = '#0d1117', '#141a22', '#19202a', '#243042'
FG, DIM, FAINT, ACCENT = '#e8eef7', '#a3afc2', '#76839a', '#6b8cff'
SELECTED, CURRENT = '#27426b', '#17352d'
ZEBRA, BORDER = '#161d26', '#252e3b'
COST_BG, COST_FG = '#1a2a22', '#7fe0a8'
# the header rule and other accents: blue through violet to teal
GRADIENT = ('#6b8cff', '#a77bff', '#3fc7b0')


def mix(a, b, t):
    """The colour t of the way from a to b (hex #rrggbb)."""
    x, y = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return '#' + ''.join(f'{round(p + (q - p) * t):02x}' for p, q in zip(x, y))


def ramp(colors, t):
    """A point 0..1 along a multi-stop gradient."""
    t = min(max(t, 0.0), 1.0) * (len(colors) - 1)
    i = min(int(t), len(colors) - 2)
    return mix(colors[i], colors[i + 1], t - i)


class GradientRule(tk.Canvas):
    """A thin horizontal gradient line that refills its width."""
    def __init__(self, master, colors=GRADIENT, height=2):
        super().__init__(master, height=height, bg=BG, highlightthickness=0, bd=0)
        self.colors = colors
        self.bind('<Configure>', lambda e: self.paint(e.width, e.height))

    def paint(self, width, height):
        self.delete('all')
        step = 4
        for x in range(0, max(width, 1), step):
            self.create_rectangle(x, 0, x + step, height, fill=ramp(self.colors, x / max(width - 1, 1)), width=0)


def dark_title_bar(window):
    """Windows 10/11 draw the title bar dark to match (DWMWA_USE_IMMERSIVE_DARK_MODE); elsewhere nothing."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        on = ctypes.c_int(1)
        for attribute in (20, 19):  # 19 on Windows 10 before 20H1
            if not ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)):
                break
    except (OSError, AttributeError):
        pass
# Each desktop's own UI and code faces; Tk substitutes the nearest installed one.
UI = 'Segoe UI' if os.name == 'nt' else 'Helvetica Neue' if sys.platform == 'darwin' else 'Cantarell'
FONT = (UI, 10)
BOLD = (UI, 10, 'bold')
SMALL_BOLD = (UI, 9, 'bold')
MONO = ('Consolas' if os.name == 'nt' else 'Menlo' if sys.platform == 'darwin' else 'DejaVu Sans Mono', 10)
LOADING_MODELS = 'Loading models…'  # the model list's only row while it loads
BUSY = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
# Claude Code's own ladder, used when a provider publishes none (as the launch menu does).
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']

# The browsing view (provider, pick, sort, search, effort) kept per session, so reopening the
# window lands where the person left it. Applying is still explicit: this never routes anything.
VIEW_FILE = Path.home() / '.config/ccx/gui-view.json'


def view_key():
    return os.environ.get('CCX_LAUNCH_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID') or 'default'


def load_view():
    try:
        data = json.loads(VIEW_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def session_view():
    """The tabs' browsing state for this launch, never another session's."""
    entry = load_view().get(view_key())
    return entry if isinstance(entry, dict) else {}


def save_view(tab, entry):
    data = load_view()
    session = data.get(view_key())
    if not isinstance(session, dict):
        session = {}
    session[tab] = entry
    data[view_key()] = session
    try:
        VIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        VIEW_FILE.write_text(json.dumps(data, indent=1), encoding='utf-8')
    except OSError:
        pass


VIEW_VERSION = 2


def migrate_view(entry):
    """A view saved before Intelligence was the default keeps Family only if it was a real pick."""
    if not isinstance(entry, dict):
        return {}
    if 'v' not in entry and entry.get('sort') == 'Family':
        return {**entry, 'sort': 'Intelligence'}
    return entry

FAMILIES = [
    (('claude', 'sonnet', 'opus', 'haiku', 'fable'), 'Claude'),
    (('gpt', 'chatgpt'), 'GPT'),
    (('gemini', 'gemma'), 'Gemini'),
    (('deepseek',), 'DeepSeek'),
    (('glm', 'zhipu', 'z-ai'), 'GLM'),
    (('qwen', 'tongyi'), 'Qwen'),
    (('kimi', 'moonshot'), 'Kimi'),
    (('llama', 'meta-llama'), 'Llama'),
    (('mistral', 'codestral', 'devstral'), 'Mistral'),
    (('minimax',), 'MiniMax'),
    (('mimo', 'xiaomi'), 'MiMo'),
    (('grok', 'x-ai'), 'Grok'),
    (('nemotron',), 'Nemotron'),
    (('phi-',), 'Phi'),
]

# Badge colours for providers without a mark: each provider's own brand colour.
BRAND_COLORS = {
    'official': '#d97757',
    'commandcode': '#4f8cff',
    'commandcode-cli': '#4f8cff',
    'opencode-go': '#14b8a6',
    'opencode-zen': '#14b8a6',
    'openai-oauth': '#10a37f',
    'openrouter': '#8b5cf6',
    'nvidia': '#76b900',
    'gemini': '#4285f4',
    'minimax': '#e64c3c',
    'ollama-cloud': '#52525b',
    'lmstudio': '#7c3aed',
}


# 3x5 pixel glyphs for initials badges: Tk images cannot draw text, and this needs no font or PIL.
GLYPHS = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789?', (
    '010101111101101', '110101110101110', '011100100100011', '110101101101110', '111100110100111',
    '111100110100100', '011100101101011', '101101111101101', '111010010010111', '001001001101010',
    '101110100110101', '100100100100111', '101111111101101', '110101101101101', '010101101101010',
    '110101110100100', '010101101111011', '110101110110101', '011100010001110', '111010010010010',
    '101101101101111', '101101101101010', '101101111111101', '101101010101101', '101101010010010',
    '111001010100111', '111101101101111', '010110010010111', '110001010100111', '110001010001110',
    '101101111001001', '111100110001110', '011100110101010', '111001010010010', '010101010101010',
    '010101011001110', '110001010000010')))


# Providers whose names would collide as two letters (opencode, openai, openrouter).
BADGE_TEXT = {'official': 'C', 'commandcode': 'CM', 'commandcode-cli': 'CM', 'opencode-go': 'OC',
              'opencode-zen': 'OC', 'openai-oauth': 'AI', 'openrouter': 'OR', 'ollama-cloud': 'OL',
              'gemini': 'G', 'minimax': 'MM', 'nvidia': 'NV', 'lmstudio': 'LM',
              'MiMo': 'XI'}  # Xiaomi's MiMo would read MM, like MiniMax


def initials(name):
    """Two letters for a badge: the capitals of `DeepSeek` (DS), else the first two letters."""
    if name in BADGE_TEXT:
        return BADGE_TEXT[name]
    letters = re.sub(r'[^A-Za-z0-9]', '', name or '')
    capitals = [c for c in letters[1:] if c.isupper()]
    return ((letters[:1] + capitals[0]) if capitals else letters[:2]).upper() or '?'


def badge_color(name):
    """A steady, readable hue per name, so a badge keeps its colour between runs."""
    if name in BRAND_COLORS:
        return BRAND_COLORS[name]
    import colorsys
    import zlib
    red, green, blue = colorsys.hsv_to_rgb(zlib.crc32((name or '?').lower().encode()) % 360 / 360, 0.45, 0.62)
    return f'#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}'


# Real marks, kept locally in ~/.config/ccx/logos (Simple Icons, CC0) and loaded when present.
LOGO_DIR = Path.home() / '.config/ccx/logos'
PROVIDER_LOGOS = {
    'official': 'claude',
    'openai-oauth': 'openai',
    'opencode-go': 'opencode',
    'opencode-zen': 'opencode',
    'openrouter': 'openrouter',
    'nvidia': 'nvidia',
    'gemini': 'gemini',
    'minimax': 'minimax',
    'ollama-cloud': 'ollama',
    'lmstudio': 'lmstudio',
}
FAMILY_LOGOS = {
    'Claude': 'claude',
    'GPT': 'openai',
    'Gemini': 'gemini',
    'DeepSeek': 'deepseek',
    'GLM': 'glm',
    'Qwen': 'qwen',
    'Kimi': 'moonshot',
    'Llama': 'meta',
    'Mistral': 'mistral',
    'MiniMax': 'minimax',
    'MiMo': 'xiaomi',
    'Grok': 'xai',
    'Nemotron': 'nvidia',
}


def model_family(model_id):
    """The family a model id belongs to, as the terminal picker groups it."""
    tail = model_id.rsplit('/', 1)[-1].strip().lower()
    for words, family in FAMILIES:
        if any(word in tail for word in words):
            return family
    for words, family in FAMILIES:
        if any(word in model_id.lower() for word in words):
            return family
    letters = ''
    for ch in tail:
        if not ch.isalpha():
            break
        letters += ch
    return letters[:1].upper() + letters[1:].lower() if len(letters) > 1 else 'Other'


def family_sorted(models):
    return sorted(models, key=lambda m: (model_family(m), m.lower()))


def filtered(models, query):
    needle = query.strip().lower()
    return [m for m in models if not needle or needle in m.lower()]


# The order the launch model menu uses: best first, rows missing a displayed fact last.
SORTS = {'Intelligence': 'intelligence', 'Score': 'score', 'Context': 'context',
         'Price': 'price', 'Family': 'family', 'Listed': 'listed'}


def tokens(n):
    """1048576 -> '1M', 131072 -> '131K'."""
    return f'{n / 1e6:.3g}M' if n >= 999_500 else f'{round(n / 1e3)}K'


def facts_text(info):
    """'ctx 1M · $4/$20 · AA 45 · → 72', as the terminal menu writes it, minus the colors."""
    if not info:
        return ''
    context = info.get('ctx')
    parts = [f'ctx {tokens(context)}' if context else 'ctx ?']
    price_in, price_out = info.get('in'), info.get('out')
    if price_in is not None and price_out is not None:
        parts.append('free' if not (price_in or price_out) else f'${price_in:.3g}/${price_out:.3g}')
    else:
        parts.append('$?')
    score = info.get('aa')
    parts.append(f'AA {score:.0f}' if score is not None else 'AA ?')
    if info.get('score') is not None:
        parts.append(f'→ {info["score"]:.0f}')
    return ' · '.join(parts)


def session_usage(session_id=None):
    """This session's tokens and API-price cost, from its transcript: {input, write, read, output, usd}.
    None before the first reply or outside a Claude Code session."""
    import glob
    session_id = session_id or os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    if not re.fullmatch(r'[0-9a-f-]{36}', session_id):
        return None
    paths = glob.glob(str(Path.home() / '.claude' / 'projects' / '*' / f'{session_id}.jsonl'))
    if not paths:
        return None
    spec = importlib.util.spec_from_file_location('ccx_statusline', Path(__file__).with_name('statusline.py'))
    statusline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(statusline)
    total, last = {'input': 0, 'write': 0, 'read': 0, 'output': 0, 'usd': 0.0}, None
    with open(paths[0], 'rb') as f:
        for line in f:
            if b'"usage"' not in line:
                continue
            try:
                message = json.loads(line).get('message') or {}
            except (ValueError, AttributeError):
                continue
            usage = message.get('usage')
            if not isinstance(usage, dict) or message.get('id') == last:
                continue  # one reply spans several lines with the same usage
            last = message.get('id')
            total['input'] += usage.get('input_tokens') or 0
            total['write'] += usage.get('cache_creation_input_tokens') or 0
            total['read'] += usage.get('cache_read_input_tokens') or 0
            total['output'] += usage.get('output_tokens') or 0
            total['usd'] += statusline.usage_cost(message.get('model'), usage) or 0.0
    return total if any(total[k] for k in ('input', 'write', 'read', 'output')) else None


def money(usd):
    return f'${usd:,.2f}' if usd >= 1 else f'${usd:.3f}'


def session_cost(info, usage):
    """What this session's tokens would cost at a model's list price; cache reads at 0.1x and
    writes at 1.25x input, as most providers bill them. None without a price or usage."""
    if not usage or not info or info.get('in') is None or info.get('out') is None:
        return None
    price_in, price_out = info['in'], info['out']
    return (usage['input'] * price_in + usage['write'] * price_in * 1.25 + usage['read'] * price_in * 0.1
            + usage['output'] * price_out) / 1e6


def fact_values(info, usage=None, effort=None):
    """One value per fact column; a fact the provider does not publish reads '?'. AA is the picked
    effort's score where Artificial Analysis rates that effort, else the model's best."""
    if not info:
        return ('?', '?', '?', '?', '')
    context = info.get('ctx')
    price_in, price_out = info.get('in'), info.get('out')
    if price_in is not None and price_out is not None:
        price = 'free' if not (price_in or price_out) else f'${price_in:.3g}/${price_out:.3g}'
    else:
        price = '?'
    return (tokens(context) if context else '?', price,
            f'{aa:.0f}' if (aa := (info.get('aa_efforts') or {}).get(effort, info.get('aa'))) is not None else '?',
            f'{info["score"]:.0f}' if info.get('score') is not None else '?',
            '' if (cost := session_cost(info, usage)) is None else '≈' + money(cost))


SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'
COMPACT_TOKENS = ['100k', '200k', '300k', '400k', '600k']


def compact_window(value):
    """Tokens of a "400k"/"1m" pick; None for anything else."""
    value = value.strip().lower()
    try:
        tokens = int(float(value[:-1]) * (1000 if value.endswith('k') else 1_000_000)) if value[-1:] in ('k', 'm') else None
    except ValueError:
        return None
    return tokens if tokens and 100_000 <= tokens <= 1_000_000 else None


def model_compact(model):
    """The main session's window for this model in Claude's settings, or 'global' for the shared one."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'global'
    window = ((settings.get('modelSettings') or {}).get(model) or {}).get('autoCompactWindow')
    return f'{window // 1000}k' if isinstance(window, int) else 'global'


def set_model_compact(model, value):
    """Store the model's autoCompactWindow ('global' removes it, so the shared window applies)."""
    tokens = None if value == 'global' else compact_window(value)
    if value != 'global' and tokens is None:
        return False, f"Compact must be 'global' or a token window from 100k to 1m, not '{value}'."
    settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    entry = dict((settings.setdefault('modelSettings', {})).get(model) or {})
    if tokens:
        entry['autoCompactWindow'] = tokens
    else:
        entry.pop('autoCompactWindow', None)
    if entry:
        settings['modelSettings'][model] = entry
    else:
        settings['modelSettings'].pop(model, None)
    tmp = SETTINGS_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(tmp, SETTINGS_FILE)
    return True, f'compact {value}'


def effort_values(levels, current=None):
    """The model's own ladder, else Claude's; the current pick stays offered either way."""
    values = ['default', *(levels or EFFORTS)]
    if current and current not in values:
        values.append(current)
    return values


def sort_models(models, facts, mode):
    """One of SORTS' modes: family, context, price, intelligence, score or the provider's order."""
    if mode == 'family':
        return family_sorted(models)
    if mode == 'listed':
        return list(models)

    def key(model):
        info = facts.get(model) or {}
        missing = info.get('missing') or 0
        if mode == 'context':
            value = info.get('ctx') or 0
            return (missing > 0, missing, value <= 0, -value)
        if mode == 'price':
            price_in, price_out = info.get('in'), info.get('out')
            priced = price_in is not None and price_out is not None
            return (missing > 0, missing, not priced,
                    price_out if priced else 0, price_in if priced else 0)
        if mode == 'intelligence':
            value = info.get('aa')
            return (missing > 0, missing, value is None, -(value or 0))
        value = info.get('score') or 0
        return (missing > 0, missing, -value)

    return sorted(models, key=key)


def tab_rows(out):
    """ccx's `value<TAB>label` picker rows."""
    rows = []
    for line in out.splitlines():
        if '\t' in line:
            value, label = line.split('\t', 1)
            if value:
                rows.append((value, label.strip() or value))
    return rows


def plain_rows(out):
    """ccx's `route/fallback models` listing: one id per line."""
    return [line.strip() for line in out.splitlines() if line.strip() and not line.strip().startswith('(')]


def indented_rows(out):
    """`agents models`: indented ids, the profile's current one starred."""
    return [line.strip().lstrip('* ').strip() for line in out.splitlines()
            if line[:1].isspace() and line.strip()]


def parse_target(out, name):
    """`Route: provider/model` (or Fallback/Helper) and an optional `Effort: level` line."""
    target = None
    for line in out.splitlines():
        if line.startswith(name + ': '):
            rest = line[len(name) + 2:].strip()
            if rest and '/' in rest:
                provider, model = rest.split('/', 1)
                target = [provider, model]
        elif line.startswith('Effort: ') and target:
            target.append(line.split(': ', 1)[1].strip())
    return target


def run_ccx(*args):
    """Run ccx beside this window, hidden; (ok, stdout-or-stderr)."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        done = subprocess.run([sys.executable, '-B', str(CCX), *args], capture_output=True,
                              text=True, creationflags=flags, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f'{type(exc).__name__}: {exc}'
    return done.returncode == 0, (done.stdout.strip() or done.stderr.strip())


def session_launch_id():
    """The key ccx files this session's settings under, or '' outside a session."""
    sid = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    return os.environ.get('CCX_LAUNCH_ID') or (f'claude-{sid}' if sid else '')


def session_entry():
    """This session's session-agents.json entry ({} when it has none)."""
    try:
        entry = json.loads((Path.home() / '.config/ccx/session-agents.json').read_text(encoding='utf-8')).get(session_launch_id())
    except (OSError, ValueError, AttributeError):
        return {}
    return entry if isinstance(entry, dict) else {}


class StatusLabel(ttk.Label):
    """A one-line status that never pushes its row's buttons away: it takes the room left
    and cuts a longer text to it with an ellipsis. cget('text') still gives the whole text."""

    def __init__(self, master, text='', **kw):
        super().__init__(master, text='', width=1, **kw)
        self.full = ''
        self.bind('<Configure>', lambda _e: self.fit())
        self.configure(text=text)

    def configure(self, cnf=None, **kw):
        if 'text' in kw:
            self.full = str(kw.pop('text'))
            super().configure(text=self.full)
            self.fit()
        return super().configure(cnf, **kw) if cnf or kw else None

    config = configure

    def cget(self, key):
        return self.full if key == 'text' else super().cget(key)

    def fit(self):
        room = self.winfo_width()
        text = ' '.join(self.full.split())  # one line: a multi-line message would grow the row
        if room > 1:
            measure = tkfont.Font(font=FONT).measure
            if measure(text) > room:
                low, high = 0, len(text)
                while low < high:  # the longest start that fits with its ellipsis
                    mid = (low + high + 1) // 2
                    low, high = (mid, high) if measure(text[:mid] + '…') <= room else (low, mid - 1)
                text = text[:low].rstrip() + '…'
        ttk.Label.configure(self, text=text)


LEVEL_TOPS = {'very-easy': 30, 'easy': 75, 'mid': 89, 'hard': 95}  # ccx-switch.py's default range tops


def level_name(name):
    return name.replace('-', ' ').capitalize()


def level_ranges(profiles):
    """ccx-switch.py's level_ranges: {level: (low, high)} on the 0-100 difficulty scale."""
    levels = [n for n in ROUTER_LEVELS if n in profiles]
    ranges, low = {}, 0
    for i, name in enumerate(levels):
        high = 100 if i == len(levels) - 1 else (profiles[name] or {}).get('upto', LEVEL_TOPS.get(name, 50))
        high = max(low, min(int(high), 100))
        ranges[name] = (low, high)
        low = min(high + 1, 100)
    return ranges


def subagent_text(preset):
    """A preset's subagents in a few words: 'Router · 5 levels' or the one model."""
    target = (preset.get('settings') or {}).get('subagent') or {}
    if target.get('provider') == 'router':
        return f"Router · {len(target.get('levels') or {})} levels"
    return short_model(target['model']) + (f' · {target["effort"]}' if target.get('effort') else '') if target.get('model') else ''


def short_model(model):
    """'claude-haiku-5-5' -> 'Haiku 5.5', 'deepseek-ai/deepseek-v4.1-flash' -> 'deepseek-v4.1-flash'."""
    name = re.sub(r'^claude-', '', str(model).rsplit('/', 1)[-1])
    m = re.match(r'^(opus|sonnet|haiku|fable)-(\d+)-(\d+)$', name)
    return f'{m[1].capitalize()} {m[2]}.{m[3]}' if m else name


def load_profiles():
    """agents.json profiles for this session, as the picker names them."""
    agents = {}
    try:
        agents = json.loads((Path.home() / '.config/ccx/agents.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    shared = agents if isinstance(agents, dict) else {}
    own = session_entry().get('agents')
    if isinstance(own, dict):
        # router levels added after this session saved its profiles reach it as set globally
        return {**own, **{n: a for n, a in shared.items() if n in ROUTER_LEVELS and n not in own}}
    return shared


class RangeBar(tk.Canvas):
    """The 0-100 difficulty scale cut into the router's levels. Each segment, in its level's colour, names
    its range, model and effort; drag a boundary to move where a level ends, click a segment to pick it."""
    HEIGHT = 40
    GRAB = 6  # px either side of a boundary that starts a drag

    def __init__(self, master, on_change, on_pick):
        super().__init__(master, height=self.HEIGHT, bg=BG, highlightthickness=0)
        self.on_change, self.on_pick = on_change, on_pick
        self.levels, self.tops, self.labels, self.selected, self.drag = [], [], {}, None, None
        self.bind('<Configure>', lambda _e: self.draw())
        self.bind('<Motion>', self.hover)
        self.bind('<ButtonPress-1>', self.press)
        self.bind('<B1-Motion>', self.move)
        self.bind('<ButtonRelease-1>', self.release)

    def set(self, levels, tops, labels, selected=None):
        """levels in order, one top per level but the last, {level: 'model · effort'}."""
        self.levels, self.tops, self.labels, self.selected = list(levels), list(tops), dict(labels), selected
        self.draw()

    # Every segment gets MIN_WIDTH px (a 5-point level stays readable); only the rest is proportional,
    # so a boundary sits at (i + 1) * min + density * (top + 1) and a drag inverts that exactly.
    MIN_WIDTH = 120

    def layout(self):
        width = max(self.winfo_width(), 2)
        least = min(self.MIN_WIDTH, width / max(1, 2 * len(self.levels)))
        return least, (width - least * len(self.levels)) / 101

    def edge(self, i):
        """x of the boundary after the i-th level (-1: the left end)."""
        least, density = self.layout()
        return 0 if i < 0 else (i + 1) * least + density * ((self.tops + [100])[i] + 1)

    def spans(self):
        lows = [0] + [t + 1 for t in self.tops]
        return list(zip(self.levels, lows, [*self.tops, 100]))

    def boundary(self, x):
        near = [(abs(x - self.edge(i)), i) for i in range(len(self.tops))]
        hit = min(near, default=None)
        return hit[1] if hit and hit[0] <= self.GRAB else None

    @staticmethod
    def dim(color):
        """Levels not being edited sit closer to the background."""
        return mix(color, BG, 0.45)

    def draw(self):
        self.delete('all')
        h, width = self.HEIGHT, max(self.winfo_width(), 2)
        gap = 3  # dark space between levels, split over both neighbours
        for i, (level, low, high) in enumerate(self.spans()):
            # every rectangle stays inside the canvas: an edge drawn on its border would be clipped away
            x0 = self.edge(i - 1) + (gap / 2 if i else 0)
            x1 = min(self.edge(i) - (gap / 2 if i < len(self.tops) else 0), width - 1)
            picked = level == self.selected
            color = LEVEL_COLORS.get(level, ACCENT)
            color = color if picked or self.selected is None else self.dim(color)
            # lit from above: a lighter top fading into the level's colour
            for y in range(0, h, 2):
                self.create_rectangle(x0, y, x1, min(y + 2, h), width=0,
                                      fill=mix(mix(color, '#ffffff', 0.28), color, y / max(h - 2, 1)))
            if picked:  # a 2 px ring wholly inside the segment
                self.create_rectangle(x0 + 1, 1, x1 - 1, h - 1, width=2, outline=FG)
            room = x1 - x0
            title = f'{level_name(level)} {low}–{high}' if room > 130 else f'{low}–{high}'
            self.create_text((x0 + x1) / 2, h * 0.31, text=title, fill=FG, font=(UI, 8, 'bold'), width=room - 6)
            self.create_text((x0 + x1) / 2, h * 0.71, text=self.labels.get(level, ''), fill=FG, font=(UI, 7),
                             width=room - 6)

    def hover(self, e):
        self.configure(cursor='sb_h_double_arrow' if self.boundary(e.x) is not None else 'hand2')

    def press(self, e):
        self.drag = self.boundary(e.x)
        if self.drag is None:
            level = next((lv for i, (lv, _, _) in enumerate(self.spans()) if e.x < self.edge(i)),
                         self.levels[-1] if self.levels else None)
            if level:
                self.on_pick(level)

    def move(self, e):
        if self.drag is None:
            return
        i = self.drag
        least, density = self.layout()
        top = round((e.x - (i + 1) * least) / max(density, 1e-6)) - 1
        lowest = self.tops[i - 1] + 1 if i else 0
        highest = self.tops[i + 1] - 1 if i + 1 < len(self.tops) else 99
        self.tops[i] = max(lowest, min(top, highest))
        self.draw()

    def release(self, _e):
        if self.drag is not None:
            self.drag = None
            self.on_change(list(self.tops))


class ThinScrollbar(tk.Canvas):
    """A slim dark scrollbar: Tk 9's native one ignores these colors."""

    def __init__(self, master, command):
        super().__init__(master, width=10, height=10, highlightthickness=0, bd=0, bg=BG)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.bind('<Configure>', lambda _e: self.draw())
        self.bind('<Button-1>', self.grab)
        self.bind('<B1-Motion>', self.grab)

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self.draw()

    def draw(self):
        self.delete('all')
        height = max(1, self.winfo_height())
        if self.last - self.first >= 1.0:
            return
        top = int(self.first * height)
        bottom = max(top + 24, int(self.last * height))
        self.create_rectangle(2, top, 8, min(height, bottom), fill='#33405a', outline='')

    def grab(self, event):
        height = max(1, self.winfo_height())
        span = self.last - self.first
        self.command('moveto', str(max(0.0, min(1.0 - span, event.y / height - span / 2))))


class RowHover:
    """A hover highlight for a Treeview's selectable rows (Tk has no built-in row hover).

    Treeview tag priority is creation order, so a zebra tag would win over a hover tag; the
    hovered row's tags are swapped instead and restored when the pointer leaves."""

    def __init__(self, tree, selectable):
        self.tree, self.selectable, self.iid, self.saved = tree, selectable, '', None
        tree.bind('<Motion>', self.move, add='+')
        tree.bind('<Leave>', lambda _e: self.clear(), add='+')

    def reset(self):
        self.restore()
        self.iid = ''
        self.tree.configure(cursor='')

    def clear(self):
        self.tree.configure(cursor='')
        self.show('')

    def show(self, iid):
        if iid == self.iid:
            return
        self.restore()
        self.iid = iid
        self.highlight(iid)

    def restore(self):
        if not self.saved:
            return
        iid, original = self.saved
        self.saved = None
        if not self.tree.exists(iid):
            return
        # Re-read the live tags: Current may have moved here while the pointer rested.
        tags = [tag for tag in self.tree.item(iid, 'tags') if tag != 'hover']
        if 'zebra' in original and 'zebra' not in tags:
            tags.append('zebra')
        self.tree.item(iid, tags=tuple(tags))

    def highlight(self, iid):
        if not iid or not self.tree.exists(iid):
            return
        tags = tuple(self.tree.item(iid, 'tags'))
        if iid in self.tree.selection() or 'current' in tags or 'family' in tags:
            return
        self.saved = (iid, tags)
        self.tree.item(iid, tags=tuple(tag for tag in tags if tag not in ('zebra', 'hover')) + ('hover',))

    def move(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and not self.selectable(iid):
            iid = ''
        self.tree.configure(cursor='hand2' if iid else '')
        self.show(iid)


class PickerTab(ttk.Frame):
    """One tab: a provider, a searchable model list and an effort, applied through ccx."""

    def __init__(self, app, key, parent=None):
        super().__init__(parent if parent is not None else app, style='Tab.TFrame')
        self.app = app
        self.key = key
        self.providers = []
        self.models = []
        self.iids = {}  # model id -> tree item
        self.current = None
        self.model_request = 0
        self.load_request = 0
        self.rows_target = None  # the target the rows in the list were fetched for
        self.facts = {}
        self.busy_job = None
        self.busy_text = ''
        self.busy_i = 0
        saved = session_view().get(key)
        self.view = migrate_view(saved)

        if key == 'subagent':
            # Off: the main model does all the work. One model: every task goes to `sub`.
            # Router: the main model sends each task to the easy, mid or hard level that fits it.
            entry = session_entry()
            self.levels = [n for n in ROUTER_LEVELS if n in load_profiles()]
            self.mode = tk.StringVar(value='off' if entry.get('off') else
                                     'router' if self.levels and entry.get('router') is not False else 'one')
            modes = ttk.Frame(self)
            modes.pack(fill='x', padx=18, pady=(16, 0))
            ttk.Label(modes, text='Subagents', style='Dim.TLabel').pack(side='left', padx=(0, 12))
            for value, text in (('off', 'Off'), ('one', 'One model'), ('router', 'Router')):
                ttk.Radiobutton(modes, text=text, value=value, variable=self.mode, style='Seg.Toolbutton',
                                command=self.set_mode,
                                state='normal' if session_launch_id() and (value != 'router' or self.levels)
                                else 'disabled').pack(side='left', padx=(0, 2))
            # what the chosen mode does, on the switch's own row; Best picks at its end while routing
            self.mode_hint = ttk.Label(modes, text='', style='Faint.TLabel')
            self.mode_hint.pack(side='left', padx=(14, 0))
            # fills every level from the picked provider: cheapest (model, effort) per level from AA and price
            self.best_button = ttk.Button(modes, text='Best picks', command=self.best_picks)

        # one row: Provider, Effort and Compact on the left, Find and Sort on the right (packed right to left)
        pick = self.pick_row = ttk.Frame(self)
        pick.pack(fill='x', padx=18, pady=(14, 10))
        self.query = tk.StringVar(value=str(self.view.get('query') or ''))
        self.query.trace_add('write', lambda *_: self.render_models())
        self.sort_mode = tk.StringVar(value=self.view.get('sort') if self.view.get('sort') in SORTS else 'Intelligence')
        self.sort_box = ttk.Combobox(pick, textvariable=self.sort_mode, state='readonly', width=11,
                                     values=list(SORTS))
        self.sort_box.pack(side='right', padx=(8, 0))
        self.sort_box.bind('<<ComboboxSelected>>', lambda _e: self.render_models())
        ttk.Label(pick, text='Sort', style='Dim.TLabel').pack(side='right')
        ttk.Entry(pick, textvariable=self.query, width=16).pack(side='right', padx=(8, 16), ipady=2)
        ttk.Label(pick, text='Find', style='Dim.TLabel').pack(side='right')
        self.target_label = ttk.Label(pick, text='Target', style='Dim.TLabel')
        self.target = tk.StringVar()
        self.target_box = ttk.Combobox(pick, textvariable=self.target, state='readonly', width=15)
        if key == 'subagent':
            # The router's levels on the 0-100 difficulty scale, one row above Provider: each segment is a
            # level (click it to edit its model) and each boundary where one ends (drag it).
            self.level_bar = ttk.Frame(self)
            self.range_canvas = RangeBar(self.level_bar, self.save_ranges, self.pick_level)
            self.range_canvas.pack(fill='x', expand=True)
            self.level_bar.pack(fill='x', padx=18, pady=(12, 0), before=pick)
            # the bar marks whichever level the list below edits
            self.target.trace_add('write', lambda *_: self.mark_level())
        elif key == 'fallback':
            self.target_label.pack(side='left')
            self.target_box.pack(side='left', padx=(8, 18))
            self.target_box.bind('<<ComboboxSelected>>', lambda _e: self.load())
            ttk.Label(self, text='Target: this session or one subagent profile (a session fallback needs a ccx adapter launch).',
                      style='Faint.TLabel', wraplength=900).pack(fill='x', padx=18, pady=(0, 8))
        self.provider_label = ttk.Label(pick, text='Provider', style='Dim.TLabel')
        self.provider_label.pack(side='left')
        self.provider = tk.StringVar()
        if key == 'subagent':
            # Best picks names the provider it fills the levels from: the one picked here
            self.provider.trace_add('write', lambda *_: self.best_button.configure(
                text=f'Best picks · {self.provider.get()}' if self.provider.get() else 'Best picks'))
        self.provider_box = ttk.Combobox(pick, textvariable=self.provider, state='readonly', width=24)
        self.provider_box.pack(side='left', padx=(8, 8))
        self.provider_box.bind('<<ComboboxSelected>>', lambda _e: self.load_models())
        ttk.Label(pick, text='Effort', style='Dim.TLabel').pack(side='left')
        self.effort = tk.StringVar(value=self.view.get('effort') or 'default')
        self.effort_box = ttk.Combobox(pick, textvariable=self.effort, state='readonly', width=10)
        self.effort_box.pack(side='left', padx=(8, 0))
        self.effort.trace_add('write', lambda *_: (self.remember(), self.show_effort_aa()))
        self.compact = tk.StringVar(value='inherit')
        if key in ('subagent', 'model'):
            # Typed values work too: a token window such as 150k, or (subagents) a 1-100 percentage.
            # The Model tab stores the main session's window per model in Claude's settings.
            ttk.Label(pick, text='Compact', style='Dim.TLabel').pack(side='left', padx=(18, 0))
            ttk.Combobox(pick, textvariable=self.compact, width=9,
                         values=['inherit', *COMPACT_TOKENS, '50%', '70%', '90%'] if key == 'subagent'
                         else ['global', *COMPACT_TOKENS]).pack(side='left', padx=(8, 0))

        card = tk.Frame(self, bg=BORDER)
        card.pack(fill='both', expand=True, padx=18, pady=(0, 10))
        inner = ttk.Frame(card, style='Card.TFrame')
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        self.list = ttk.Treeview(inner, columns=('context', 'price', 'intelligence', 'score', 'session'),
                                 show='tree headings', style='Picker.Treeview', selectmode='browse',
                                 height=6)
        self.list.heading('#0', text='Model', anchor='w')
        self.list.column('#0', width=360, stretch=True, anchor='w')
        for column, title, width in (('context', 'Context', 80), ('price', 'Price $/M', 132),
                                     ('intelligence', 'AA', 76), ('score', 'Score', 60),
                                     ('session', 'This session', 110)):
            self.list.heading(column, text=title, anchor='e')
            self.list.column(column, width=width, stretch=False, anchor='e')
        self.list.tag_configure('family', foreground=DIM, background=PANEL,
                                font=SMALL_BOLD)
        self.list.tag_configure('current', background=CURRENT, foreground='#a8ebcf')
        self.list.tag_configure('zebra', background=ZEBRA)
        self.list.tag_configure('hover', background=HOVER, foreground=FG)
        self.hover = RowHover(self.list, lambda iid: iid in self.iids.values())
        scroll = ThinScrollbar(inner, self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        self.list.bind('<MouseWheel>', lambda e: self.list.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
        self.list.pack(side='left', fill='both', expand=True, padx=(10, 0), pady=10)
        scroll.pack(side='right', fill='y', padx=(6, 6), pady=10)
        self.list.bind('<<TreeviewSelect>>', lambda _e: self.on_select())
        self.list.bind('<Double-Button-1>', lambda _e: self.apply())
        self.list.bind('<Return>', lambda _e: self.apply())
        # An overlay in the empty list: the spinner while a list loads, else why it is empty.
        self.empty = ttk.Label(inner, text='', style='Empty.TLabel')
        self.empty.place(relx=0.5, rely=0.5, anchor='center')

        # The footer and the Current line sit at the bottom ahead of the list: a small window shrinks
        # the list, never the buttons. Buttons pack before the status, which gets only the room left.
        self.now = StatusLabel(self, text='', style='Faint.TLabel')
        self.now.pack(side='bottom', fill='x', padx=18, pady=(0, 12), before=card)
        foot = ttk.Frame(self)
        foot.pack(side='bottom', fill='x', padx=18, pady=(0, 14), before=card)
        ttk.Button(foot, text='Close', command=self.app.destroy).pack(side='right', padx=(8, 0))
        ttk.Button(foot, text='Apply', style='Accent.TButton', command=self.apply).pack(side='right')
        self.status = StatusLabel(foot, text='', style='Dim.TLabel')
        self.status.pack(side='left', fill='x', expand=True, padx=(0, 12))
        if key == 'subagent':
            self.show_mode()

    def set_mode(self):
        mode = self.mode.get()
        steps = [('agents', 'off')] if mode == 'off' else [('agents', 'on'), ('agents', 'router', 'on' if mode == 'router' else 'off')]
        for step in steps:
            ok, out = run_ccx(*step)
            if not ok:
                break
        self.status.configure(text=out.strip().splitlines()[-1] if out.strip() else '')
        self.show_mode()
        self.load()

    def show_levels(self):
        """Each level button names its difficulty range and the model it runs on now."""
        profiles = load_profiles()
        ranges = level_ranges(profiles)
        labels = {}
        for name in self.levels:
            profile = profiles.get(name) or {}
            compact = profile.get('autocompact')
            labels[name] = ' · '.join(filter(None, [short_model(profile['model']) if profile.get('model') else '',
                                                    profile.get('effort') or '',
                                                    f'ac {compact}%' if type(compact) is int else f'ac {compact}' if compact else '']))
        shown = [n for n in self.levels if n in ranges]
        self.range_canvas.set(shown, [ranges[n][1] for n in shown[:-1]], labels, self.target.get() or None)

    def mark_level(self):
        if self.mode.get() == 'router' and self.target.get() in self.levels:
            self.range_canvas.selected = self.target.get()
            self.range_canvas.draw()

    def pick_level(self, level):
        """A level button or a click on its segment: its models load below, and the bar marks it."""
        self.target.set(level)
        self.load()
        self.show_levels()

    def save_ranges(self, tops):
        current = [level_ranges(load_profiles())[n][1] for n in self.levels[:-1]]
        if tops == current:
            return  # a click with nothing moved
        ok, out = run_ccx('agents', 'router', 'ranges', *map(str, tops))
        self.settle(out.strip().splitlines()[-1] if out.strip() else ('Saved.' if ok else 'Failed.'))
        self.show_levels()  # a rejected pair snaps back to the saved ranges

    def best_picks(self):
        provider = self.provider.get() or 'official'
        self.busy(f'Finding the best {provider} picks…')

        def done(result):
            ok, out = result
            self.settle(out.strip().splitlines()[-1] if out.strip() else ('Done.' if ok else 'Failed.'))
            self.show_levels()
            self.load()
        self.app.background(lambda: run_ccx('agents', 'router', 'best', provider), done)

    def show_mode(self):
        mode = self.mode.get()
        self.show_levels()
        self.mode_hint.configure(text={
            'off': 'The main model does all the work itself.',
            'one': 'Every task runs on the one model picked below.',
            'router': 'Tasks run on the level their 0-100 score falls in.',
        }[mode])
        if mode == 'router':
            self.level_bar.pack(fill='x', padx=18, pady=(12, 0), before=self.pick_row)
            self.best_button.pack(side='right', before=self.mode_hint)  # claims its room before the hint
        else:
            self.level_bar.pack_forget()
            self.best_button.pack_forget()

    # ---- data ----

    def busy(self, text):
        """An animated status line, and the same spinner in an empty list."""
        self.busy_text = text
        if self.busy_job is None:
            self.busy_i = 0
            self.busy_tick()

    def busy_tick(self):
        frame = BUSY[self.busy_i % len(BUSY)]
        self.busy_i += 1
        self.status.configure(text=f'{frame}  {self.busy_text}')
        if not self.iids:
            self.empty.configure(text=f'{frame}  {self.busy_text}')
            self.empty.place(relx=0.5, rely=0.5, anchor='center')
        self.busy_job = self.after(90, self.busy_tick)

    def settle(self, text):
        if self.busy_job is not None:
            self.after_cancel(self.busy_job)
            self.busy_job = None
        self.status.configure(text=text)
        self.show_empty()

    def show_empty(self):
        if self.busy_job is not None:
            return  # the spinner owns the overlay
        if self.iids:
            self.empty.place_forget()
        else:
            self.empty.configure(text='No models match' if self.query.get().strip() else 'No models listed')
            self.empty.place(relx=0.5, rely=0.5, anchor='center')

    def provider_rows(self):
        if self.key == 'subagent':
            return tab_rows(run_ccx('agents', 'providers')[1])
        if self.key == 'fallback':
            # a subagent fallback picks from the routes the adapter can reach
            command = 'fallback' if self.target.get() in ('', 'session') else 'route'
            return tab_rows(run_ccx(command, 'routes')[1])
        return tab_rows(run_ccx('route', 'routes')[1])

    def model_command(self, provider):
        if self.key == 'subagent':
            return ['agents', 'models', self.target.get().lower(), provider]
        if self.key == 'fallback':
            return ['fallback', 'models', provider]
        return ['route', 'models', provider]

    def load(self):
        self.busy('Loading providers…')
        targets = [('', '')]
        if self.key == 'subagent':
            # shortcut: profiles other than sub and the levels are only editable with `ccx agents set`
            targets = [(n, n) for n in self.levels] if self.mode.get() == 'router' else [('sub', 'sub')]
            if self.mode.get() == 'router' and self.target.get() not in self.levels:
                self.target.set('')  # the saved view picks the level, else the first
        elif self.key == 'fallback':
            # the subagents this session uses: the router's levels, or the one model
            names = list(ROUTER_LEVELS) if session_entry().get('router') is not False else ['sub']
            targets = [('session', 'session')] + [(name, name) for name in names if name in load_profiles()]
        names = [value for _, value in targets]
        self.target_box.configure(values=names)
        current, saved = self.target.get(), self.view.get('target') or ''
        # A change made in the combobox wins over the cached one; the cache is only the first open.
        self.target.set(current if current in names else saved if saved in names else (names[0] if names else ''))
        if self.rows_target is not None and self.rows_target != self.target.get():
            # The old target's rows must not be redrawn (or picked) while the new list loads.
            self.models, self.facts = [], {}
            self.list.delete(*self.list.get_children())
            self.iids = {}
            self.current = None
            self.rows_target = None
            self.show_empty()
        self.load_request += 1
        request, target = self.load_request, self.target.get()

        def done(rows):
            if request == self.load_request and target == self.target.get():
                self.show_providers(rows if isinstance(rows, list) else [])

        self.app.background(self.provider_rows, done)

    def show_effort_aa(self):
        """The AA column follows the Effort box: each model's score at that effort, where AA rates it."""
        if not hasattr(self, 'list'):
            return  # the effort is set while the tab is still being built
        effort = self.effort.get()
        self.list.heading('intelligence', text='AA' if effort in ('', 'default') else f'AA {effort}')
        for model, iid in self.iids.items():
            if self.list.exists(iid):
                self.list.set(iid, 'intelligence', fact_values(self.facts.get(model), None, effort)[2])

    def remember(self):
        """Keep this tab's browsing picks (never the applied route) for the next opening."""
        previous = self.view if isinstance(self.view, dict) else {}
        same = previous.get('target', '') == self.target.get()
        picks = dict(previous.get('picks') or {}) if same and isinstance(previous.get('picks'), dict) else {}
        chosen = self.chosen()
        if chosen:
            picks[self.provider.get()] = chosen
        entry = {
            'v': VIEW_VERSION,
            'target': self.target.get(),
            'provider': self.provider.get(),
            'picks': picks,
            'model': picks.get(self.provider.get()),
            'sort': self.sort_mode.get(),
            'query': self.query.get(),
            'effort': self.effort.get(),
        }
        self.view = entry
        save_view(self.key, entry)

    def same_target(self):
        return self.view.get('target', '') == self.target.get()

    def picks(self):
        picks = self.view.get('picks') if self.same_target() else None
        return picks if isinstance(picks, dict) else {}

    def show_providers(self, rows):
        self.providers = rows
        self.provider_box.configure(values=[r[0] for r in rows])
        self.render_now()
        # opens on the provider in use, else official (the user's default), never on the last one browsed
        wanted = (self.current[0] if self.current else '') or 'official'
        choice = next((r[0] for r in rows if r[0] == wanted), rows[0][0] if rows else '')
        if not self.same_target() or choice != self.provider.get():
            # neither the old target's row nor the old provider's model is a pick for this one
            self.list.selection_remove(self.list.selection())
        self.provider.set(choice)
        effort = self.view.get('effort') if self.same_target() and self.view.get('provider') == choice else None
        # 'default' is not a pick: it falls back to the effort the route in use was launched with.
        self.effort.set(effort if effort not in (None, '', 'default')
                        else (self.current[2] if self.current and len(self.current) > 2 else 'default'))
        self.remember()
        self.load_models()

    def load_models(self):
        provider = self.provider.get()
        self.model_request += 1
        request = self.model_request
        target = self.target.get()
        self.models, self.facts = [], self.app.facts_cache.get(provider, {})
        self.rows_target = None
        self.render_models()
        if not provider:
            self.settle('No provider here.')
            return
        cached = self.app.models_cache.get((self.key, target, provider))
        if cached is not None:
            self.show_models(cached)
            return
        self.busy(f'Loading {provider} models…')
        args = self.model_command(provider)

        def done(result):
            if request == self.model_request and (provider, target) == (self.provider.get(), self.target.get()):
                if result[0]:
                    self.app.models_cache[(self.key, target, provider)] = result
                self.show_models(result)

        self.app.background(lambda: run_ccx(*args), done)
        self.load_facts(provider)

    def load_facts(self, provider):
        """The launch menu's facts (context, price, intelligence, score) for these models."""
        if provider in self.app.facts_cache:
            self.facts = self.app.facts_cache[provider]
            return
        self.app.background(lambda: run_ccx('facts', provider),
                            lambda result: self.show_facts(provider, result))

    def show_facts(self, provider, result):
        ok, out = result
        facts = {}
        if ok:
            for line in out.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get('id'), str):
                    facts[row['id']] = row
        self.app.facts_cache[provider] = facts
        if provider == self.provider.get():
            self.facts = facts
            self.render_models()

    def show_models(self, result):
        ok, out = result
        if not ok:
            self.models = []
            self.render_models()
            self.settle(out.splitlines()[-1] if out else 'Could not list models.')
            return
        self.models = indented_rows(out) if self.key == 'subagent' else plain_rows(out)
        self.rows_target = self.target.get()
        cached = self.picks().get(self.provider.get())
        wanted = cached or (self.current[1] if self.current and self.current[0] == self.provider.get() else None)
        self.render_models(wanted)
        self.settle(f'{len(self.models)} models' if self.models else 'No models listed.')
        self.load_efforts()

    def render_models(self, select=None):
        wanted = select or self.chosen() or self.picks().get(self.provider.get())
        if not wanted and self.current and self.current[0] == self.provider.get():
            wanted = self.current[1]
        mode = SORTS.get(self.sort_mode.get(), 'intelligence')
        models = sort_models(filtered(self.models, self.query.get()), self.facts, mode)
        self.hover.reset()
        self.list.delete(*self.list.get_children())
        self.iids = {}
        zebra = 0
        if mode == 'family':
            groups = {}
            for model in models:
                groups.setdefault(model_family(model), []).append(model)
            for family in sorted(groups):
                logo = self.app.mark(FAMILY_LOGOS.get(family), family)
                parent = self.list.insert('', 'end', text=f' {family.upper()}', image=logo,
                                          open=True, tags=('family',))
                for model in groups[family]:
                    self.insert_model(model, parent, zebra % 2 == 1)
                    zebra += 1
        else:
            for model in models:
                self.insert_model(model, '', zebra % 2 == 1)
                zebra += 1
        iid = self.iids.get(wanted) if wanted else None
        if iid is None and self.iids:
            iid = next(iter(self.iids.values()))
        if iid:
            self.list.selection_set(iid)
            self.list.focus(iid)
            self.after_idle(lambda i=iid: self.reveal(i))
        self.show_empty()
        self.remember()

    def row_index(self, iid):
        """The row's position in drawing order; Treeview.index() only counts within one parent."""
        rows = []

        def walk(parent=''):
            for child in self.list.get_children(parent):
                rows.append(child)
                walk(child)

        walk()
        return rows.index(iid)

    def reveal(self, iid):
        """Keep the top of the ranking on screen when the pick is inside the first screenful."""
        try:
            if not self.list.winfo_exists():
                return
            if not self.list.exists(iid):
                return
            # Before the window maps, height is 1: assume a screenful rather than scrolling one row.
            height = self.list.winfo_height()
            rows = max(1, height // 36) if height >= 36 else 8
            if self.row_index(iid) < rows:
                self.list.yview_moveto(0)
            else:
                self.list.see(iid)
        except tk.TclError:  # the window closed before the idle callback ran
            pass

    def insert_model(self, model, parent='', zebra=False):
        logo = self.app.mark(FAMILY_LOGOS.get(model_family(model)), model_family(model))
        current = bool(self.current and self.current[:2] == [self.provider.get(), model])
        tags = ('zebra',) if zebra else ()
        if current:
            tags += ('current',)
        iid = self.list.insert(parent, 'end', text=f' {model}' + ('  ·  Current' if current else ''), image=logo,
                               values=fact_values(self.facts.get(model), self.app.usage, self.effort.get())
                               if self.facts or self.provider.get() in self.app.facts_cache else ('…', '…', '…', '…', ''),  # facts on the way
                               tags=tags)
        self.iids[model] = iid

    def chosen(self):
        picked = self.list.selection()
        if not picked:
            return None
        return next((model for model, iid in self.iids.items() if iid == picked[0]), None)

    def on_select(self):
        self.hover.clear()
        if self.chosen() is None:
            picked = self.list.selection()
            children = self.list.get_children(picked[0]) if picked else ()
            if children:
                self.list.selection_set(children[0])
                self.list.focus(children[0])
                return
        self.remember()
        self.load_efforts()

    def load_efforts(self):
        model, provider = self.chosen(), self.provider.get()
        if not model or not provider:
            return
        self.app.background(lambda: efforts_for(provider, model),
                            lambda levels: self.show_efforts(levels)
                            if (provider, model) == (self.provider.get(), self.chosen()) else None)

    def show_efforts(self, levels):
        values = effort_values(levels, self.effort.get())
        self.effort_box.configure(values=values)
        if self.effort.get() not in values:
            self.effort.set('default')

    def render_now(self):
        if self.key == 'subagent' or (self.key == 'fallback' and self.target.get() not in ('', 'session')):
            profile = load_profiles().get(self.target.get(), {})
            target = profile if self.key == 'subagent' else profile.get('fallback') or {}
            self.current = ([target['provider'], target['model'], target.get('effort') or 'default']
                            if target.get('provider') and target.get('model') else None)
            if self.key == 'subagent':
                own = profile.get('autocompact')
                self.compact.set(f'{own}%' if type(own) is int else own or 'inherit')
            label = '/'.join(self.current[:2]) + ' · ' + self.current[2] if self.current else 'not configured'
            self.now.configure(text=f'Current · {self.target.get()} · {label}')
            return
        command = {'model': ('route',), 'fallback': ('fallback',), 'auto': ('helper', 'auto'),
                   'tools': ('helper', 'tools')}.get(self.key)
        if not command:
            self.current = None
            self.now.configure(text='')
            return
        out = run_ccx(*command)[1]
        name = {'model': 'Route', 'fallback': 'Fallback', 'auto': 'Helper', 'tools': 'Helper'}[self.key]
        self.current = parse_target(out, name)
        if self.key == 'model' and self.current:
            self.compact.set(model_compact(self.current[1]))
        lines = [line for line in out.splitlines() if line.strip()]
        self.now.configure(text='   ·   '.join(lines))

    # ---- apply ----

    def apply(self):
        model = self.chosen() or (self.current[1] if self.key == 'subagent' and self.current
                                  and self.current[0] == self.provider.get() else None)
        if not model:
            self.status.configure(text='Pick a model first.')
            return
        provider, effort, target = self.provider.get(), self.effort.get(), self.target.get().lower()
        compact = self.compact.get().strip() or 'inherit'
        self.busy(f'Setting {provider}/{model}…')
        self.app.background(lambda: self.apply_command(provider, model, effort, target, compact), self.show_result)

    def apply_command(self, provider, model, effort, target, compact='inherit'):
        level = [] if effort in ('', 'default') else [effort]
        if self.key == 'model':
            ok, said = set_model_compact(model, compact if compact != 'inherit' else 'global')
            if not ok:
                return ok, said
            ok, out = run_ccx('route', provider, model, *level)
            return ok, f'{out.rstrip()}\n{said}' if ok else out
        if self.key in ('auto', 'tools'):
            return run_ccx('helper', self.key, provider, model, *level)
        if self.key == 'subagent':
            return run_ccx('agents', 'set', target, '--provider', provider,
                          '--model', model, '--effort', effort or 'default', '--autocompact', compact)
        if self.key == 'fallback' and target == 'session':
            return run_ccx('fallback', provider, model, *level)
        if self.key == 'fallback':
            return run_ccx('fallback', 'subagent', target, provider, model, *level)
        return False, 'Nothing to apply.'

    def mark_current(self):
        """Move the Current mark without rebuilding the list, so scroll and selection stay put."""
        for model, iid in self.iids.items():
            current = bool(self.current and self.current[:2] == [self.provider.get(), model])
            tags = tuple(tag for tag in self.list.item(iid, 'tags') if tag != 'current')
            if current:
                tags += ('current',)
            self.list.item(iid, text=f' {model}' + ('  ·  Current' if current else ''), tags=tags)

    def show_result(self, result):
        ok, out = result
        lines = out.splitlines() if out else []
        # a subagent edit prints every profile, then a long note: say only what this edit set
        mine = next((l.strip() for l in lines if l.strip().startswith(f'ccx-{self.target.get().lower()}:')), None)
        self.settle(('✔ ' + mine[4:]) if ok and self.key == 'subagent' and mine
                    else lines[-1] if lines else ('Applied.' if ok else 'Failed.'))
        if not ok:
            return
        if self.key == 'subagent':
            self.render_now()
            self.show_levels()
            self.mark_current()
        else:
            name = {'model': 'Route', 'fallback': 'Fallback', 'auto': 'Helper', 'tools': 'Helper'}[self.key]
            self.current = parse_target(out, name) or self.current
            lines = [line for line in out.splitlines() if line.strip()]
            self.now.configure(text='   ·   '.join(lines))
            self.mark_current()
        self.remember()


class PresetsTab(ttk.Frame):
    """Each provider's default pick, one Enter away."""

    def __init__(self, app, parent=None):
        super().__init__(parent if parent is not None else app, style='Tab.TFrame')
        self.app = app
        self.presets = []
        self.shown = []
        self.iids = []  # (tree item, preset)
        self.current = None
        self.wanted = None  # the preset to select after the next reload, e.g. one just created
        self.busy_job = None
        self.busy_text = ''
        self.busy_i = 0

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=18, pady=(16, 10))
        ttk.Label(bar, text='Find', style='Dim.TLabel').pack(side='left')
        self.query = tk.StringVar()
        self.query.trace_add('write', lambda *_: self.render())
        ttk.Entry(bar, textvariable=self.query, width=34).pack(side='left', padx=(8, 0), ipady=4)

        card = tk.Frame(self, bg=BORDER)
        card.pack(fill='both', expand=True, padx=18, pady=(0, 10))
        inner = ttk.Frame(card, style='Card.TFrame')
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        self.list = ttk.Treeview(inner, columns=('model', 'effort', 'subagent', 'source', 'state'),
                                 show='tree headings', style='Picker.Treeview', selectmode='browse',
                                 height=6)
        self.list.heading('#0', text='Preset', anchor='w')
        self.list.column('#0', width=170, minwidth=100, stretch=True, anchor='w')
        for column, title, width in [('model', 'Model', 260), ('effort', 'Effort', 76), ('subagent', 'Subagents', 190),
                                     ('source', 'Saved', 80), ('state', 'Session', 76)]:
            self.list.heading(column, text=title, anchor='w')
            self.list.column(column, width=width, minwidth=65, stretch=column == 'model', anchor='w')
        self.list.tag_configure('current', background=CURRENT, foreground='#a8ebcf')
        self.list.tag_configure('zebra', background=ZEBRA)
        self.list.tag_configure('hover', background=HOVER, foreground=FG)
        self.hover = RowHover(self.list, lambda iid: any(iid == item for item, _ in self.iids))
        scroll = ThinScrollbar(inner, self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        self.list.bind('<MouseWheel>', lambda e: self.list.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
        self.list.pack(side='left', fill='both', expand=True, padx=(10, 0), pady=10)
        scroll.pack(side='right', fill='y', padx=(6, 6), pady=10)
        self.list.bind('<<TreeviewSelect>>', lambda _e: self.hover.clear())
        self.list.bind('<Double-Button-1>', lambda _e: self.edit())
        self.list.bind('<Return>', lambda _e: self.apply())
        self.empty = ttk.Label(inner, text='', style='Empty.TLabel')
        self.empty.place(relx=0.5, rely=0.5, anchor='center')

        foot = ttk.Frame(self)
        foot.pack(side='bottom', fill='x', padx=18, pady=(0, 14), before=card)
        ttk.Button(foot, text='Close', command=self.app.destroy).pack(side='right', padx=(8, 0))
        ttk.Button(foot, text='Use in this session', style='Accent.TButton', command=self.apply).pack(side='right')
        ttk.Button(foot, text='Edit…', command=self.edit).pack(side='right', padx=(0, 8))
        ttk.Button(foot, text='New…', command=self.create).pack(side='right', padx=(0, 8))
        self.status = StatusLabel(foot, text='', style='Dim.TLabel')
        self.status.pack(side='left', fill='x', expand=True, padx=(0, 12))
        self.render()

    def busy(self, text):
        self.busy_text = text
        if self.busy_job is None:
            self.busy_i = 0
            self.busy_tick()

    def busy_tick(self):
        frame = BUSY[self.busy_i % len(BUSY)]
        self.busy_i += 1
        self.status.configure(text=f'{frame}  {self.busy_text}')
        if not self.iids:
            self.empty.configure(text=f'{frame}  {self.busy_text}')
            self.empty.place(relx=0.5, rely=0.5, anchor='center')
        self.busy_job = self.after(90, self.busy_tick)

    def settle(self, text):
        if self.busy_job is not None:
            self.after_cancel(self.busy_job)
            self.busy_job = None
        self.status.configure(text=text)
        self.show_empty()

    def show_empty(self):
        if self.busy_job is not None:
            return
        if self.iids:
            self.empty.place_forget()
        else:
            self.empty.configure(text='No presets match' if self.query.get().strip() else 'No presets listed')
            self.empty.place(relx=0.5, rely=0.5, anchor='center')

    def load(self):
        self.busy('Loading presets…')
        self.app.background(lambda: run_ccx('presets', 'bundles'), self.show_result_load)

    def show_result_load(self, result):
        ok, out = result
        if not ok:
            self.settle(out.splitlines()[-1] if out else 'Could not list presets.')
            return
        self.show(out)

    def show(self, out):
        self.current = parse_target(run_ccx('route')[1], 'Route')
        self.presets = []
        if out.lstrip().startswith('['):
            try:
                self.presets = json.loads(out)
                for preset in self.presets:
                    preset.setdefault('name', preset['provider'])
                    preset['label'] = f'{preset["name"]} · {preset["model"]}'
            except (ValueError, KeyError, TypeError):
                self.presets = []
        else:
            for line in out.splitlines():
                parts = line.split('\t')
                if len(parts) >= 4:
                    provider, model, effort, label = parts[:4]
                    self.presets.append({'name': provider, 'provider': provider, 'model': model, 'effort': effort or None,
                                         'label': label, 'custom': len(parts) >= 5 and parts[4].strip() == '1'})
        self.render()
        self.settle(f'{len(self.presets)} presets' if self.presets else 'No presets listed.')

    def render(self):
        picked = self.picked()
        wanted, self.wanted = self.wanted or (picked['name'] if picked else None), None
        needle = self.query.get().strip().lower()
        self.hover.reset()
        self.list.delete(*self.list.get_children())
        self.shown = [p for p in self.presets if not needle or needle in
                      ' '.join(str(p.get(k) or '') for k in ('name', 'provider', 'model', 'effort')).lower()]
        self.iids = []
        active = None
        for index, preset in enumerate(self.shown):
            logo = self.app.mark(PROVIDER_LOGOS.get(preset['provider']), preset['provider'])
            current = bool(self.current and self.current[:2] == [preset['provider'], preset['model']]
                           and (self.current[2] if len(self.current) > 2 else 'default') == (preset['effort'] or 'default'))
            tags = ('zebra',) if index % 2 else ()
            if current:
                tags += ('current',)
            iid = self.list.insert('', 'end', text=' ' + preset['name'], image=logo,
                                   values=(preset['model'], preset['effort'] or 'default', subagent_text(preset),
                                           'Custom' if preset.get('custom') else 'Default',
                                           'Current' if current else ''),
                                   tags=tags)
            self.iids.append((iid, preset))
            if current:
                active = iid
        if self.iids:
            iid = next((iid for iid, p in self.iids if p['name'] == wanted), active or self.iids[0][0])
            self.list.selection_set(iid)
            self.list.focus(iid)
            self.list.see(iid)
        self.show_empty()

    def picked(self):
        selection = self.list.selection()
        if not selection:
            return None
        return next((preset for iid, preset in self.iids if iid == selection[0]), None)

    def edit(self):
        preset = self.picked()
        if preset is None:
            self.status.configure(text='Pick a preset first.')
            return
        PresetEditor(self.app, preset, self.load)

    def create(self):
        """A new named preset, started from the picked one."""
        base = self.picked() or (self.presets[0] if self.presets else None)
        if base is None:
            self.status.configure(text='No preset to start from.')
            return
        name = (simpledialog.askstring('New preset', 'Name (letters, digits, - or _):', parent=self) or '').strip().lower()
        if not name:
            return
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', name):
            self.status.configure(text='A preset name is 1-32 letters, digits, - or _.')
            return
        if any(p['name'] == name for p in self.presets):
            self.status.configure(text=f'A preset named {name} already exists.')
            return
        self.wanted = name
        PresetEditor(self.app, {**base, 'name': name, 'custom': True, 'new': True}, self.load)

    def apply(self):
        preset = self.picked()
        if preset is None:
            self.status.configure(text='Pick a preset first.')
            return
        settings = preset.get('settings')
        if not settings:
            args = ['route', preset['provider'], preset['model']]
            if preset['effort']:
                args.append(preset['effort'])
            work = lambda: run_ccx(*args)
        else:
            def work():
                for key, label in TABS[:-1]:
                    target = settings[key]
                    provider, model = target['provider'], target.get('model', '')
                    effort = target.get('effort') or 'default'
                    pair = ['off'] if provider == 'off' else [provider, model, *([] if effort == 'default' else [effort])]
                    if key == 'subagent':  # router or one model, as `ccx presets use-subagent` sets it
                        commands = [['presets', 'use-subagent', preset.get('name') or preset['provider']]]
                    else:
                        command = (['route', *pair] if key == 'model' else ['fallback', *pair] if key == 'fallback'
                                   else ['helper', key, *(['inherit'] if provider == 'off' else pair)])
                        commands = [command]
                    for command in commands:
                        ok, out = run_ccx(*command)
                        if not ok:
                            return False, f'{label}: {out}\nEarlier categories may already be applied. {label}: {out.splitlines()[-1] if out else "Failed."}'
                return True, 'Applied all five preset categories.'
        self.busy(f'Setting {preset["label"]}…')
        self.app.background(work, self.show_result)

    def mark_current(self):
        """Move the Current mark in place: no reload, no lost selection."""
        for iid, preset in self.iids:
            current = bool(self.current and self.current[:2] == [preset['provider'], preset['model']]
                           and (self.current[2] if len(self.current) > 2 else 'default') == (preset['effort'] or 'default'))
            values = list(self.list.item(iid, 'values'))
            values[3] = 'Current' if current else ''
            tags = tuple(tag for tag in self.list.item(iid, 'tags') if tag != 'current')
            if current:
                tags += ('current',)
            self.list.item(iid, values=values, tags=tags)

    def show_result(self, result):
        ok, out = result
        self.settle(out.splitlines()[-1] if out else ('Applied.' if ok else 'Failed.'))
        if ok:
            self.current = parse_target(run_ccx('route')[1], 'Route')
            self.mark_current()


class PresetEditor(tk.Toplevel):
    """All five preset categories, saved together without changing active routes."""

    def __init__(self, app, preset, saved):
        super().__init__(app)
        self.app = app
        preset = self.preset = {'name': preset['provider'], **preset}
        self.saved = saved
        self.title(f'Preset · {preset["name"]}')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(10, lambda: dark_title_bar(self))
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=20, pady=18)
        head = ttk.Frame(body)
        head.grid(row=0, column=0, columnspan=6, sticky='we', pady=(0, 14))
        ttk.Label(head, text=preset['name'], style='Title.TLabel').pack(side='top', anchor='w')
        GradientRule(head, height=2).pack(side='top', fill='x', pady=(8, 0))
        self.fields = {}
        main = {'provider': preset['provider'], 'model': preset['model'], 'effort': preset.get('effort') or 'default'}
        settings = preset.get('settings') or {key: dict(main) for key, _ in TABS[:-1]}
        if not preset.get('settings'):
            settings['fallback'] = {'provider': 'off'}
        providers = [value for value, _ in tab_rows(run_ccx('agents', 'providers')[1]) if value != 'off']
        providers = list(dict.fromkeys([*providers, *[t['provider'] for t in settings.values() if t['provider'] not in ('off', 'router')]]))
        fallback_providers = [value for value, _ in tab_rows(run_ccx('fallback', 'routes')[1])]
        helper_providers = [value for value, _ in tab_rows(run_ccx('route', 'routes')[1])]
        for column, title in enumerate(('', 'Provider', 'Model', 'Effort', 'Up to', 'Compact')):
            ttk.Label(body, text=title, style='Dim.TLabel').grid(row=1, column=column, sticky='w', padx=(0, 10))
        # a router preset's levels: as saved, else as the levels are set now
        router = settings['subagent'] if settings['subagent'].get('provider') == 'router' else None
        profiles = load_profiles()
        ranges = level_ranges(profiles)
        levels = (router or {}).get('levels') or {
            n: {**{k: (profiles.get(n) or {}).get(k) for k in ('provider', 'model', 'effort')},
                'upto': ranges.get(n, (0, LEVEL_TOPS.get(n, 100)))[1]} for n in ROUTER_LEVELS if n in profiles}
        # the preset's own levels (an older preset may have three), in order
        self.router_levels = [n for n in ROUTER_LEVELS if n in levels]
        self.level_rows = []
        rows = []
        for key, label in TABS[:-1]:
            rows.append((key, label, settings[key]))
            if key == 'subagent':
                rows += [(n, f'   {level_name(n)}', levels.get(n) or {}) for n in self.router_levels]
        for row, (key, label, target) in enumerate(rows, 2):
            fields = {name: tk.StringVar(value=target.get(name) or ('default' if name == 'effort' else ''))
                      for name in ('provider', 'model', 'effort')}
            self.fields[key] = fields
            if key in ROUTER_LEVELS:  # a dot in the level's Difficulty-bar colour
                name_label = tk.Label(body, text=f'  ● {level_name(key)}', bg=BG, fg=LEVEL_COLORS.get(key, DIM), font=FONT)
            else:
                name_label = ttk.Label(body, text=label, style='TLabel')
            name_label.grid(row=row, column=0, sticky='w', pady=6, padx=(0, 12))
            allowed = (fallback_providers if key == 'fallback' else helper_providers if key in ('auto', 'tools') else providers)
            allowed = allowed + (['off'] if key in ('fallback', 'auto', 'tools') else ['router'] if key == 'subagent' else [])
            box = ttk.Combobox(body, textvariable=fields['provider'], values=allowed, state='readonly', width=19)
            fields['provider_box'] = box
            box.grid(row=row, column=1, padx=(0, 10))
            if key == 'model':
                # a provider's own preset keeps its provider; a named one may start on any route
                box.configure(values=[preset['provider']] if preset['name'] == preset['provider'] else helper_providers)
            box.bind('<<ComboboxSelected>>', lambda _e, k=key: self.change_provider(k))
            model_box = ttk.Combobox(body, textvariable=fields['model'], width=38)
            model_box.grid(row=row, column=2, padx=(0, 10))
            model_box.bind('<<ComboboxSelected>>', lambda _e, k=key: self.pick_model(k))
            model_box.bind('<FocusOut>', lambda _e, k=key: self.load_efforts(k))
            effort_box = ttk.Combobox(body, textvariable=fields['effort'], state='readonly',
                                      values=['default', *EFFORTS], width=10)
            effort_box.grid(row=row, column=3, padx=(0, 10))
            # the spinner sits inside the model box, left of its arrow, while the provider's list loads
            spinner = ttk.Label(model_box, text='', style='Spin.TLabel')
            fields.update(model_box=model_box, effort_box=effort_box, spinner=spinner, loading=False, request=0)
            if key in ROUTER_LEVELS:
                last = key == self.router_levels[-1]
                fields['upto'] = tk.StringVar(value='100' if last else str(target.get('upto', LEVEL_TOPS.get(key, 50))))
                upto = (ttk.Label(body, text='100', style='Dim.TLabel') if last else
                        ttk.Spinbox(body, from_=0, to=99, increment=5, textvariable=fields['upto'], width=5))
                upto.grid(row=row, column=4, sticky='w')
                own = target.get('autocompact')
                fields['autocompact'] = tk.StringVar(value=f'{own}%' if type(own) is int else own or 'inherit')
                compact = ttk.Combobox(body, textvariable=fields['autocompact'], width=8,
                                       values=['inherit', *COMPACT_TOKENS, '50%', '70%', '90%'])
                compact.grid(row=row, column=5, sticky='w', padx=(10, 0))
                self.level_rows += [name_label, box, model_box, effort_box, upto, compact]
        self.spinner_job = None
        self.spinner_frame = 0
        self.model, self.effort = self.fields['model']['model'], self.fields['model']['effort']
        self.model_box, self.effort_box = self.fields['model']['model_box'], self.fields['model']['effort_box']
        self.status = StatusLabel(body, text='Loading models…', style='Faint.TLabel')
        self.status.grid(row=20, column=0, columnspan=6, sticky='we', pady=(10, 0))
        ttk.Label(body, text='Off = disable fallback / inherit helper. Router = each task goes to the level '
                  'by its difficulty (0-100); each level takes scores up to its top.',
                  style='Faint.TLabel', wraplength=900).grid(row=21, column=0, columnspan=6, sticky='w')
        foot = ttk.Frame(body)
        foot.grid(row=22, column=0, columnspan=6, sticky='we', pady=(16, 0))
        named = preset['name'] != preset['provider']
        if not preset.get('new') and (preset.get('custom') or not named):
            ttk.Button(foot, text='Delete' if named else 'Restore default', command=self.reset).pack(side='left')
        ttk.Button(foot, text='Cancel', command=self.close).pack(side='right', padx=(8, 0))
        ttk.Button(foot, text='Save', style='Accent.TButton', command=self.save).pack(side='right')
        self.levels_loaded = False
        self.show_router(router is not None)  # before sizing, so the dialog centres at its real height
        self.bind('<Escape>', lambda _e: self.close())
        self.transient(app)
        self.grab_set()
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_reqwidth()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f'+{max(0, x)}+{max(0, y)}')
        for key in ('subagent', 'fallback', 'auto', 'tools', 'model'):
            self.load_models(key)
        self.after(120, self.model_box.focus_set)

    def spin_models(self):
        self.spinner_job = None
        for fields in self.fields.values():
            frame = BUSY[self.spinner_frame % len(BUSY)]
            if fields['loading']:
                fields['spinner'].configure(text=frame)
                fields['spinner'].place(relx=1, x=-26, rely=0.5, anchor='e')
                fields['model_box'].configure(values=[f'{frame} {LOADING_MODELS}'])
            else:
                fields['spinner'].place_forget()
        self.spinner_frame += 1
        if any(fields['loading'] for fields in self.fields.values()):
            self.spinner_job = self.after(90, self.spin_models)

    def change_provider(self, key):
        """A new provider drops the old provider's model, then fills in the new one's first model."""
        fields = self.fields[key]
        fields['model'].set('')
        fields['effort'].set('default')
        fields['model_box'].configure(values=[])
        self.load_models(key)

    def load_models(self, key='model'):
        fields = self.fields[key]
        fields['request'] += 1
        request = fields['request']
        provider = fields['provider'].get()
        disabled = provider in ('off', 'router')
        if key == 'subagent':
            self.show_router(provider == 'router')
            if provider == 'router':
                fields['model'].set('by difficulty: levels below')
                fields['effort'].set('default')
        fields['typed'] = fields['model'].get()
        fields['loading'] = not disabled
        if self.spinner_job is not None:  # restart, so this box shows its spinner now, not on the next tick
            self.after_cancel(self.spinner_job)
        self.spin_models()
        fields['model_box'].configure(state='disabled' if disabled else 'normal')
        fields['effort_box'].configure(state='disabled' if disabled else 'readonly')
        if not disabled:
            self.app.background(lambda: run_ccx('models', provider),
                                lambda result: self.show_models(result, key)
                                if self.winfo_exists() and provider == fields['provider'].get()
                                and request == fields['request'] else None)

    def show_models(self, result, key='model'):
        if not self.winfo_exists():
            return
        fields = self.fields[key]
        fields['loading'] = False
        fields['spinner'].place_forget()
        ok, out = result
        models = plain_rows(out) if ok else []
        current = fields['model'].get().strip()
        if not current and models:
            current = models[0]
            fields['model'].set(current)
        values = [current, *[m for m in models if m != current]] if current else models
        fields['model_box'].configure(values=values)
        self.status.configure(text=f'{len(models)} models; type any id' if models else 'Type a model id.')
        self.load_efforts(key)

    def pick_model(self, key):
        """A pick from the list; the loading row is no model, so it brings the typed one back."""
        fields = self.fields[key]
        if fields['model'].get().endswith(LOADING_MODELS):
            fields['model'].set(fields.get('typed', ''))
            return
        self.load_efforts(key)

    def load_efforts(self, key='model'):
        fields = self.fields[key]
        provider, model = fields['provider'].get(), fields['model'].get().strip()
        if model and provider not in ('off', 'router'):
            self.app.background(lambda: efforts_for(provider, model),
                                lambda levels: self.show_efforts(levels, key)
                                if self.winfo_exists() and model == fields['model'].get().strip()
                                and provider == fields['provider'].get() else None)

    def show_efforts(self, levels, key='model'):
        if not self.winfo_exists():
            return
        fields = self.fields[key]
        values = ['default', *(EFFORTS if levels is None else levels)]
        fields['effort_box'].configure(values=values)
        if fields['effort'].get() not in values:
            fields['effort'].set('default')

    def show_router(self, on):
        """The level rows show only while the subagent is the router; their models load the first time."""
        for widget in self.level_rows:
            widget.grid() if on else widget.grid_remove()
        if on and not self.levels_loaded:
            self.levels_loaded = True
            for level in self.router_levels:
                self.load_models(level)

    def save(self):
        settings = {}
        router = self.fields['subagent']['provider'].get() == 'router'
        for key, fields in self.fields.items():
            if key in ROUTER_LEVELS and not router:
                continue
            provider, model = fields['provider'].get(), fields['model'].get().strip()
            if provider == 'off':
                settings[key] = {'provider': 'off'}
                continue
            if provider == 'router':
                continue
            if not provider or not model:
                self.status.configure(text=f'{key.title()}: pick a provider and model first.')
                return
            effort = fields['effort'].get()
            settings[key] = {'provider': provider, 'model': model,
                             **({'effort': effort} if effort not in ('', 'default') else {})}
            if key in self.router_levels:
                compact = fields['autocompact'].get().strip()
                if compact not in ('', 'inherit'):
                    settings[key]['autocompact'] = int(compact[:-1]) if compact.endswith('%') and compact[:-1].isdigit() else compact
            if key in self.router_levels[:-1]:
                try:
                    settings[key]['upto'] = int(fields['upto'].get())
                except ValueError:
                    self.status.configure(text=f'{key.title()}: its top must be a whole number.')
                    return
        if router:
            settings['subagent'] = {'provider': 'router', 'levels': {n: settings.pop(n) for n in self.router_levels}}
        args = ['presets', 'set-bundle', self.preset['name'], json.dumps(settings)]
        self.status.configure(text='Saving…')
        self.app.background(lambda: run_ccx(*args), self.done)

    def reset(self):
        self.status.configure(text='Restoring default…')
        self.app.background(lambda: run_ccx('presets', 'reset', self.preset['name']), self.done)

    def done(self, result):
        if not self.winfo_exists():
            return
        ok, out = result
        if not ok:
            self.status.configure(text=out.splitlines()[-1] if out else 'Could not save.')
            return
        self.saved()
        self.close()

    def close(self):
        if self.spinner_job is not None:
            self.after_cancel(self.spinner_job)
            self.spinner_job = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class App(tk.Tk):
    def __init__(self, start='model'):
        super().__init__()
        self.title('ccx model picker')
        self.configure(bg=BG)
        self.geometry('980x660')
        self.minsize(760, 520)
        self.option_add('*TCombobox*Listbox.background', FIELD)
        self.option_add('*TCombobox*Listbox.foreground', FG)
        self.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        self.option_add('*TCombobox*Listbox.selectForeground', 'white')
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.style.configure('.', background=BG, foreground=FG, fieldbackground=FIELD,
                             bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD, font=FONT)
        self.style.configure('TFrame', background=BG)
        self.style.configure('Tab.TFrame', background=BG)
        self.style.configure('Card.TFrame', background=PANEL)
        self.style.configure('TLabel', background=BG, foreground=FG)
        self.style.configure('Title.TLabel', background=BG, foreground=FG,
                             font=(UI, 16, 'bold'))
        self.style.configure('Dim.TLabel', background=BG, foreground=DIM)
        self.style.configure('Faint.TLabel', background=BG, foreground=FAINT)
        self.style.configure('Cost.TLabel', background=COST_BG, foreground=COST_FG, font=BOLD, padding=(10, 4))
        self.style.configure('Empty.TLabel', background=PANEL, foreground=FAINT, font=FONT)
        self.style.configure('TButton', background=FIELD, foreground=FG, borderwidth=0, focusthickness=0,
                             padding=(16, 8), font=FONT)
        self.style.map('TButton', background=[('active', HOVER), ('pressed', SELECTED)])
        self.style.configure('Accent.TButton', background=ACCENT, foreground='white', padding=(22, 8), font=BOLD)
        self.style.map('Accent.TButton', background=[('active', '#8aa3ff'), ('pressed', '#5574e6')])
        self.style.configure('TNotebook', background=BG, borderwidth=0, tabmargins=(14, 10, 14, 0),
                             bordercolor=BG, lightcolor=BG, darkcolor=BG)
        self.style.configure('TNotebook.Tab', background=BG, foreground=DIM, padding=(18, 9),
                             borderwidth=0, font=FONT, bordercolor=BG, lightcolor=BG, darkcolor=BG)
        self.style.map('TNotebook.Tab',
                       background=[('selected', PANEL)],
                       foreground=[('selected', ACCENT), ('active', FG)],
                       bordercolor=[('selected', PANEL)],
                       lightcolor=[('selected', PANEL)], darkcolor=[('selected', PANEL)])
        # One flat layout for every tab: clam's selected-tab focus ring otherwise shrinks it.
        self.style.layout('TNotebook.Tab', [
            ('Notebook.tab', {'sticky': 'nswe', 'children': [
                ('Notebook.padding', {'side': 'top', 'sticky': 'nswe', 'children': [
                    ('Notebook.label', {'side': 'top', 'sticky': ''})]})]})])
        self.style.configure('TEntry', fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                             bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=6)
        self.style.configure('TCombobox', fieldbackground=FIELD, background=PANEL, foreground=FG,
                             arrowcolor=DIM, bordercolor=BORDER, lightcolor=BORDER,
                             darkcolor=BORDER, padding=6)
        self.style.map('TCombobox', fieldbackground=[('readonly', FIELD)],
                       foreground=[('readonly', FG)], bordercolor=[('focus', ACCENT)])
        # A segmented switch (Subagent tab's mode and level): flat cells, the chosen one in the accent.
        self.style.configure('Seg.Toolbutton', background=FIELD, foreground=DIM, borderwidth=0, focusthickness=0,
                             padding=(14, 6), font=FONT, anchor='center')
        self.style.map('Seg.Toolbutton', background=[('selected', ACCENT), ('disabled', BG), ('active', HOVER)],
                       foreground=[('selected', 'white'), ('disabled', FAINT), ('active', FG)])
        self.style.configure('Spin.TLabel', background=FIELD, foreground=ACCENT, font=FONT)
        self.style.configure('TSpinbox', fieldbackground=FIELD, background=PANEL, foreground=FG, arrowcolor=DIM,
                             bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, insertcolor=FG, padding=4)
        self.style.map('TSpinbox', bordercolor=[('focus', ACCENT)])
        self.style.configure('TCheckbutton', background=BG, foreground=FG)
        self.style.map('TCheckbutton', background=[('active', BG)])
        self.style.configure('Picker.Treeview', background=FIELD, fieldbackground=FIELD, foreground=FG,
                             borderwidth=0, bordercolor=BORDER, rowheight=34, font=MONO)
        self.style.map('Picker.Treeview',
                       background=[('selected', SELECTED)], foreground=[('selected', 'white')])
        self.style.configure('Picker.Treeview.Heading', background=PANEL, foreground=DIM,
                             borderwidth=0, relief='flat', font=SMALL_BOLD,
                             padding=(10, 7))

        # A slim header naming the window and the session it edits, over an accent rule.
        head = ttk.Frame(self)
        head.pack(fill='x', padx=18, pady=(14, 10))
        ttk.Label(head, text='Model picker', style='Title.TLabel').pack(side='left')
        self.cost = ttk.Label(head, text='', style='Cost.TLabel')
        self.cost.pack(side='right', padx=(12, 0))
        self.session = ttk.Label(head, text=session_label(), style='Faint.TLabel')
        self.session.pack(side='right')
        self.usage = None
        GradientRule(self, height=3).pack(fill='x', padx=18)

        self.notebook = tk.Frame(self, bg=BG)
        self.notebook.pack(fill='x', padx=18, pady=(8, 0))
        self.container = tk.Frame(self, bg=BG)
        self.container.pack(fill='both', expand=True, pady=(4, 0))
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)
        self.tabs = {}
        self.tab_labels = {}
        self.facts_cache = {}  # provider -> {model id: ccx facts}
        self.models_cache = {}  # (tab, target, provider) -> successful model listing
        self.logo_cache = {}  # logo file stem -> PhotoImage
        for key, label in TABS:
            tab = PresetsTab(self, parent=self.container) if key == 'presets' else PickerTab(self, key, self.container)
            tab.grid(row=0, column=0, sticky='nsew')
            self.tabs[key] = tab
            node = tk.Label(self.notebook, text=label, bg=BG, fg=DIM, font=FONT,
                            padx=14, pady=8, cursor='hand2')
            node.pack(side='left')
            node.bind('<Button-1>', lambda _e, k=key: self.select(k))
            node.bind('<Enter>', lambda _e, k=key: self.hover_tab(k, True))
            node.bind('<Leave>', lambda _e, k=key: self.hover_tab(k, False))
            self.tab_labels[key] = node
        self.underline = tk.Frame(self.notebook, bg=ACCENT, height=2)
        self.selected = start if start in self.tabs else 'model'
        self.tabs[self.selected].tkraise()
        self.paint_tabs()
        self.notebook.bind('<Configure>', lambda _e: self.move_underline())
        self.after(30, self.move_underline)
        self.after(60, self.load_current)
        self.after(90, lambda: self.background(session_usage, self.show_usage))
        self.bind('<Escape>', lambda _e: self.destroy())
        ico = Path(__file__).with_name('ccx.ico')
        try:
            self.iconbitmap(default=str(ico))
        except tk.TclError:  # icon file missing or non-Windows Tk: plain accent square
            self.icon = tk.PhotoImage(width=16, height=16)
            self.icon.put(ACCENT, to=(0, 0, 16, 16))
            self.iconphoto(True, self.icon)
        self.center()
        dark_title_bar(self)
        self.after(150, self.raise_self)

    def raise_self(self):
        """Ask Windows for the foreground: the launcher that spawned us cannot give it."""
        if os.name != 'nt':
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # The classic raise: top-most once, then back, then foreground.
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_TOPMOST, no size/move
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def center(self):
        self.update_idletasks()
        width, height = 980, 660
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f'{width}x{height}+{x}+{y}')

    def show_usage(self, usage):
        """The session's API-price cost in the header, and the per-model estimate column."""
        if not isinstance(usage, dict):
            return
        self.usage = usage
        self.cost.configure(text=f'API {money(usage["usd"])} so far')
        for tab in self.tabs.values():
            if hasattr(tab, 'iids') and getattr(tab, 'models', None):
                tab.render_models()

    def background(self, work, done):
        """Work off the UI thread; its result (or failure) lands on the UI thread."""
        def run():
            try:
                result = work()
            except Exception:
                result = (False, traceback.format_exc(limit=2))
            self.after(0, lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    def load_current(self):
        self.tabs[self.current_key()].load()

    def current_key(self):
        return self.selected

    def logo(self, key):
        """A brand mark from ~/.config/ccx/logos, or None when the file is missing."""
        if not key:
            return None
        if key in self.logo_cache:
            return self.logo_cache[key]
        image = None
        path = LOGO_DIR / f'{key}.svg'
        if path.is_file():
            try:
                image = tk.PhotoImage(file=str(path))
                if image.width() > 64:  # some sources ship a huge canvas
                    image = image.subsample(max(1, round(image.width() / 24)))
            except tk.TclError:
                image = None
        self.logo_cache[key] = image
        return image

    def mark(self, slug, name):
        """The brand mark when ~/.config/ccx/logos has one, else an initials badge."""
        return self.logo(slug) or self.badge(name)

    def badge(self, name):
        """A round 24px badge with the name's initials, drawn pixel by pixel."""
        text = initials(name)
        key = f'badge:{text}:{name}'
        if key in self.logo_cache:
            return self.logo_cache[key]
        image = tk.PhotoImage(width=24, height=24)
        fill = badge_color(name)
        for y in range(24):
            row = [x for x in range(24) if (x - 11.5) ** 2 + (y - 11.5) ** 2 <= 132]
            if row:
                image.put(fill, to=(row[0], y, row[-1] + 1, y + 1))
        # each glyph pixel is 2x2; two glyphs 6px wide with a 2px gap, centred
        width = len(text) * 6 + (len(text) - 1) * 2
        left, top = (24 - width) // 2, 7
        for i, ch in enumerate(text):
            bits = GLYPHS.get(ch, GLYPHS['?'])
            for n, bit in enumerate(bits):
                if bit == '1':
                    x, y = left + i * 8 + (n % 3) * 2, top + (n // 3) * 2
                    image.put('white', to=(x, y, x + 2, y + 2))
        self.logo_cache[key] = image
        return image

    def select(self, key):
        if key not in self.tabs or key == self.selected:
            return
        self.selected = key
        self.tabs[key].tkraise()
        self.paint_tabs()
        self.after_idle(self.move_underline)
        self.load_current()

    def paint_tabs(self):
        """The selected tab reads as a raised panel; the rest stay quiet until hovered."""
        for name, node in self.tab_labels.items():
            selected = name == self.selected
            node.configure(bg=PANEL if selected else BG,
                           fg=FG if selected else DIM,
                           font=BOLD if selected else FONT)

    def hover_tab(self, key, inside):
        if key != self.selected:
            self.tab_labels[key].configure(bg=HOVER if inside else BG, fg=FG if inside else DIM)

    def move_underline(self):
        node = self.tab_labels.get(self.selected)
        if node is None:
            return
        if node.winfo_width() <= 1:  # not mapped yet: ask again once the bar has a size
            self.after(50, self.move_underline)
            return
        self.underline.place(x=node.winfo_x(), y=max(0, self.notebook.winfo_height() - 2),
                             width=node.winfo_width(), height=2)


def session_label():
    """What this window would change: the launch, or the plain session."""
    launch = os.environ.get('CCX_LAUNCH_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')
    provider = os.environ.get('CCX_MAIN_PROVIDER')
    if not launch:
        return 'no ccx launch'
    label = f'{provider} · {launch}' if provider else launch
    return label if len(label) <= 46 else label[:45] + '…'


def efforts_for(provider, model):
    """The model's effort ladder: None when unknown, empty when not adjustable."""
    snippet = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('ccx_switch', sys.argv[1])\n"
        "ccx = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(ccx)\n"
        "print(json.dumps(ccx.model_efforts(sys.argv[2], sys.argv[3])))\n")
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        done = subprocess.run([sys.executable, '-B', '-c', snippet, str(CCX), provider, model],
                              capture_output=True, text=True, creationflags=flags, timeout=60)
        levels = json.loads(done.stdout.strip().splitlines()[-1])
        return [level for level in levels if isinstance(level, str)] if isinstance(levels, list) else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError):
        return None


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
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


def lock_file():
    """One window per session, not one per machine."""
    key = os.environ.get('CCX_LAUNCH_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID') or 'default'
    safe = ''.join(ch for ch in key if ch.isalnum() or ch in '-_')[:60] or 'default'
    return Path.home() / '.config/ccx' / f'gui-{safe}.pid'


def raise_existing(pid):
    """Bring this session's open picker (process pid) forward (Windows), best effort.
    Every session's window shares one title, so a title lookup could raise another session's
    picker, whose Apply then routes that other launch."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        user32, found = ctypes.windll.user32, []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def check(hwnd, _):
            owner, title = ctypes.c_ulong(), ctypes.create_unicode_buffer(64)
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), title, 64)
            if owner.value == pid and title.value == 'ccx model picker':
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(check, None)
        if not found:
            return False
        user32.ShowWindow(ctypes.c_void_p(found[0]), 9)  # SW_RESTORE
        user32.SetForegroundWindow(ctypes.c_void_p(found[0]))
        return True
    except Exception:
        return False


def claim_window():
    """False when this session's window is already up (then it is raised)."""
    lock = lock_file()
    try:
        pid = int(lock.read_text(encoding='utf-8').strip())
        if pid != os.getpid() and pid_alive(pid):
            raise_existing(pid)
            return False
    except (OSError, ValueError):
        pass
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding='utf-8')
    except OSError:
        pass
    return True


def release_window():
    lock = lock_file()
    try:
        if lock.read_text(encoding='utf-8').strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    start = 'model'
    if '--tab' in argv:
        index = argv.index('--tab')
        if index + 1 < len(argv):
            start = argv[index + 1]
    if '--help' in argv or '-h' in argv:
        print('Usage: python ccx-gui.py [--tab model|subagent|fallback|auto|tools|presets]')
        return 0
    if not claim_window():
        return 0
    if os.name == 'nt':  # own taskbar identity: shared pythonw.exe otherwise borrows another app's pinned icon
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ccx.ModelPicker')
        except (AttributeError, OSError):
            pass
    try:
        app = App(start)
        app.mainloop()
        return 0
    finally:
        release_window()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log = Path.home() / '.config/ccx/gui-error.log'
        try:
            log.write_text(traceback.format_exc(), encoding='utf-8')
        except OSError:
            pass
        if sys.stderr:
            print(f'ccx-gui: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
