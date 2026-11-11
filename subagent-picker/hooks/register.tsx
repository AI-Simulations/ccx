import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, UiOpenResult } from 'claude-code'

import type { Col, Grid, GridTab, Row } from '../types'

// ccx's install folder; python expands ~ since hooks have no process or fs access
const CCX = '~/.config/ccx/ccx-switch.py'
const COST = '~/.config/ccx/statusline.py'
// runs a script path that starts with ~, as `python <script> <args>` would
const RUN = "import os, runpy, sys; p = os.path.expanduser(sys.argv[1]); sys.argv = [p] + sys.argv[2:]; sys.path.insert(0, os.path.dirname(p)); runpy.run_path(p, run_name='__main__')"
// Claude Code's ladder, for models whose provider publishes none of its own
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
// the Compact column: auto-compact at this share of the context window, or the default threshold
const PERCENTS = ['default', '100k', '200k', '300k', '400k', '600k', ...Array.from({ length: 10 }, (_, i) => String(95 - i * 5))]
// "100k" / "1.5m" as tokens; undefined for a percentage
const compactTokens = (v: string) => /^\d+(\.\d+)?[km]$/.test(v) ? Number(v.slice(0, -1)) * (v.endsWith('k') ? 1e3 : 1e6) : undefined
const compactLabel = (v: string) => v === 'default' ? 'def' : compactTokens(v) ? v : `${v}%`
const grid = atom({ plugin: 'subagent-picker', key: 'grid' } as const, null)
const cost = atom({ plugin: 'subagent-picker', key: 'cost' } as const, '')
// Green at $0 through yellow to red at $100 and above, the status line's gradient (hsv, s 0.6, v 0.9).
export const costColor = (usd: number) => {
  const hue = 120 * (1 - Math.min(Math.max(usd, 0), 100) / 100)
  const f = (n: number) => {
    const k = (n + hue / 60) % 6
    return Math.round(255 * 0.9 * (1 - 0.6 * Math.max(0, Math.min(k, 4 - k, 1))))
  }
  return '#' + [f(5), f(3), f(1)].map(c => c.toString(16).padStart(2, '0')).join('')
}

type Target = { provider: string; model: string; effort?: string }
type Profile = { name: string; provider: string; model: string; effort: string; autocompact: string; fallback?: Target }

// Filled at session start and after each change, so a command opens its menu with no process to wait on.
let cachedProfiles: Profile[] | undefined
let cachedProviders: Row[] | undefined
const cachedModels = new Map<string, Promise<string[]>>()

// A pane the engine does not draw (narrow terminal, no surface) must not swallow the command.
const UNPLACED = 'menu not drawn: '
const placed = (r: UiOpenResult) => (r.isPlaced ? undefined : UNPLACED + r.reason)

// The host process lacks the variables a Bash child gets, so ccx-switch.py would edit the global
// agents.json instead of this session's profiles: hand it the session as its Bash tool would.
async function sessionEnv($: EngineInterface) {
  return { CLAUDECODE: '1', CLAUDE_CODE_SESSION_ID: await $.session.id() }
}

// `python` on Windows, `python3` on macOS and most Linux; whichever starts first is kept.
let python: string | undefined
async function py($: EngineInterface, args: string[], init: { timeoutMs?: number; env?: Record<string, string> } = {}) {
  for (const exe of python ? [python] : ['python', 'python3']) {
    try {
      const r = await $.process.run([exe, ...args], init)
      // 9009: Windows' app-execution alias stub when no real python is installed
      if (!python && r.exitCode === 9009) continue
      python = exe
      return r
    } catch (err) {
      if (python) throw err
    }
  }
  throw new Error('No python or python3 on PATH; ccx needs Python 3.10+.')
}

async function ccx($: EngineInterface, args: string[]) {
  const r = await py($, ['-B', '-c', RUN, CCX, ...args], { timeoutMs: 60000, env: await sessionEnv($) })
  return { ok: r.exitCode === 0, out: `${r.stdout}\n${r.stderr}`.trim() }
}

const PROFILES = `import json,os
agents = json.load(open(os.path.expanduser('~/.config/ccx/agents.json')))
launch = os.environ.get('CCX_LAUNCH_ID', '') or 'claude-' + os.environ['CLAUDE_CODE_SESSION_ID']
if launch:
    try:
        own = json.load(open(os.path.expanduser('~/.config/ccx/session-agents.json'))).get(launch, {}).get('agents')
        if isinstance(own, dict):
            agents = {**own, **{n: a for n, a in agents.items() if n in ('easy', 'mid', 'hard') and n not in own}}
    except Exception:
        pass
print(json.dumps(agents))`

async function loadProfiles($: EngineInterface) {
  cachedProfiles = parseProfiles((await py($, ['-B', '-c', PROFILES], { env: await sessionEnv($) })).stdout)
  return cachedProfiles
}

// `value<TAB>label` rows, as ccx prints them for its pickers.
function tabRows(out: string): Row[] {
  return out.split(/\r?\n/).flatMap(l => {
    const [value, label] = l.split('\t')
    return value && label ? [{ value, label }] : []
  })
}

async function loadProviders($: EngineInterface) {
  cachedProviders = tabRows((await ccx($, ['agents', 'providers'])).out)
  return cachedProviders
}

// A listing (a live one takes seconds) kept a day in the plugin's store, across sessions; a failed
// refresh falls back to the stale copy, and with none is retried next time.
const DAY = 86_400_000
function listed($: EngineInterface, key: string, fetch: () => Promise<string[]>) {
  let list = cachedModels.get(key)
  if (!list) {
    list = (async () => {
      const hit = (await $.store.get(`models:${key}`).catch(() => undefined)) as { at: number; list: string[] } | undefined
      if (hit?.list?.length && Date.now() - hit.at < DAY) return hit.list
      try {
        const fresh = await fetch()
        if (fresh.length) await $.store.set(`models:${key}`, { at: Date.now(), list: fresh }).catch(() => undefined)
        return fresh
      } catch (err) {
        if (hit?.list?.length) return hit.list
        throw err
      }
    })()
    list.catch(() => cachedModels.delete(key))
    cachedModels.set(key, list)
  }
  return list
}

const lines = (out: string) => out.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith('('))
const models = ($: EngineInterface, agent: string, provider: string) =>
  listed($, provider, () => ccx($, ['agents', 'models', agent, provider]).then(({ ok, out }) => (ok ? parseModels(out) : Promise.reject(new Error(out)))))
const routeModels = ($: EngineInterface, provider: string) =>
  listed($, `route:${provider}`, () => ccx($, ['route', 'models', provider]).then(({ ok, out }) => (ok ? lines(out) : Promise.reject(new Error(out)))))
const fallbackModels = ($: EngineInterface, provider: string) =>
  listed($, `fallback:${provider}`, () => ccx($, ['fallback', 'models', provider]).then(({ ok, out }) => (ok ? lines(out) : Promise.reject(new Error(out)))))

// Every provider's lists, fetched in the background (three at a time) so no column waits on one later.
let warming: Promise<void> | undefined
function warmModels($: EngineInterface) {
  warming ??= (async () => {
    const agent = (cachedProfiles ?? (await loadProfiles($)))[0]?.name ?? 'sub'
    const providers = (cachedProviders ?? (await loadProviders($))).map(p => p.value).filter(p => p !== 'official' && p !== 'off')
    const jobs = providers.flatMap(p => [() => models($, agent, p), () => fallbackModels($, p), ...(nativeModel ? [] : [() => routeModels($, p)])])
    const worker = async () => {
      for (let job = jobs.shift(); job; job = jobs.shift()) await job().catch(() => undefined)
    }
    await Promise.all([worker(), worker(), worker()])
  })().finally(() => (warming = undefined))
  return warming
}

// agents.json as JSON: the profile's route, effort, auto-compact and fallback.
export function parseProfiles(out: string): Profile[] {
  let agents: Record<string, Record<string, unknown>>
  try {
    agents = JSON.parse(out.trim().split(/\r?\n/).pop() ?? '{}')
  } catch {
    return []
  }
  return Object.entries(agents).map(([name, a]) => {
    const fb = a.fallback as Target | undefined
    return {
      name,
      provider: String(a.provider ?? ''),
      model: String(a.model || 'default'),
      effort: String(a.effort || 'default'),
      autocompact: String(a.autocompact || 'default'),
      ...(fb?.provider && fb.model ? { fallback: { provider: fb.provider, model: fb.model, ...(fb.effort ? { effort: fb.effort } : {}) } } : {}),
    }
  })
}

// Model rows are indented, the profile's current one starred.
export function parseModels(out: string) {
  return out.split(/\r?\n/).filter(l => /^\s+\S/.test(l)).map(l => l.trim().replace(/^\*\s+/, ''))
}

// The family a model id belongs to, so one provider's list reads by category: every Claude together,
// every GPT together. Matched on the id's last segment; an unknown id falls back to its first word.
const FAMILIES: readonly (readonly [RegExp, string])[] = [
  [/claude|sonnet|opus|haiku|fable/i, 'Claude'],
  [/gpt|^o[1-9]\b|chatgpt/i, 'GPT'],
  [/gemini|gemma/i, 'Gemini'],
  [/deepseek/i, 'DeepSeek'],
  [/glm|zhipu|z-ai/i, 'GLM'],
  [/qwen|tongyi/i, 'Qwen'],
  [/kimi|moonshot/i, 'Kimi'],
  [/llama|meta-llama/i, 'Llama'],
  [/mistral|codestral|devstral/i, 'Mistral'],
  [/minimax/i, 'MiniMax'],
  [/mimo|xiaomi/i, 'MiMo'],
  [/grok|x-ai/i, 'Grok'],
  [/nemotron/i, 'Nemotron'],
  [/phi-/i, 'Phi'],
  [/command[-_ ]?code|cmdc/i, 'Command Code'],
]
export function modelFamily(id: string) {
  const tail = (id.split('/').pop() ?? id).trim()
  const known = (s: string) => FAMILIES.find(([pattern]) => pattern.test(s))?.[1]
  const family = known(tail) ?? known(id)
  if (family) return family
  const word = /^[a-z]+/i.exec(tail)?.[0]
  return word && word.length > 1 ? word[0]!.toUpperCase() + word.slice(1).toLowerCase() : 'Other'
}

// The typed search: rows whose name reads it, case-insensitively; the value is the name for most providers.
export function filterRows(rows: Row[], query: string) {
  const needle = query.trim().toLowerCase()
  return needle ? rows.filter(r => `${r.label ?? ''}\n${r.value}`.toLowerCase().includes(needle)) : rows
}

// The same list sorted by family, then by name: one category block per family.
export function familySort(rows: Row[]) {
  return [...rows].sort((a, b) => {
    const [fa, fb] = [modelFamily(a.value), modelFamily(b.value)]
    return fa === fb ? a.value.localeCompare(b.value) : fa.localeCompare(fb)
  })
}

export function shapeModels(rows: Row[], query: string, sort: Grid['sort']) {
  return filterRows(sort === 'family' ? familySort(rows) : rows, query)
}

// `ccx presets` rows: provider, model, effort and the label the column shows; the value carries all three
// as JSON, model ids having their own slashes.
export function parsePresets(out: string): Row[] {
  return out.split(/\r?\n/).flatMap(line => {
    const [provider, model, effort, label] = line.split('\t')
    if (!provider || !model) return []
    const target: Target = { provider, model, ...(effort && effort !== 'default' ? { effort } : {}) }
    return [{ value: JSON.stringify(target), label: label || `${provider}/${model}` }]
  })
}

export function presetTarget(value: string | undefined): Target | undefined {
  if (!value) return undefined
  try {
    const target = JSON.parse(value) as Target
    return typeof target.provider === 'string' && typeof target.model === 'string' ? target : undefined
  } catch {
    return undefined
  }
}

// `Route: provider/model/with/slashes` (or `Fallback: …`) and an optional `Effort: level` line.
export function parseTarget(out: string, name: 'Route' | 'Fallback' | 'Helper'): Target | undefined {
  const m = new RegExp(`^${name}: (\\S+?)/(\\S+)$`, 'm').exec(out)
  const effort = /^Effort: (\S+)$/m.exec(out)?.[1]
  return m ? { provider: m[1]!, model: m[2]!, ...(effort ? { effort } : {}) } : undefined
}

// One dot per step of the model's own ladder; `off` fills none.
export function effortBar(level: string, levels: readonly string[] = EFFORTS) {
  const steps = levels.filter(v => v !== 'off')
  const i = steps.indexOf(level)
  if (level !== 'off' && i < 0) return '◌'.repeat(steps.length)
  return '●'.repeat(i + 1) + '○'.repeat(steps.length - i - 1)
}

// The model's ladder (ccx's model_efforts: null when the provider publishes none, [] for no
// adjustable effort) and the level it runs at with no effort set: cmdc's per-model or global
// `~/.commandcode/config.json` effort, Codex's `default_reasoning_level`, or Claude Code's own
// `modelSettings` / `effortLevel` for official workers; null when unknown.
const EFFORT_INFO = `import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('ccx_switch', os.path.expanduser(sys.argv[1]))
ccx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccx)
provider, model = sys.argv[2], sys.argv[3]
levels = ccx.model_efforts(provider, model) if model != 'default' else None
default = None
try:
    if provider in ('commandcode', 'commandcode-cli'):
        cfg = json.load(open(os.path.expanduser('~/.commandcode/config.json'), encoding='utf-8'))
        default = (cfg.get('reasoningEffort') or {}).get(model) or cfg.get('effort')
    elif provider == 'openai-oauth':
        home = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
        rows = json.load(open(os.path.join(home, 'models_cache.json'), encoding='utf-8')).get('models') or []
        default = ccx.default_effort(provider, model) or next((r.get('default_reasoning_level') for r in rows if str(r.get('slug', '')).lower() == model.lower()), None)
    elif provider == 'official':
        cfg = json.load(open(os.path.expanduser('~/.claude/settings.json'), encoding='utf-8-sig'))
        default = ((cfg.get('modelSettings') or {}).get(model) or {}).get('effortLevel') or cfg.get('effortLevel')
except (OSError, ValueError, AttributeError):
    pass
print(json.dumps({'levels': levels, 'default': default}))`

const cachedEfforts = new Map<string, Promise<{ levels: string[] | null; default: string | null }>>()

function effortInfo($: EngineInterface, provider: string, model: string) {
  const key = `${provider}/${model}`
  let info = cachedEfforts.get(key)
  if (!info) {
    info = py($, ['-B', '-c', EFFORT_INFO, CCX, provider, model], { timeoutMs: 60000 }).then(r => {
      try {
        return JSON.parse(r.stdout.trim().split(/\r?\n/).pop() ?? '') as { levels: string[] | null; default: string | null }
      } catch {
        return { levels: null, default: null }
      }
    })
    cachedEfforts.set(key, info)
  }
  return info
}

// ---------------- /model: a grid in the command's own chat row ----------------

// The effort rows: the model's own ladder, its default level flagged on that level's row (which
// stores its explicit level, so choosing it updates an existing session) and a separate `default`
// row leads, so following the provider never disguises a named effort selection.
export function effortOptions(levels: readonly string[], fallback: string | null) {
  const flagged = fallback && levels.includes(fallback) ? fallback : null
  const pad = Math.max(...levels.map(v => v.length)) + 1
  const rows = levels.map(v => ({
    value: v,
    label: `${v.padEnd(pad)}${effortBar(v, levels)}${v === flagged ? ' def' : ''}`,
  }))
  return [{ value: 'default', label: 'default' }, ...rows]
}

const seqs: number[] = []
let sessionRoute: Target | undefined
let sessionFallback: Target | undefined
let autoTarget: Target | undefined
let toolsTarget: Target | undefined
const helperTab = (t: GridTab) => t.name === 'Auto' || t.name === 'Tools'
// Set when this session runs on Claude Code's own model (not a ccx adapter route): the Main model tab
// then sets it through the built-in /model and /effort.
let nativeModel: string | undefined
let fallbackOk = false
// shortcut: the current Claude lineup, refresh when a new generation ships
const CLAUDE_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1']

const colIndex = (t: GridTab, ...titles: string[]) => t.cols.findIndex(c => titles.includes(c.title))
const selected = (c: Col | undefined) => c?.rows[c.pick ?? c.at]?.value
// The subagent (or `session`) a tab sets; one profile draws no Subagent column.
const whoOf = (t: GridTab) => selected(t.cols[colIndex(t, 'Subagent', 'For')]) ?? (t.name === 'Subagent' ? cachedProfiles?.[0]?.name : undefined)
const show = (t?: Target) => (t ? `${t.provider}/${t.model}${t.effort ? `, effort ${t.effort}` : ''}` : 'off')

// What the tab's chosen subagent (or the session) runs on now.
function targetOf(t: GridTab, who: string | undefined): Target | undefined {
  if (t.name === 'Auto') return autoTarget
  if (t.name === 'Tools') return toolsTarget
  if (t.name === 'Model') return nativeModel ? { provider: 'official', model: nativeModel } : sessionRoute
  if (t.name === 'Fallback' && who === 'session') return sessionFallback
  const p = cachedProfiles?.find(x => x.name === who)
  if (!p) return undefined
  return t.name === 'Fallback' ? p.fallback : { provider: p.provider, model: p.model, effort: p.effort }
}

function nowLine(t: GridTab, who: string | undefined) {
  const now = targetOf(t, who)
  if (helperTab(t)) return `${t.name === 'Auto' ? 'Safety classifier' : 'WebFetch extraction and model-backed hooks'}: ${now ? show(now) : 'inherit session provider'}. ${t.name === 'Tools' ? 'Default: main model, low effort if supported; otherwise inherit. External search backends are separate.' : 'Permission checks remain enabled.'}`
  if (t.name === 'Model' && nativeModel) return `This session: Claude Code's own ${nativeModel}. Claude models switch now; another provider copies a ccx command that resumes this conversation there.`
  if (t.name === 'Fallback' && who === 'session' && !fallbackOk) return 'A session fallback needs a session started by ccx on an adapter route (ccx gemini …, ccx nvidia …).'
  if (t.name === 'Model') return `This session:${now ? show(now) : 'its launch route'}. Applies to the next request.`
  if (t.name === 'Subagent') return `Subagent ${who}: ${show(now)}. Applies to its next task.`
  return `${who === 'session' ? 'This session' : `Subagent ${who}`} falls back to ${show(now)} when its provider refuses (429, 402, 5xx).`
}

async function providerRows($: EngineInterface, t: GridTab, who: string | undefined): Promise<Row[]> {
  if (t.name === 'Subagent') return (cachedProviders ?? (await loadProviders($))).map(p => ({ value: p.value }))
  // a direct session switches provider only by relaunching: the other providers copy that command
  if (t.name === 'Model' && nativeModel)
    return [{ value: 'official' }, ...(cachedProviders ?? (await loadProviders($))).filter(p => p.value !== 'official').map(p => ({ value: p.value }))]
  if (t.name === 'Fallback' && who === 'session' && !fallbackOk) return []
  // the session's fallback list leaves out its own provider; shortcut: a subagent's `official` fallback
  // (a rerun on Claude) is set with `ccx fallback subagent`, not listed here
  const routes = tabRows((await ccx($, t.name === 'Fallback' && who === 'session' ? ['fallback', 'routes'] : ['route', 'routes'])).out)
  return [{ value: 'off', label: helperTab(t) ? 'inherit session' : t.name === 'Model' ? 'off (launch)' : 'off' }, ...routes.map(r => ({ value: r.value }))]
}

function modelList($: EngineInterface, t: GridTab, who: string | undefined, provider: string) {
  if (t.name === 'Model' && nativeModel && provider === 'official') return Promise.resolve(CLAUDE_MODELS.includes(nativeModel) ? CLAUDE_MODELS : [nativeModel, ...CLAUDE_MODELS])
  // ccx lists a provider's models through a profile; any one serves
  if (t.name === 'Model' && nativeModel) return models($, cachedProfiles?.[0]?.name ?? 'sub', provider)
  if (t.name === 'Model' || helperTab(t)) return routeModels($, provider)
  if (t.name === 'Fallback') return fallbackModels($, provider)
  return models($, who ?? '', provider).then(list => ['default', ...list])
}

function setTab($: EngineInterface, ti: number, change: (t: GridTab) => GridTab) {
  return update($, grid, g => (g ? { ...g, tabs: g.tabs.map((t, i) => (i === ti ? change(t) : t)) } : g))
}

// The model list's view: a family order and a typed search over each column's unfiltered `all`,
// so either can change without asking ccx for the list again.
let viewQuery = ''
let viewSort: Grid['sort']
const viewOf = (rows: Row[]) => shapeModels(rows, viewQuery, viewSort)

function setCol($: EngineInterface, ti: number, ci: number, col: Partial<Col>) {
  return setTab($, ti, t => ({
    ...t,
    cols: t.cols.map((c, i) => {
      if (i !== ci) return c
      const merged = { ...c, ...col }
      if (!col.rows || merged.title !== 'Model') return merged
      const picked = merged.rows[merged.pick ?? merged.at]?.value
      merged.all = col.rows
      merged.rows = viewOf(col.rows)
      const found = picked ? merged.rows.findIndex(r => r.value === picked) : -1
      if (found >= 0) merged.at = merged.pick = found
      else {
        merged.at = Math.max(0, Math.min(merged.at, merged.rows.length - 1))
        merged.pick = merged.at
      }
      if (viewQuery && col.rows.length && !merged.rows.length) merged.note = 'no match'
      else if (!viewQuery && merged.note === 'no match') merged.note = undefined
      return merged
    }),
  }))
}

// Re-shape every tab's model columns from their source lists: a new search or order, no refetch.
function reflow($: EngineInterface) {
  return update($, grid, x => {
    if (!x) return x
    return {
      ...x,
      tabs: x.tabs.map(tab => ({
        ...tab,
        cols: tab.cols.map(c => {
          if (c.title !== 'Model' || !c.all) return c
          const picked = c.rows[c.pick ?? c.at]?.value
          const rows = viewOf(c.all)
          const found = picked ? rows.findIndex(r => r.value === picked) : -1
          const fit = (n: number) => Math.max(0, Math.min(n, rows.length - 1))
          const note = viewQuery
            ? (c.all.length && !rows.length ? 'no match' : undefined)
            : (c.note === 'no match' ? undefined : c.note)
          return found >= 0 ? { ...c, rows, at: found, pick: found, note }
            : { ...c, rows, at: fit(c.at), pick: fit(c.pick ?? c.at), note }
        }),
      })),
    }
  })
}

async function setQuery($: EngineInterface, query: string) {
  viewQuery = query
  await update($, grid, g => (g ? { ...g, query } : g))
  await reflow($)
}

async function toggleSort($: EngineInterface) {
  viewSort = viewSort === 'family' ? undefined : 'family'
  await update($, grid, g => (g ? { ...g, sort: viewSort } : g))
  await reflow($)
}

const at = (rows: Row[], value: string | undefined) => Math.max(0, rows.findIndex(r => r.value === value))
// A refilled column's cursor and pick both start on its default: the value in use, else the first row.
const cursor = (rows: Row[], value: string | undefined) => ({ at: at(rows, value), pick: at(rows, value) })

const COMPACT_ROWS: Row[] = PERCENTS.map(v => ({ value: v, label: compactLabel(v) }))
// The main session's threshold, kept per session id so a resume keeps it.
const compactKey = async ($: EngineInterface) => `compact:${await $.session.id()}`
async function mainCompact($: EngineInterface) {
  return ((await $.store.get(await compactKey($)).catch(() => undefined)) as string | undefined) ?? 'default'
}
async function compactNow($: EngineInterface, t: GridTab, who: string | undefined) {
  return t.name === 'Model' ? mainCompact($) : cachedProfiles?.find(p => p.name === who)?.autocompact ?? 'default'
}

// Refill the columns right of `changed` from the selection left of them: a new subagent re-reads its
// providers, a new provider its models, a new model its efforts. A later move cancels an older fill.
async function refill($: EngineInterface, ti: number, changed: number) {
  const my = (seqs[ti] = (seqs[ti] ?? 0) + 1)
  const live = () => seqs[ti] === my
  const tabNow = async () => ((await read($, grid)) as Grid | null)?.tabs[ti]
  let t = await tabNow()
  if (!t) return
  const iw = colIndex(t, 'Subagent', 'For')
  const ip = colIndex(t, 'Provider')
  if (ip < 0) return // the Presets tab is a plain list; it has nothing to refill
  const im = colIndex(t, 'Model')
  const ie = colIndex(t, 'Effort')
  const who = whoOf(t)
  const now = targetOf(t, who)
  if (changed < ip) {
    await setTab($, ti, x => ({ ...x, now: nowLine(x, who) }))
    const ic = colIndex(t, 'Compact')
    if (ic >= 0) {
      const current = await compactNow($, t, who)
      if (!live()) return
      await setCol($, ti, ic, { rows: COMPACT_ROWS, current, ...cursor(COMPACT_ROWS, current) })
    }
    const rows = await providerRows($, t, who)
    if (!live()) return
    const current = now?.provider ?? 'off'
    await setCol($, ti, ip, { rows, current, ...cursor(rows, current), note: rows.length ? undefined : 'not here' })
    if (!rows.length) {
      await setCol($, ti, im, { rows: [], note: '—' })
      await setCol($, ti, ie, { rows: [], note: '—' })
      return
    }
    t = await tabNow()
    if (!t) return
  }
  const provider = selected(t.cols[ip])
  if (changed < im) {
    if (!provider || provider === 'off') {
      await setCol($, ti, im, { rows: [], note: '—' })
      await setCol($, ti, ie, { rows: [], note: '—' })
      return
    }
    await setCol($, ti, im, { rows: [], note: 'Loading…' })
    await setCol($, ti, ie, { rows: [], note: '' })
    try {
      const rows = (await modelList($, t, who, provider)).map(v => ({ value: v, ...(v === 'default' ? { label: 'default (provider)' } : {}) }))
      if (!live()) return
      const current = now?.provider === provider ? now.model : undefined
      await setCol($, ti, im, { rows, current, ...cursor(rows, current), note: rows.length ? undefined : 'none listed' })
    } catch (err) {
      if (live()) await setCol($, ti, im, { rows: [], note: String((err as Error).message).split(/\r?\n/)[0] })
      return
    }
    t = await tabNow()
    if (!t) return
  }
  // a move in Effort (or a column right of it) keeps the effort cursor where the person put it
  if (changed >= ie) return
  const model = selected(t.cols[im])
  if (!provider || provider === 'off' || !model) return
  await setCol($, ti, ie, { rows: [], note: 'Loading…' })
  const info = await effortInfo($, provider, model)
  if (!live()) return
  const levels = info.levels ?? EFFORTS
  if (!levels.length) return setCol($, ti, ie, { rows: [], note: 'none' })
  const rows = effortOptions(levels, info.default)
  const same = now?.provider === provider && now.model === model
  // an explicit level equal to the flagged default ticks that row
  const effort = now?.effort ?? 'default'
  await setCol($, ti, ie, { rows, current: same ? effort : undefined, ...cursor(rows, same ? effort : 'default') })
}

async function buildGrid($: EngineInterface, start: GridTab['name'], who?: string): Promise<Grid> {
  const [route, fallback, profiles, auto, tools, preset, router] = await Promise.all([
    ccx($, ['route']), ccx($, ['fallback']), loadProfiles($), ccx($, ['helper', 'auto']), ccx($, ['helper', 'tools']),
    ccx($, ['presets']), ccx($, ['agents', 'router']),
  ])
  autoTarget = auto.ok ? parseTarget(auto.out, 'Helper') : undefined
  toolsTarget = tools.ok ? parseTarget(tools.out, 'Helper') : undefined
  sessionRoute = route.ok ? parseTarget(route.out, 'Route') : undefined
  sessionFallback = fallback.ok ? parseTarget(fallback.out, 'Fallback') : undefined
  nativeModel = route.ok ? undefined : await $.session.model()
  fallbackOk = fallback.ok
  const cols = (...titles: string[]): Col[] => titles.map(title => ({ title, rows: [], at: 0, note: title === 'Provider' ? 'Loading…' : '' }))
  const names = profiles.map(p => p.name)
  const subagents = names.map(value => ({ value }))
  // Each provider's default pick: the same helper defaults the launcher uses, one Enter away.
  const presets = preset.ok ? parsePresets(preset.out) : []
  const here = presets.find(r => {
    const t = presetTarget(r.value)
    return !!t && !!sessionRoute && t.provider === sessionRoute.provider && t.model === sessionRoute.model
  })
  const tabs: GridTab[] = [
    { name: 'Model', now: '', cols: cols('Provider', 'Model', 'Effort', 'Compact') },
    presets.length
      ? { name: 'Presets', now: 'Provider defaults; Enter sets this session route.', cols: [{ title: 'Preset', rows: presets, ...cursor(presets, here?.value) }] }
      : { name: 'Presets', now: preset.ok ? 'No provider defaults listed.' : preset.out, cols: [] },
    names.length
      ? { name: 'Subagent', now: '', cols: names.length > 1 ? [{ title: 'Subagent', rows: subagents, ...cursor(subagents, who) }, ...cols('Provider', 'Model', 'Effort', 'Compact')] : cols('Provider', 'Model', 'Effort', 'Compact') }
      : { name: 'Subagent', now: 'No configured ccx subagents.', cols: [] },
  ]
  tabs.push(
    auto.ok ? { name: 'Auto', now: '', cols: cols('Provider', 'Model', 'Effort') }
      : { name: 'Auto', now: auto.out, cols: [] },
    tools.ok ? { name: 'Tools', now: '', cols: cols('Provider', 'Model', 'Effort') }
      : { name: 'Tools', now: tools.out, cols: [] },
  )
  // the session's row stays when it cannot have a fallback, saying why
  const forRows = [{ value: 'session' }, ...names.map(value => ({ value }))]
  tabs.push(forRows.length
    ? { name: 'Fallback', now: '', cols: [{ title: 'For', rows: forRows, ...cursor(forRows, who) }, ...cols('Provider', 'Model', 'Effort')] }
    : { name: 'Fallback', now: fallback.out.split(/\r?\n/).pop() ?? fallback.out, cols: [] })
  const tab = Math.max(0, tabs.findIndex(t => t.name === start))
  return {
    tab, col: Math.max(0, colIndex(tabs[tab]!, 'Provider')), tabs, room: 10,
    ...(viewQuery ? { query: viewQuery } : {}), ...(viewSort ? { sort: viewSort } : {}),
    // the Subagent tab's router switch, drawn only when the level profiles exist
    ...(router.ok && /^Router o(n|ff)\b/.test(router.out) ? { router: router.out.startsWith('Router on') } : {}),
  }
}

export const ROUTER_USAGE = 'Usage: /model router [on | off | best [provider] | ranges [easy-top mid-top] | easy|mid|hard <provider> <model> [effort]]'

// `/model router` words as the ccx command that does them; undefined for anything else.
export function routerArgs(words: string[]): string[] | undefined {
  const [verb, ...rest] = words.map(w => w.trim()).filter(Boolean)
  const level = verb?.toLowerCase()
  if (!level) return ['agents', 'router']
  if ((level === 'on' || level === 'off') && !rest.length) return ['agents', 'router', level]
  if (level === 'best' && rest.length <= 1) return ['agents', 'router', 'best', ...rest]
  if (level === 'ranges' && (rest.length === 0 || rest.length === 2)) return ['agents', 'router', 'ranges', ...rest]
  if (['easy', 'mid', 'hard'].includes(level) && (rest.length === 2 || rest.length === 3))
    return ['agents', 'set', level, '--provider', rest[0]!, '--model', rest[1]!, ...(rest[2] ? ['--effort', rest[2]] : [])]
  return undefined
}

// /model's grid: a pane holding the keyboard, as the built-in picker does (nothing reaches the prompt).
const GRID_PANE = 'model-picker'
const GRID_SIZE = { rows: 22, columns: 110 } as const

// /model (and /fallback) with no arguments: the grid, opened on `start`.
async function openGrid($: EngineInterface, start: GridTab['name'], who?: string) {
  // a reopened grid starts unfiltered, in the provider's own order
  viewQuery = ''
  viewSort = undefined
  const g = await buildGrid($, start, who)
  await update($, grid, () => g)
  g.tabs.forEach((t, ti) => {
    if (t.cols.length) void refill($, ti, colIndex(t, 'Subagent', 'For')).catch(() => undefined)
  })
  const out = placed(await $.ui.open({ id: GRID_PANE, title: '/model', focus: true, closeOnEscape: true, ...GRID_SIZE }))
  void runMarquee($).catch(() => undefined)
  void warmModels($).catch(() => undefined)
  return out
}

// The cursor cell scrolls a name wider than its column: a step per 250 ms while the pane is open.
let marquee = 0
let marqueeOn = false
let marqueeWanted = false
async function runMarquee($: EngineInterface) {
  if (marqueeOn) return
  marqueeOn = true
  try {
    for (let i = 0; ; i++) {
      await $.clock.sleep(250)
      const panes = await $.ui.panes()
      if (!panes.some(p => p.id === GRID_PANE)) return
      // modal, as the built-in picker: keys that land outside the grid come back to it
      if (!panes.some(p => p.isFocused)) await takeGridKeys($)
      if (!marqueeWanted) continue
      marquee++
      await update($, grid, x => (x ? { ...x, tick: marquee } : x))
    }
  } finally {
    marqueeOn = false
  }
}

// A blocked prompt is put back in the composer, and a pane cannot take the keyboard over text there:
// empty it and put the ring on the cursor cell once the pane holds the keys.
async function takeGridKeys($: EngineInterface) {
  for (let i = 0; i < 20; i++) {
    await $.clock.sleep(100)
    if ((await $.prompt.read()).text.trim()) await $.prompt.fill({ text: '' })
    const pane = (await $.ui.panes()).find(p => p.id === GRID_PANE)
    if (!pane) return
    if (!pane.isFocused) await $.ui.open({ id: GRID_PANE, title: '/model', focus: true, closeOnEscape: true, ...GRID_SIZE })
    else if (!('deny' in (await $.ui.focus({ requestId: GRID_PANE, key: 'C' }).catch(err => ({ deny: String(err) }))))) return
  }
}

// The keys the focus ring's targets stand for. The engine walks Tab through the elements in drawing
// order and moves the arrows to the nearest element that way, so the cursor cell `C` is drawn
// between the two tab targets, and one target sits above, below, left and right of it.
export const RING_KEYS: Record<string, [string, boolean]> = {
  'T-': ['tab', true], 'T+': ['tab', false], U: ['up', false], D: ['down', false], L: ['left', false], R: ['right', false],
  F: ['find', false], S: ['sort', false], X: ['router', false],
}

// Focus the search field: its typing filters the model list, and arrows then walk the result.
const focusFind = ($: EngineInterface) =>
  $.ui.focus({ requestId: GRID_PANE, key: 'Q' }).catch(err => $.ui.toast(String(err)))

// A key for the grid: Tab cycles the tabs, ←/→ the columns, ↑/↓ the cursor (wrapping), Space picks the
// cursor's row, Enter sets every column's pick (each starts on the value in use). `f` finds by name,
// `s` toggles the family order.
async function onGridKey($: EngineInterface, key: string, shift: boolean) {
  marquee = 0
  const g = (await read($, grid)) as Grid | null
  if (!g) return
  const t = g.tabs[g.tab]!
  if (key === 'find') return focusFind($)
  if (key === 'sort') return toggleSort($)
  if (key === 'router') {
    if (t.name !== 'Subagent' || g.router === undefined) return
    const { ok, out } = await ccx($, ['agents', 'router', g.router ? 'off' : 'on'])
    const said = out.split(/\r?\n/).filter(Boolean).pop() ?? out
    return update($, grid, x => (x ? { ...x, ...(ok ? { router: !g.router } : {}), status: ok ? `✔ ${said}` : said } : x))
  }
  if (key === 'tab') {
    const tab = (g.tab + (shift ? -1 : 1) + g.tabs.length) % g.tabs.length
    return update($, grid, x => (x ? { ...x, tab, col: Math.max(0, colIndex(x.tabs[tab]!, 'Provider')), status: undefined } : x))
  }
  if (key === 'left' || key === 'right') {
    const col = Math.max(0, Math.min(t.cols.length - 1, g.col + (key === 'left' ? -1 : 1)))
    return update($, grid, x => (x ? { ...x, col } : x))
  }
  if (key === 'up' || key === 'down') {
    const c = t.cols[g.col]
    if (!c?.rows.length) return
    const next = (c.at + (key === 'up' ? -1 : 1) + c.rows.length) % c.rows.length
    // the pick stays where it was: only Space moves it
    await setCol($, g.tab, g.col, { at: next, pick: c.pick ?? c.at })
    return update($, grid, x => (x ? { ...x, status: undefined } : x))
  }
  if (key === 'space') {
    const c = t.cols[g.col]
    if (!c?.rows.length) return
    await setCol($, g.tab, g.col, { pick: c.at })
    await update($, grid, x => (x ? { ...x, status: undefined } : x))
    // the columns to the right follow in the background
    void refill($, g.tab, g.col).catch(() => undefined)
    return
  }
  if (key === 'return') return applyGrid($, g)
}

// The ccx command that resumes this conversation on another route.
export function relaunchCommand(provider: string, model: string, effort: string, session: string) {
  return `ccx ${provider} ${model}${EFFORTS.includes(effort) ? ` --effort ${effort}` : ''} --resume ${session}`
}

// A session on Claude Code's own model: the built-in /model, then /effort when one was picked.
async function applyNative($: EngineInterface, g: Grid, provider: string, model: string, effort: string) {
  if (provider !== 'official') {
    const command = relaunchCommand(provider, model, effort, await $.session.id())
    const copied = !(await $.ui.copy({ text: command }).catch(() => ({ deny: 'no clipboard' })) as { deny?: string }).deny
    return update($, grid, x => (x ? { ...x, status: `Exit, then relaunch to switch: ${command}${copied ? '  (copied)' : ''}` } : x))
  }
  const ran = [await $.command.run({ command: 'model', args: model })]
  if (effort !== 'default') ran.push(await $.command.run({ command: 'effort', args: effort }))
  const compact = selected(g.tabs[g.tab]!.cols[colIndex(g.tabs[g.tab]!, 'Compact')])
  const said = (ran.map(r => String((r as { text?: string }).text ?? '').split(/\r?\n/)[0]).filter(Boolean).join(', ') || `model ${model}`)
    + (compact ? `, compact ${compact === 'default' ? 'default' : compactLabel(compact)}` : '')
  const fresh = await buildGrid($, 'Model')
  await update($, grid, () => ({ ...fresh, tab: g.tab, col: g.col, status: `✔ ${said}` }))
  fresh.tabs.forEach((x, ti) => {
    if (x.cols.length) void refill($, ti, colIndex(x, 'Subagent', 'For')).catch(() => undefined)
  })
}

// Enter: the tab's selection, through the same ccx commands the terminal uses; the grid stays for more.
async function applyGrid($: EngineInterface, g: Grid) {
  const t = g.tabs[g.tab]!
  // Presets carry their own provider, model and effort: one Enter is the whole pick.
  if (t.name === 'Presets') {
    const target = presetTarget(selected(t.cols[colIndex(t, 'Preset')]))
    if (!target) return
    if (nativeModel) return applyNative($, g, target.provider, target.model, target.effort ?? 'default')
    const { ok, out } = await ccx($, ['route', target.provider, target.model, ...(target.effort ? [target.effort] : [])])
    if (!ok) return update($, grid, x => (x ? { ...x, status: out.split(/\r?\n/).pop() ?? out } : x))
    // the preset's subagent too: the router (its levels and ranges) or one model
    const sub = await ccx($, ['presets', 'use-subagent', target.provider])
    const said = `${target.provider}/${target.model}${target.effort ? `, effort ${target.effort}` : ''}`
      + (sub.ok ? `; ${sub.out.trim().split(/\r?\n/).pop()}` : '')
    const fresh = await buildGrid($, 'Presets')
    await update($, grid, () => ({ ...fresh, tab: g.tab, col: g.col, status: `✔ ${said}` }))
    fresh.tabs.forEach((x, ti) => {
      if (x.cols.length) void refill($, ti, colIndex(x, 'Subagent', 'For')).catch(() => undefined)
    })
    $.ui.toast(said)
    return
  }
  const who = whoOf(t)
  const provider = selected(t.cols[colIndex(t, 'Provider')])
  const model = selected(t.cols[colIndex(t, 'Model')])
  const effort = selected(t.cols[colIndex(t, 'Effort')]) ?? 'default'
  const compact = selected(t.cols[colIndex(t, 'Compact')])
  if (t.name === 'Model' && compact) {
    if (compact === 'default') await $.store.delete(await compactKey($))
    else await $.store.set(await compactKey($), compact)
  }
  if (!provider) return
  if (provider !== 'off' && !model) return update($, grid, x => (x ? { ...x, status: 'Pick a model first (the list is still loading or empty).' } : x))
  if (t.name === 'Model' && nativeModel) return applyNative($, g, provider, model!, effort)
  const pair = provider === 'off' ? ['off'] : [provider, model!, ...(effort === 'default' ? [] : [effort])]
  const args = helperTab(t) ? ['helper', t.name.toLowerCase(), ...(provider === 'off' ? ['inherit'] : pair)]
    : t.name === 'Model' ? ['route', ...pair]
    : t.name === 'Subagent' ? ['agents', 'set', who!, '--provider', provider, '--model', model!, '--effort', effort, ...(compact ? ['--autocompact', compact] : [])]
    : who === 'session' ? ['fallback', ...pair] : ['fallback', 'subagent', who!, ...pair]
  const { ok, out } = await ccx($, args)
  const said = (t.name === 'Subagent' ? `${who}: ${provider}/${model}, effort ${effort}` : out.split(/\r?\n/).filter(Boolean).join(', '))
    + (compact && t.name !== 'Fallback' ? `, compact ${compact === 'default' ? 'default' : compactLabel(compact)}` : '')
  if (!ok) return update($, grid, x => (x ? { ...x, status: out.split(/\r?\n/).pop() ?? out } : x))
  // Adapter route effort is launch-scoped; native /effort would also persist it globally.
  // re-read what is set so the ticks move, on the same tab and column
  const fresh = await buildGrid($, t.name, who)
  await update($, grid, () => ({ ...fresh, tab: g.tab, col: g.col, status: `✔ ${said}` }))
  fresh.tabs.forEach((x, ti) => {
    if (x.cols.length) void refill($, ti, colIndex(x, 'Subagent', 'For')).catch(() => undefined)
  })
  $.ui.toast(said)
}

// The window of a grid column: centred on its cursor, clamped to the list.
export function colTop(at: number, count: number, room: number) {
  return Math.max(0, Math.min(count - room, at - Math.floor(room / 2)))
}

type Spot = { x: number; y: number; label: string }
type Line = { text: string; style?: { dimColor?: boolean; bold?: boolean; color?: string } }

// The grid as lines of text, and where the focus ring's targets go over it: the cursor cell, the cells
// above and below it, the neighbour columns' cursor cells (or a blank at the edge), and the tab line's ends.
// The cursor cell's text, scrolled `tick` steps when it is wider than `n`: it starts moving at once
// and pauses at the end before going round.
export function marqueeText(s: string, n: number, tick: number) {
  const over = s.length - n
  if (over <= 0) return s.padEnd(n)
  const PAUSE = 4
  const p = Math.min(over, tick % (over + PAUSE))
  return s.slice(p, p + n)
}

export function gridLayout(g: Grid, width: number, room: number, tick = 0) {
  const MARGIN = 2
  const GAP = 2
  const FIRST = 4 // tab line, now line, blank, titles
  const t = g.tabs[g.tab]!
  // each column as wide as its widest row (mark, label, tick) within a cap; Model takes what is left
  const CAP: Record<string, number> = { Provider: 15, Effort: 20, Compact: 8 }
  const fixed = (c: Col) => c.title === 'Model' ? 0
    : Math.min(CAP[c.title] ?? 14, Math.max(c.title.length + 1, ...c.rows.map(r => (r.label ?? r.value).length + 4)))
  const available = width - MARGIN - 2
  const fixedWidth = t.cols.reduce((n, c) => n + fixed(c), 0) + GAP * Math.max(0, t.cols.length - 1)
  const wrapped = t.cols.length > 2 && fixedWidth + (t.cols.some(c => c.title === 'Model') ? 24 : 0) > available
  const across = wrapped ? 2 : Math.max(1, t.cols.length)
  const groups = Math.ceil(t.cols.length / across)
  const activeGroup = wrapped ? Math.floor(g.col / across) : 0
  const hidden = wrapped ? t.cols.map((_, i) => i).filter(i => Math.floor(i / across) !== activeGroup) : []
  if (wrapped) room = Math.max(1, room - Math.ceil(hidden.length / 2))
  const rest = Math.max(12, available - fixedWidth)
  const w = t.cols.map((c, i) => wrapped
    ? Math.floor((available - GAP) / 2) + (i % 2 ? (available - GAP) % 2 : 0)
    : fixed(c) || rest)
  const x = w.map((_, i) => MARGIN + (wrapped ? (i % 2 ? w[i - 1]! + GAP : 0)
    : w.slice(0, i).reduce((n, v) => n + v + GAP, 0)))
  const first = (_ci: number) => FIRST
  const tops = t.cols.map(c => colTop(c.at, c.rows.length, room))
  const fit = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s.padEnd(n))
  const cell = (ci: number, i: number) => {
    const c = t.cols[ci]!
    const r = c.rows[i]
    if (!r) return fit(i === 0 && !c.rows.length ? `  ${c.note || '—'}` : '', w[ci]!)
    // ❯ the cursor in the focused column, › each column's pick
    const mark = ci === g.col && i === c.at ? '❯' : i === (c.pick ?? c.at) ? '›' : ' '
    if (i === c.at && ci === g.col) return `${mark} ${marqueeText(`${r.label ?? r.value}${r.value === c.current ? ' ✔' : ''}`, w[ci]! - 2, tick)}`
    return fit(`${mark} ${r.label ?? r.value}${r.value === c.current ? ' ✔' : ''}`, w[ci]!)
  }
  const title = (ci: number) => fit(`${ci === g.col ? '▸' : ' '}${t.cols[ci]!.title}`, w[ci]!)
  const more = (ci: number) => {
    const n = t.cols[ci]!.rows.length - tops[ci]! - room
    return fit(n > 0 ? `  … +${n} more` : '', w[ci]!)
  }
  const row = (group: number, f: (ci: number) => string) => ' '.repeat(MARGIN)
    + t.cols.slice(group * across, (group + 1) * across).map((_, i) => f(group * across + i)).join(' '.repeat(GAP))
  const names = g.tabs.map((x, i) => {
    const name = x.name === 'Model' ? 'Main model' : x.name === 'Fallback' ? 'Fallbacks' : x.name
    return i === g.tab ? `[ ${name} ]` : `  ${name}  `
  })
  const strip = ' '.repeat(MARGIN) + names.join(' ')
  const lines: Line[] = [{ text: fit(strip, width), style: { bold: true } }, { text: fit(t.now, width), style: { dimColor: true } }, { text: '' }]
  for (let group = activeGroup; group < (wrapped ? activeGroup + 1 : groups); group++) {
    lines.push({ text: row(group, title), style: { bold: true } })
    for (let r = 0; r < room; r++) lines.push({ text: row(group, ci => cell(ci, tops[ci]! + r)) })
    lines.push({ text: row(group, more), style: { dimColor: true } })
  }
  for (let i = 0; i < hidden.length; i += 2) {
    lines.push({ text: ' '.repeat(MARGIN) + hidden.slice(i, i + 2).map(ci => {
      const c = t.cols[ci]!
      const r = c.rows[c.pick ?? c.at]
      return fit(`${c.title}: ${r?.label ?? r?.value ?? c.note ?? '—'}${r && r.value === c.current ? ' ✔' : ''}`, w[ci]!)
    }).join(' '.repeat(GAP)), style: { dimColor: true } })
  }
  lines.push({ text: '' })
  if (g.status) lines.push({ text: fit(g.status, width), style: { color: g.status.startsWith('✔') ? 'success' : 'warning' } })
  const help = `Tab tabs · ←/→ column · ↑/↓ move · Space pick · Enter set · f find · s sort${t.name === 'Subagent' && g.router !== undefined ? ' · r router' : ''} · Esc close`
  for (let at = 0; at < help.length; at += width) lines.push({ text: help.slice(at, at + width), style: { dimColor: true } })
  // The find field and the sort toggle sit on the blank line, out of the arrows' way.
  const sortLabel = `sort: ${g.sort === 'family' ? 'family' : 'listed'}`
  const sortSpot = { x: Math.max(MARGIN + 24, width - sortLabel.length - 2), y: 2, label: sortLabel }
  const findSpot = { x: MARGIN, y: 2, label: 'find' }
  const routerLabel = t.name === 'Subagent' && g.router !== undefined ? `router: ${g.router ? 'on' : 'off'}` : ''
  const routerSpot = { x: routerLabel ? Math.max(MARGIN + 20, sortSpot.x - routerLabel.length - 6) : sortSpot.x, y: 2, label: routerLabel }
  const filterSpot = { x: MARGIN + 8, y: 2, width: Math.max(8, routerSpot.x - MARGIN - 10) }
  // where an index of column ci is drawn; an empty column's note sits on its first row
  const spot = (ci: number, i: number): Spot => ({ x: x[ci]!, y: first(ci) + Math.max(0, i - tops[ci]!), label: cell(ci, i) })
  const blank = (sx: number, y: number): Spot => ({ x: sx, y, label: ' ' })
  const col = Math.min(g.col, Math.max(0, t.cols.length - 1))
  const c = t.cols[col]
  // a tab without columns still holds the ring, on its now line
  if (!c) {
    const here = { x: MARGIN, y: 1, label: fit(t.now || ' ', Math.max(1, width - MARGIN - 2)) }
    return { lines, room, tabEnd: Math.min(strip.length + 1, width - 1), scrolls: false, c: here, u: blank(MARGIN, 0), d: blank(MARGIN, 2), l: blank(0, 1), r: blank(width - 1, 1), find: findSpot, filter: filterSpot, sort: sortSpot, router: routerSpot }
  }
  const at = c.rows.length ? c.at : 0
  const here = spot(col, at)
  const u = c.rows.length && at - 1 >= tops[col]! ? spot(col, at - 1) : { x: x[col]!, y: first(col) - 1, label: title(col) }
  const below = at + 1 < Math.min(c.rows.length, tops[col]! + room)
  const d = below ? spot(col, at + 1) : { x: x[col]!, y: here.y + 1, label: here.y + 1 === first(col) + room ? more(col) : fit('', w[col]!) }
  const side = (ci: number) => spot(ci, t.cols[ci]!.rows.length ? t.cols[ci]!.at : 0)
  const l = wrapped ? blank(Math.max(0, here.x - 1), here.y) : col > 0 ? side(col - 1) : blank(0, here.y)
  const r = wrapped ? blank(here.x + w[col]!, here.y) : col < t.cols.length - 1 ? side(col + 1) : blank(x[col]! + w[col]! + 1, here.y)
  const cur = c.rows[at]
  const scrolls = !!cur && `${cur.label ?? cur.value}${cur.value === c.current ? ' ✔' : ''}`.length > w[col]! - 2
  return { lines, room, tabEnd: Math.min(strip.length + 1, width - 1), scrolls, c: here, u, d, l, r, find: findSpot, filter: filterSpot, sort: sortSpot, router: routerSpot }
}

async function warm($: EngineInterface) {
  const all = await loadProfiles($)
  await loadProviders($)
  for (const p of all) void models($, p.name, p.provider).catch(() => undefined)
}

const dollars = (n: string, k: string) => Number(n.replace(/,/g, '')) * (k ? 1000 : 1)

/** The week's figures (`7d $1,491/$13.6k`, `7d $3.10 4%`) dim, in the colour of the share of the week used. */
export function weekly(Text: any, rest: string) {
  const out: any[] = []
  const week = /7d \$([\d,.]+)(k?)(?:\/\$([\d,.]+)(k?)| (\d+)%)/g
  let at = 0
  for (let m = week.exec(rest); m; m = week.exec(rest)) {
    const pct = m[5] !== undefined ? Number(m[5]) : dollars(m[1]!, m[2]!) * 100 / dollars(m[3]!, m[4]!)
    out.push(rest.slice(at, m.index), <Text dimColor color={costColor(pct)}>{m[0]}</Text>)
    at = m.index + m[0].length
  }
  out.push(rest.slice(at))
  return out
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    void warm($).catch(() => undefined)
    // The session's API-price cost (main, subagents), drawn at the end of the mode row under the prompt.
    const showCost = async () => {
      const r = await py($, ['-B', '-c', RUN, COST, '--cost', await $.session.id()], { timeoutMs: 30000 })
      update($, cost, () => r.stdout.trim())
    }
    void showCost().catch(() => undefined)
    $.clock.every(10000, () => showCost().catch(() => undefined))
    return next(e)
  })

  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    // `≈ $2.70 (7d $1,491/$13.6k Ultra)`: the session's API-price cost, coloured by size, then the week's
    const part = /^(.*?)≈ (\$([\d,.]+))(.*)$/.exec(await read($, cost))
    // The shift+tab reminder is dropped always, cost or not: the person knows the key. The mode badge stays.
    const reminder = /\s*·?\s*\(?shift\+tab to cycle\)?/i
    const raw = String(e.props.hint ?? '')
    if (!reminder.test(raw)) {
      // Keep the engine's own line so its pills (shells, monitors) stay live and coloured; the cost rides
      // as `tail`, which the engine always draws dim after its own ` · `, so the total loses its colour here.
      return part ? next({ ...e, props: { ...e.props, tail: part[0].trim() } }) : next(e)
    }
    // The engine's own line fills its row (a tree beside it wraps below) and its `tail` is always dim, so the
    // row is drawn here from its text: the hint dim, then the total coloured by how much it is.
    // One Text with inline children, cut at the edge: side-by-side boxes each wrapped on their own and split the row.
    const { Text } = $.ui.resolve(e)
    const hint = raw.replace(reminder, '').trim()
    if (!part) return <Text dimColor wrap="truncate-end">{hint}</Text>
    return (
      <Text dimColor wrap="truncate-end">
        {`${hint ? hint + ' · ' : ''}${part[1]}≈ `}
        <Text color={costColor(Number(part[3]!.replace(/,/g, '')))}>{part[2]}</Text>
        {weekly(Text, part[4]!)}
      </Text>
    )
  })

  // The grid's ring never leaves the cursor cell: a move onto one of its targets is the key that target
  // stands for, and the ring stays put.
  on('ui.focus', { requestId: GRID_PANE }, async ($, e, next) => {
    if (e.origin.kind !== 'person') return next(e)
    const key = RING_KEYS[e.element ?? '']
    if (key) void onGridKey($, key[0], key[1]).catch(err => $.ui.toast(String(err)))
    return { deny: 'the grid moves its own cursor' }
  })

  // Esc clears a typed search before it closes the picker, as a password field's first Esc does.
  on('ui.close', { id: GRID_PANE }, async ($, e, next) => {
    if (e.origin.kind === 'person' && viewQuery) {
      await setQuery($, '')
      return { deny: 'cleared the search' }
    }
    return next(e)
  })

  // ↑/↓ scroll a pane taller than its body instead of moving the ring (the grid is drawn so): a
  // one-row scroll is the cursor's move within its column, and the window stays put. ←/→ are bound
  // to pane:pageUp/pageDown in ~/.claude/keybindings.json, so a page-sized scroll is a column move, and
  // Space to pane:bottom, whose scroll is the whole tree: a pick (a Button would take Space as Enter).
  on('ui.scroll', { requestId: GRID_PANE }, async ($, e, next) => {
    if (e.origin.kind !== 'person') return next(e)
    const key = e.pointer ? (e.by < 0 ? 'up' : 'down') : e.by > e.bodyRows ? 'space'
      : Math.abs(e.by) > 1 ? (e.by < 0 ? 'left' : 'right') : e.by < 0 ? 'up' : 'down'
    if (e.by) void onGridKey($, key, false).catch(err => $.ui.toast(String(err)))
    return { deny: 'the grid moves its own cursor' }
  })

  on('ui.render', { component: 'Pane', requestId: GRID_PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Text, Button } = ui
    // Mobile has no Input; the other surfaces do, so draw the field only where it exists.
    const Input = 'Input' in ui ? ui.Input : undefined
    const g = (await read($, grid)) as Grid | null
    if (!g) return <Text dimColor>Loading…</Text>
    const L = gridLayout(g, Math.max(20, e.props.bodyColumns), Math.max(3, Math.min(12, e.props.scroll.bodyRows - 9)), marquee)
    marqueeWanted = L.scrolls
    if (L.room !== g.room) void update($, grid, x => (x ? { ...x, room: L.room } : x))
    const at = (k: string, x: number, y: number, label: string, dim = false) => {
      // a click on a target does what its key does; Enter on the cursor cell sets
      const [key, shift] = k === 'C' ? ['return', false] : RING_KEYS[k] ?? ['', false]
      return (
      // one row exactly the cell's width: a label the layout squeezes would wrap onto the row below
      <Box key={`at:${k}`} position="absolute" left={x} top={y} width={Math.max(1, label.length)} height={1} flexShrink={0} overflow="hidden">
        <Button key={k} plain dimColor={dim || undefined} autoFocus={k === 'C' ? true : undefined}
          onPress={() => void onGridKey($, key, shift).catch(err => $.ui.toast(String(err)))}>
          {label}
        </Button>
      </Box>
      )
    }
    // A one-letter control: pressed from the grid, or clicked, it runs its key.
    const hot = (k: string, x: number, y: number, label: string, letter: string) => {
      const [key, shift] = RING_KEYS[k] ?? ['', false]
      return (
        <Box key={`at:${k}`} position="absolute" left={x} top={y} width={Math.max(1, label.length + 4)} height={1} flexShrink={0} overflow="hidden">
          <Button key={k} plain dimColor hotkey={letter}
            onPress={() => void onGridKey($, key, shift).catch(err => $.ui.toast(String(err)))}>
            {label}
          </Button>
        </Box>
      )
    }
    return (
      <Box flexDirection="column">
        {L.lines.map((l, i) => <Text key={`l:${i}`} {...l.style} wrap="truncate-end">{l.text || ' '}</Text>)}
        {/* taller than the body, so the engine turns ↑/↓ into scrolls, which say their direction */}
        <Box key="overflow" height={e.props.scroll.bodyRows + 2} />
        {at('T-', 0, 0, '‹')}
        {at('C', L.c.x, L.c.y, L.c.label)}
        {at('T+', L.tabEnd, 0, '›')}
        {at('U', L.u.x, L.u.y, L.u.label)}
        {at('D', L.d.x, L.d.y, L.d.label)}
        {at('L', L.l.x, L.l.y, L.l.label)}
        {at('R', L.r.x, L.r.y, L.r.label)}
        {hot('F', L.find.x, L.find.y, L.find.label, 'f')}
        {hot('S', L.sort.x, L.sort.y, L.sort.label, 's')}
        {L.router.label && hot('X', L.router.x, L.router.y, L.router.label, 'r')}
        {Input !== undefined && (
          <Box key="at:Q" position="absolute" left={L.filter.x} top={L.filter.y} width={L.filter.width} height={1} flexShrink={0} overflow="hidden">
            <Input key="Q" placeholder="search models" value={g.query ?? ''}
              onInput={value => void setQuery($, value).catch(err => $.ui.toast(String(err)))}
              onSubmit={() => void focusFind($)} />
          </Box>
        )}
      </Box>
    )
  })

  // command.run is the person's own command, so its pane is placed at any terminal width (an
  // expansion hook's open counts as unasked and waits below 144 columns). Answering here runs no
  // command; next(e) runs the built-in /model, or expands the others for ccx-switch.py's settings hook.
  // /model and /fallback with no arguments draw a grid in their own chat row instead of a pane.
  // The main session's Compact pick: compact once the context reaches that share of the window.
  let compacting = false
  on('session.measure', async ($, e, next) => {
    const out = await next(e)
    const pick = String((await mainCompact($)) ?? '')
    const tokens = compactTokens(pick)
    const pct = tokens ? tokens / e.context.window * 100 : Number(pick)
    const used = e.context.percent ?? (e.context.tokens ?? 0) / e.context.window * 100
    if (pct && !compacting && used >= pct) {
      compacting = true
      void $.session.compact().catch(() => undefined).finally(() => (compacting = false))
    }
    return out
  })

  for (const name of ['model', 'fallback']) on('command.run', { command: name }, async ($, e, next) => {
    const command = e.command
    const words = e.args.split(/\s+/).filter(Boolean)
    const head = words[0]?.toLowerCase()
    // `/model router ...`: the router from the prompt (switch, best picks, ranges, a level's model)
    if (command === 'model' && head === 'router') {
      const args = routerArgs(words.slice(1))
      if (!args) return { text: ROUTER_USAGE }
      const { ok, out } = await ccx($, args)
      return { text: (ok ? '' : 'Failed: ') + (out.trim() || (ok ? 'Done.' : ROUTER_USAGE)) }
    }
    // `/model <name>` stays Claude Code's; a complete /fallback call stays with ccx-switch.py.
    const dialog = command === 'model' ? !words.length : !words.length || (head === 'subagent' && words.length <= 2)
    if (!dialog) return next(e)
    // The picker is its own window (ccx gui) on a ccx adapter session; the in-terminal grid stays as
    // the fallback when the window cannot open (no Tk, a missing file, a refused launch). A native
    // session keeps the grid: only it can run Claude Code's own /model and /effort, which its Model
    // tab switches; the window would refuse its Apply.
    let window: { ok: boolean; out: string } | undefined
    if ((await ccx($, ['route'])).ok) {
      window = await ccx($, ['gui', command === 'model' ? 'model' : 'fallback'])
      if (window.ok) return { text: window.out.split(/\r?\n/).pop() || `/${command}: opened the picker window.` }
    }
    const failed = await openGrid($, command === 'model' ? 'Model' : 'Fallback', words[1])
    if (failed) {
      // the built-in /model, or ccx-switch.py's settings hook, answers instead
      void $.ui.close({ id: GRID_PANE })
      return next(e)
    }
    void takeGridKeys($).catch(() => undefined)
    const reason = window ? window.out.split(/\r?\n/).pop() : 'this session has no ccx launch'
    return { text: `/${command}: the picker window is unavailable (${reason ?? 'ccx gui failed'}); Tab switches tabs, arrows move, Space picks, Enter sets, Esc closes.` }
  })
}
