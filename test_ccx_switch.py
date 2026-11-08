"""Offline regression check: python -B ~/.config/ccx/test_ccx_switch.py."""
import contextlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import sys

if os.name != 'nt':  # these checks drive the PowerShell launcher; test_ccx_posix.py covers bash/zsh
    if 'pytest' in sys.modules:
        import pytest
        pytest.skip('PowerShell launcher checks', allow_module_level=True)
    print('SKIP: test_ccx_switch.py needs Windows PowerShell; run test_ccx_posix.py.')
    raise SystemExit(0)

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('ccx_switch', root / 'ccx-switch.py')
ccx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccx)
# Never write the real effort cache: the fake catalogs below would replace it for a day.
ccx.EFFORTS_FILE = os.path.join(tempfile.mkdtemp(), 'efforts.json')
# Tests that set no routes file of their own once wrote helper rows into the real ~/.config/ccx/routes.json.
ccx.ROUTES_FILE = os.path.join(tempfile.mkdtemp(), 'routes.json')
# Each check below fetches with its own mocks: no shared model cache (and never the real one).
ccx.MODELS_CACHE_FILE = os.path.join(tempfile.mkdtemp(), 'models-cache.json')
ccx.MODELS_TTL = 0
# Keep real credentials out of mock failure output, and make runs inside Claude Code match plain runs.
for _name in [k for k in os.environ if k == 'CLAUDECODE' or re.search('KEY|TOKEN|SECRET|PASSWORD', k.upper())]:
    del os.environ[_name]

import ast
for filename in ('test_ccx_switch.py', 'test_ccx_resume.py', 'critic_mock_check.py'):
    for node in ast.walk(ast.parse((root / filename).read_text(encoding='utf-8'))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'
                and node.func.attr in {'run', 'Popen'}):
            assert any(k.arg == 'creationflags' or k.arg is None for k in node.keywords), f'{filename}:{node.lineno}: test command must be hidden'

with patch.object(ccx.subprocess, 'run') as launch:
    launch.return_value.stdout = ('TCP 127.0.0.1:9999 0.0.0.0:0 LISTENING 12345' if os.name == 'nt' else '')
    ccx.stop_port_owner(9999)
    assert launch.call_count == (2 if os.name == 'nt' else 1)  # netstat + taskkill; lsof elsewhere
    assert os.name != 'nt' or all(call.kwargs.get('creationflags') == getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                  for call in launch.call_args_list), 'background netstat/taskkill must be hidden'

with tempfile.TemporaryDirectory(prefix='ccx-check-') as directory:
    temp = Path(directory)
    ccx.APPLY_FILE = str(temp / 'apply.ps1')
    ccx.STATE_FILE = str(temp / 'state.json')
    ccx.OPENCODE_AUTH = str(temp / 'auth.json')
    Path(ccx.OPENCODE_AUTH).write_text(json.dumps({'opencode': {'type': 'api', 'key': 'unrelated-key'}}))
    with patch.dict(os.environ, {}, clear=True), patch.object(ccx, 'user_env', return_value=''):
        assert ccx.auth_key('openrouter') == '', 'Must not leak an unrelated provider key'
        assert ccx.auth_key('nvidia') == ''
        assert ccx.auth_key('opencode-go') == ''
        assert ccx.auth_key('opencode') == 'unrelated-key'
    with patch.dict(os.environ, {'CMD_API_KEY': 'command-test', 'OPENROUTER_API_KEY': 'router-test'}):
        assert ccx.auth_key('commandcode') == 'command-test'
        assert ccx.auth_key('openrouter') == 'router-test'
    with patch.object(ccx, 'fetch', return_value={'data': [
        {'id': 'claude-sonnet-4-6', 'supported_endpoints': ['/messages']},
        {'id': 'openai/gpt-5.3-codex', 'supported_endpoints': ['/responses']},
        {'id': 'gpt-6-sol', 'supported_endpoints': ['/chat/completions', '/responses']},
    ]}), patch.object(ccx, 'auth_key', return_value='test'):
        assert [m['id'] for m in ccx.get_models('commandcode')] == ['claude-sonnet-4-6', 'gpt-6-sol'], 'Only models Claude Code can reach are listed'
    with patch.object(ccx, 'fetch', side_effect=OSError('offline')), contextlib.redirect_stderr(io.StringIO()):
        assert ccx.get_models('commandcode')[0]['id'] == 'claude-opus-5-5'
    # Command Code lists models the plan (and route) cannot use; the menus keep only the usable ones.
    client_catalog = {'data': [
        {'id': 'allowed/go', 'minimumPlan': 'individual-go', 'category': 'opensource', 'reasoningEfforts': ['low', 'medium']},
        {'id': 'allowed/goat', 'minimumPlan': 'individual-goat', 'category': 'opensource'},
        {'id': 'locked/pro', 'minimumPlan': 'individual-pro', 'category': 'premium'},
        {'id': 'hidden/free', 'minimumPlan': 'individual-go', 'category': 'opensource', 'hidden': True},
        {'id': 'ultra/only', 'minimumPlan': None, 'category': 'premium'},
        {'id': 'premium/noplan', 'category': 'premium'},
        {'id': 'opensource/noplan', 'category': 'opensource'},
        {'id': 'future/opensource', 'minimumPlan': 'individual-mega', 'category': 'opensource'},
        {'id': 'future/premium', 'minimumPlan': 'individual-mega', 'category': 'premium'},
    ]}
    plain_catalog = {'data': [
        {'id': 'allowed/go', 'supported_endpoints': ['/messages']},
        {'id': 'allowed/goat', 'supported_endpoints': ['/chat/completions']},
        {'id': 'locked/pro', 'supported_endpoints': ['/messages']},
    ]}
    def account(plan='individual-goat', status='active', bought=0):
        def fetch(url, headers=None, timeout=15):
            if url.endswith('/subscriptions'):
                return {'data': {'planId': plan, 'status': status}}
            if url.endswith('/credits'):
                return {'credits': {'monthlyCredits': 5, 'purchasedCredits': bought, 'freeCredits': 0}}
            return client_catalog if 'view=client' in url else plain_catalog
        return fetch
    cli_ids = {m['id'] for m in client_catalog['data'] if not m.get('hidden')}
    def listed(provider, **account_args):
        ccx._PLAN_CACHE.clear()
        ccx._CATALOG_CACHE.clear()
        with patch.object(ccx, 'fetch', side_effect=account(**account_args)), patch.object(ccx, 'auth_key', return_value='test'), \
             patch.object(ccx.catalog, '_live_model_ids', return_value=cli_ids):
            return [m['id'] for m in ccx.get_models(provider)]
    assert listed('commandcode') == ['allowed/go', 'allowed/goat'], 'Plan-locked models are hidden'
    # The CLI route applies the same plan; a missing or newer-than-known minimumPlan falls
    # back to the category, and a null one (cmdc's "locked for every plan") is never offered.
    assert listed('commandcode-cli') == ['allowed/go', 'allowed/goat', 'future/opensource', 'opensource/noplan']
    assert listed('commandcode', bought=1) == ['allowed/go', 'allowed/goat', 'locked/pro'], 'Purchased credits unlock every model'
    assert 'hidden/free' not in listed('commandcode-cli', bought=1), 'Hidden models stay hidden'
    assert 'ultra/only' in listed('commandcode-cli', bought=1), 'Credits unlock a null minimumPlan'
    assert listed('commandcode', plan='individual-new') == ['allowed/go', 'allowed/goat', 'locked/pro'], 'An unknown plan keeps every model'
    assert listed('commandcode', status='canceled') == ['allowed/go', 'allowed/goat', 'locked/pro'], 'Only an active subscription enforces its plan'
    # The billing catalog and the route catalogs spell ids differently; matching ignores case.
    def cased(url, headers=None, timeout=15):
        if url.endswith('/subscriptions'):
            return {'data': {'planId': 'individual-goat', 'status': 'active'}}
        if url.endswith('/credits'):
            return {'credits': {}}
        return ({'data': [{'id': 'Allowed/GO', 'minimumPlan': 'individual-go', 'category': 'opensource'},
                          {'id': 'LOCKED/PRO', 'minimumPlan': 'individual-pro', 'category': 'premium'}]}
                if 'view=client' in url else
                {'data': [{'id': 'allowed/go', 'supported_endpoints': ['/messages']},
                          {'id': 'locked/pro', 'supported_endpoints': ['/messages']}]})
    ccx._PLAN_CACHE.clear()
    ccx._CATALOG_CACHE.clear()
    with patch.object(ccx, 'fetch', side_effect=cased), patch.object(ccx, 'auth_key', return_value='test'):
        assert [m['id'] for m in ccx.get_models('commandcode')] == ['allowed/go'], 'Plan ids match regardless of case'
    # The /model picker offers the live usable models, not only stale fallbacks.
    live = [{'id': 'claude-opus-5-5'}, {'id': 'claude-sonnet-5'}, {'id': 'gpt-6-sol'}]
    with patch.object(ccx, 'load_state', return_value={}):
        picks = ccx.picker_models('commandcode', 'claude-opus-5-5', live)
    assert picks[0] == 'claude-sonnet-5', 'A live model fills the first /model slot'
    assert 'gpt-6-sol' not in picks, 'The other Command Code route stays out of the picker'
    # Remembered picks that the plan no longer allows (or that are retired) stay out.
    with patch.object(ccx, 'load_state', return_value={'recent': {'commandcode-cli': ['google/gemini-4-argon', 'Xiaomi/MIMO-V2.6-PRO']}}):
        picks = ccx.picker_models('commandcode-cli', 'xiaomi/mimo-v2.6-pro',
                                 [{'id': 'xiaomi/mimo-v2.6-pro'}, {'id': 'deepseek/deepseek-v4-pro'}])
    assert picks == ['deepseek/deepseek-v4-pro'], 'Stale plan-locked picks stay out of /model'
    # Reasoning-effort support comes from the same catalog (MiMo publishes none).
    ccx._CATALOG_CACHE.clear()
    with patch.object(ccx, 'fetch', side_effect=account()), patch.object(ccx, 'auth_key', return_value='test'):
        assert ccx.model_efforts('commandcode-cli', 'allowed/go') == ['low', 'medium']
        assert ccx.model_efforts('commandcode-cli', 'allowed/goat') == [], 'No published efforts means none'
        assert ccx.model_efforts('commandcode-cli', 'not/listed') is None, 'An unlisted model is unknown'
    def locked_only(url, headers=None, timeout=15):
        if url.endswith('/subscriptions'):
            return {'data': {'planId': 'individual-goat', 'status': 'active'}}
        if url.endswith('/credits'):
            return {'credits': {}}
        return client_catalog if 'view=client' in url else {'data': [{'id': 'locked/pro', 'supported_endpoints': ['/messages']}]}
    ccx._PLAN_CACHE.clear()
    with patch.object(ccx, 'fetch', side_effect=locked_only), patch.object(ccx, 'auth_key', return_value='test'):
        assert ccx.get_models('commandcode') == [], 'The offline fallback must not re-offer plan-locked models'

    (temp / 'models_cache.json').write_text(json.dumps({'models': [
        {'slug': 'gpt-6-astra', 'context_window': 272000, 'max_context_window': 872000},
        {'slug': 'gpt-5.5', 'context_window': 272000},
    ]}))
    with patch.dict(os.environ, {'CODEX_HOME': str(temp)}), patch.object(ccx, 'ensure_oauth_proxy'), patch.object(ccx, 'fetch', return_value={'data': [
        {'id': 'gpt-6-astra'}, {'id': 'gpt-5.5'}, {'id': 'unknown'},
        {'id': 'explicit', 'context_window': 400000},
    ]}):
        assert [m['context'] for m in ccx.get_models('openai-oauth')] == [872000, 272000, None, 400000]
        (temp / 'models_cache.json').write_text('invalid json')
        assert [m['context'] for m in ccx.get_models('openai-oauth')] == [None, None, None, 400000]
    router_models = [{'id': f'openai/model-{i}', 'context_length': 400000} for i in range(20)]
    with patch.object(ccx, 'fetch', return_value={'data': router_models}):
        assert len(ccx.get_models('openrouter')) == 20, 'Typed models beyond menu shortlist need limits too'

    fake_key = "test'$(throw 'must not execute')`secret"
    for provider, model in [('commandcode', 'claude-sonnet-4-6'), ('openrouter', 'openai/gpt-5.3-codex'), ('official', None), ('openai-oauth', 'gpt-6-astra'), ('nvidia', 'test/model'), ('openai-oauth', "model'$(throw 'must not execute')`id")]:
        context = 872000 if provider == 'openai-oauth' and model == 'gpt-6-astra' else 131072 if provider == 'nvidia' else None
        with patch.object(ccx, 'get_models', return_value=[{'id': model, 'context': context}]), patch.object(ccx, 'auth_key', return_value=fake_key), patch.object(ccx, 'ensure_oauth_proxy'), patch.object(ccx, 'ensure_anthropic_proxy'), patch.object(ccx.catalog, '_models_dev_facts', return_value={}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ccx.apply(provider, model)
        script = temp / 'check.ps1'
        script.write_text('''$ErrorActionPreference = 'Stop'
function claude {
    if (Test-Path -LiteralPath $env:CCX_TEST_APPLY) { throw 'Apply file must be removed before launch' }
    [pscustomobject]@{
        client = $(if ($script:codexLaunched) { 'codex' } else { 'claude' })
        base = $env:ANTHROPIC_BASE_URL
        key = $env:ANTHROPIC_API_KEY
        token = $env:ANTHROPIC_AUTH_TOKEN
        model = $env:ANTHROPIC_MODEL
        fable = $env:ANTHROPIC_DEFAULT_FABLE_MODEL
        haiku = $env:ANTHROPIC_DEFAULT_HAIKU_MODEL
        sonnet = $env:ANTHROPIC_DEFAULT_SONNET_MODEL
        sonnetName = $env:ANTHROPIC_DEFAULT_SONNET_MODEL_NAME
        connectors = $env:ENABLE_CLAUDEAI_MCP_SERVERS
        apiTimeout = $env:API_TIMEOUT_MS
        opusCapabilities = $env:ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES
        subagent = $env:CLAUDE_CODE_SUBAGENT_MODEL
        openai = $env:OPENAI_API_KEY
        openaiBase = $env:OPENAI_BASE_URL
        headers = $env:ANTHROPIC_CUSTOM_HEADERS
        launch = $env:CCX_LAUNCH_ID
        toolSearch = $env:ENABLE_TOOL_SEARCH
        autoModeServer = $env:CLAUDE_CODE_AUTO_MODE_SERVER
        gateway = $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY
        context = $env:CLAUDE_CODE_MAX_CONTEXT_TOKENS
        firstParty = $env:_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL
        launchArgs = @($args)
    } | ConvertTo-Json -Compress
}
function codex { $script:codexLaunched = $true; claude @args }
. $env:CCX_TEST_APPLY
''', encoding='utf-8')
        env = dict(os.environ, CCX_TEST_APPLY=ccx.APPLY_FILE)
        env.update({v: 'stale-value' for v in ccx.CCX_VARS + ccx.OAI_VARS})
        result = subprocess.run(['powershell', '-NoProfile', '-File', str(script)], env=env, capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout.splitlines()[-1])
        assert data['opusCapabilities'] == 'thinking,adaptive_thinking,interleaved_thinking,effort,xhigh_effort,max_effort' if provider == 'commandcode' else not data['opusCapabilities']
        assert data['headers'] == 'x-ccx-launch: ' + data['launch'] and len(data['launch']) == 32 if provider in (*ccx.FALLBACK_ROUTES, 'official') else not data['headers'], 'Adapter routes (and official, passed through) name their launch for /fallback and /model'
        assert data['firstParty'] == ('1' if provider == 'official' else None), 'Only official runs as first party; other routes clear it'
        assert data['toolSearch'] == 'false' if provider in ('openai-oauth', 'nvidia') else not data['toolSearch']
        assert data['autoModeServer'] == '0' if provider != 'official' else not data['autoModeServer']
        assert data['gateway'] == '1' if provider not in ('official', 'commandcode') else not data['gateway'], 'The /model picker sees the gateway list'
        assert data['connectors'] == 'false' if provider != 'official' else not data['connectors']
        assert data['apiTimeout'] == '1800000' if provider != 'official' else not data['apiTimeout']
        assert data['client'] == 'claude'
        assert data['context'] == str(context) if context else not data['context']
        assert not data['openai'] and not data['openaiBase']
        if provider == 'official':
            # the claude.ai login, passed through the adapter so /model can reroute it
            assert data['base'] == f'http://127.0.0.1:{ccx.ADAPTER_PORT}/official'
            assert all(not data[k] for k in ['key', 'token', 'model', 'fable', 'sonnet', 'subagent'])
            assert data['launchArgs'] == []
        else:
            assert data['base'] == (f'http://127.0.0.1:{ccx.ADAPTER_PORT}/{provider}' if provider in ('openai-oauth', 'nvidia') else ccx.PROVIDERS[provider]['base'])
            assert data['model'] == data['haiku'] == data['subagent'] == model
            # /model offers the provider's other models; Command Code keeps Claude ids on their route.
            picks = ccx.picker_models(provider, model, [{'id': model, 'context': context}])
            assert data['sonnet'] == data['sonnetName'] == (picks[0] if picks else None) and model not in picks
            assert provider != 'commandcode' or all(m.startswith('claude-') for m in picks)
            assert data['launchArgs'] == ['--model', model]
            # A bearer token avoids Claude's claude.ai/ANTHROPIC_API_KEY conflict warning.
            assert data['token'] == ('openai-oauth' if provider == 'openai-oauth' else fake_key) and not data['key']

    for provider, model, key in [('commandcode', '', 'test'), ('commandcode', 'claude-sonnet-4-6', ''), ('openrouter', 'openai/gpt-5.3-codex', '')]:
        with patch.object(ccx, 'auth_key', return_value=key), contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.apply(provider, model)
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError('Invalid input accepted')
        assert not Path(ccx.APPLY_FILE).exists()
    for provider, model in [('commandcode', 'claude-sonnet-4-6'), ('official', None), ('openai-oauth', 'gpt-6-astra'), ('nvidia', 'test/model')]:
        with patch.object(ccx, 'get_models', return_value=[{'id': model, 'context': 872000}]), patch.object(ccx, 'auth_key', return_value='test'), patch.object(ccx, 'ensure_oauth_proxy'), patch.object(ccx, 'ensure_anthropic_proxy'), contextlib.redirect_stdout(io.StringIO()):
            ccx.apply(provider, model, no_run=True)
        generated = Path(ccx.APPLY_FILE).read_text()
        assert '\nclaude' not in generated and '\ncodex' not in generated
        assert ("Set-Item 'Env:CLAUDE_CODE_MAX_CONTEXT_TOKENS' '872000'" in generated) == (provider != 'official')
        script.write_text("$ErrorActionPreference = 'Stop'\nfunction claude { throw 'Unexpected launch' }\nfunction codex { throw 'Unexpected launch' }\n. $env:CCX_TEST_APPLY\n", encoding='utf-8')
        subprocess.run(['powershell', '-NoProfile', '-File', str(script)], env=env, capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for invalid_context in [None, True, '1000000', -1, 0, 1.5]:
        with patch.object(ccx, 'get_models', return_value=[{'id': 'test/model', 'context': invalid_context}]), patch.object(ccx, 'auth_key', return_value='test'), patch.object(ccx, 'ensure_anthropic_proxy'), patch.object(ccx.catalog, '_models_dev_facts', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            ccx.apply('nvidia', 'test/model', no_run=True)
        assert "Set-Item 'Env:CLAUDE_CODE_MAX_CONTEXT_TOKENS'" not in Path(ccx.APPLY_FILE).read_text()
    with patch.object(ccx, 'get_models', return_value=[]), patch.object(ccx, 'auth_key', return_value='test'), patch.object(ccx, 'ensure_anthropic_proxy'), patch.object(ccx.catalog, '_models_dev_facts', return_value={}), contextlib.redirect_stdout(io.StringIO()):
        ccx.apply('nvidia', 'test/model', effort='xhigh')
        generated = Path(ccx.APPLY_FILE).read_text()
        assert "'--effort' 'xhigh'" in generated and "Set-Item 'Env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT' '1'" in generated
        assert ccx.load_state()['effort'] == 'xhigh'
        ccx.apply('official')
        assert "Set-Item 'Env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT'" not in Path(ccx.APPLY_FILE).read_text()
assert ccx.parse_pct('100k') == '100k' and ccx.parse_pct('80%') == 80 and ccx.parse_pct('inherit') == ''
assert ccx.compact_tokens('1.5m') == 1_500_000 and ccx.compact_text('100k') == '100k' and ccx.compact_text(None) == 'inherit'
_env, _args = {'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '90'}, []
ccx.compact_launch(_env, _args, '100k')
assert _args == ['--settings', '{"autoCompactWindow": 100000}'] and _env == {'CCX_AUTOCOMPACT': '100k'}, 'A token window replaces an inherited percentage'
print('PASS: context limits and reset, OAuth metadata, credential isolation, model filtering, offline fallback, PowerShell routing/quoting/cleanup, OAuth and NVIDIA Claude launch, official reset, invalid input, NoRun.')

with tempfile.TemporaryDirectory(prefix='ccx-entry-check-') as directory:
    result = subprocess.run(['powershell', '-NoProfile', '-File', str(root / 'ccx-switch.ps1'), 'invalid-provider'], env=dict(os.environ, USERPROFILE=directory), capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
assert result.returncode != 0, 'Invalid providers must return failure from the PowerShell entry point'
print('PASS: PowerShell entry point propagates errors.')

with tempfile.TemporaryDirectory(prefix='ccx-agents-check-') as directory:
    temp = Path(directory)
    ccx.AGENTS_FILE, ccx.AGENTS_DIR, ccx.STATE_FILE = str(temp / 'agents.json'), str(temp / 'agents'), str(temp / 'state.json')
    Path(ccx.AGENTS_FILE).write_text(json.dumps({
        'cheap': {'provider': 'nvidia', 'model': 'test/model', 'effort': 'low', 'autocompact': 60,
                  'args': ['--permission-mode', 'auto'], 'description': 'Bulk edits.'},
        'plain': {}}))
    # Direct terminal calls may start fresh; in-Claude relays require a context envelope.
    parent = {'ANTHROPIC_BASE_URL': 'https://parent', 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '90', 'CLAUDECODE': '', 'KEEP': 'yes'}
    for agent, autocompact, command in [
            ('cheap', '60', ['claude', '-p', '--strict-mcp-config', '--mcp-config', ccx.worker_mcp(), '--disallowedTools', ccx.WORKER_UNUSED_TOOLS, '--append-system-prompt', ccx.WORKER_RULES, '--model', 'test/model', '--effort', 'low', '--permission-mode', 'auto']),
            ('plain', '90', ['claude', '-p', '--strict-mcp-config', '--mcp-config', ccx.worker_mcp(), '--disallowedTools', ccx.WORKER_UNUSED_TOOLS, '--append-system-prompt', ccx.WORKER_RULES])]:
        with patch.dict(os.environ, parent), patch.object(ccx.subprocess, 'run') as launch, patch.object(ccx, 'get_models', return_value=[{'id': 'test/model', 'context': 131072}]), patch.object(ccx, 'auth_key', return_value='test'), patch.object(ccx, 'ensure_anthropic_proxy'):
            launch.return_value.returncode = 3
            try:
                ccx.run_agent(agent, 'täsk')
            except SystemExit as exc:
                assert exc.code == 3, 'The child exit code must reach the relay'
            sent = launch.call_args
        assert sent.args[0] == command and sent.kwargs['input'] == 'täsk'.encode('utf-8')
        child = sent.kwargs['env']
        assert child.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE') == autocompact, 'A subagent threshold replaces the parent one; none inherits it'
        assert 'CLAUDECODE' not in child and child['KEEP'] == 'yes'
        assert child.get('ANTHROPIC_BASE_URL') == (f'http://127.0.0.1:{ccx.ADAPTER_PORT}/nvidia' if agent == 'cheap' else None)
        assert agent == 'plain' or child['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] == '131072'
    with contextlib.redirect_stdout(io.StringIO()):
        ccx.sync_agents()
    shim = (temp / 'agents' / 'ccx-cheap.md').read_text(encoding='utf-8')
    # Delegated runs must require the context envelope; direct CLI runs may start fresh.
    assert 'name: ccx-cheap' in shim and 'agent-relay --feed cheap <<' in shim and 'nvidia/test/model' not in shim
    assert 'including its leading CCX_CONTEXT_V1 line' in shim
    for bad in [{'x': {'provider': 'nope'}}, {'x': {'effort': 'huge'}}, {'x': {'autocompact': 0}}, {'x': {'autocompact': True}}, {'Bad Name': {}}, {'x': {'args': '--x'}}, {'x': {'model': 'bad model'}}, {'x': {'args': ['ok', 1]}}]:
        Path(ccx.AGENTS_FILE).write_text(json.dumps(bad))
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.load_agents()
            except SystemExit:
                pass
            else:
                raise AssertionError(f'Invalid subagent profile accepted: {bad}')
    Path(ccx.AGENTS_FILE).write_text(json.dumps({'cheap': {'provider': 'nvidia', 'model': 'test/model', 'autocompact': 60}}))
    with patch.object(ccx.sys, 'argv', ['ccx', 'agents', 'set', 'rev', '--provider', 'official', '--effort', 'MAX', '--autocompact=75']), contextlib.redirect_stdout(io.StringIO()):
        ccx.main()
    with patch.object(ccx.sys, 'argv', ['ccx', 'agents', 'set', 'cheap', '--autocompact', 'default', '--effort', 'low']), contextlib.redirect_stdout(io.StringIO()):
        ccx.main()
    assert ccx.load_agents() == {'cheap': {'provider': 'nvidia', 'model': 'test/model', 'effort': 'low'},
                                'rev': {'provider': 'official', 'effort': 'max', 'autocompact': 75}}
    with patch.object(ccx.sys, 'argv', ['ccx', 'agents', 'remove', 'rev']), contextlib.redirect_stdout(io.StringIO()):
        ccx.main()
    assert list(ccx.load_agents()) == ['cheap'] and not (temp / 'agents' / 'ccx-rev.md').exists(), 'Removed profiles lose their relay'
    with patch.object(ccx, 'get_models', return_value=[{'id': 'test/model'}, {'id': 'other/model'}]), contextlib.redirect_stdout(io.StringIO()) as models_out:
        ccx.agent_models('cheap')
    text = models_out.getvalue()
    assert 'nvidia/test/model' in text and 'effort' in text and '* test/model' in text and 'other/model' in text, 'The model list marks the profile pick'
    with contextlib.redirect_stdout(io.StringIO()) as profiles_out:
        ccx.agent_models()
    assert 'cheap:' in profiles_out.getvalue(), 'With no name the profiles are listed'
    with patch.object(ccx, 'load_agents', return_value={'cheap': {'provider': 'nvidia', 'model': 'Test/MODEL'}}), \
         patch.object(ccx, 'get_models', return_value=[{'id': 'test/model'}]), contextlib.redirect_stdout(io.StringIO()) as cased_out:
        ccx.agent_models('cheap')
    assert '* test/model' in cased_out.getvalue(), 'The current-model marker ignores id casing'
    # A model that publishes no reasoning efforts is run without --effort (MiMo rejects it).
    for efforts, expected in (([], ['claude', '-p', '--strict-mcp-config', '--mcp-config', ccx.worker_mcp(), '--disallowedTools', ccx.WORKER_UNUSED_TOOLS, '--append-system-prompt', ccx.WORKER_RULES, '--model', 'test/model']),
                              (['low'], ['claude', '-p', '--strict-mcp-config', '--mcp-config', ccx.worker_mcp(), '--disallowedTools', ccx.WORKER_UNUSED_TOOLS, '--append-system-prompt', ccx.WORKER_RULES, '--model', 'test/model', '--effort', 'low'])):
        with patch.object(ccx, 'route_env', return_value={}), patch.object(ccx, 'model_efforts', return_value=efforts), patch.object(ccx, 'parent_compact', return_value=None), \
             patch.object(ccx.subprocess, 'run') as launch, contextlib.redirect_stderr(io.StringIO()):
            launch.return_value.returncode = 0
            with patch.object(ccx.sys, 'argv', ['ccx', 'agent', 'cheap', 'fix']):
                try:
                    ccx.main()
                except SystemExit:
                    pass
            assert launch.call_args.args[0] == expected, f'effort support {efforts}'
    with patch.object(ccx.sys, 'argv', ['ccx', 'agent', 'cheap', '--provider', 'official', '--autocompact', '40', 'fix', '--model', 'x']), patch.object(ccx.subprocess, 'run') as launch:
        launch.return_value.returncode = 0
        try:
            ccx.main()
        except SystemExit:
            pass
    assert launch.call_args.args[0] == ['claude', '-p', '--strict-mcp-config', '--mcp-config', ccx.worker_mcp(), '--disallowedTools', ccx.WORKER_UNUSED_TOOLS, '--append-system-prompt', ccx.WORKER_RULES, '--effort', 'low'], 'A provider override drops the other provider model'
    assert launch.call_args.kwargs['input'] == b'fix --model x', 'Only leading options are options; the task stays whole'
    assert launch.call_args.kwargs['env']['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'] == '40'
    assert ccx.load_agents()['cheap'].get('autocompact') is None, 'Run options do not change the profile'
    ccx.APPLY_FILE = str(temp / 'apply.ps1')
    for argv, expected in [(['ccx', '-NoRun', 'official', 'claude-opus-5-5', '--autocompact', '70'], "Set-Item 'Env:CLAUDE_AUTOCOMPACT_PCT_OVERRIDE' '70'"),
                           (['ccx', '-NoRun', 'official', 'claude-opus-5-5', '--autocompact=default'], "Remove-Item \"Env:CLAUDE_AUTOCOMPACT_PCT_OVERRIDE\"")]:
        with patch.object(ccx.sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            ccx.main()
        assert expected in Path(ccx.APPLY_FILE).read_text()
    assert ccx.load_state()['autocompact'] == ''
    for bad in ['0', '101', 'x', '5.5']:
        with patch.object(ccx.sys, 'argv', ['ccx', '-NoRun', 'official', '--autocompact', bad]), contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.main()
            except SystemExit:
                pass
            else:
                raise AssertionError(f'Invalid auto-compact accepted: {bad}')
print('PASS: subagent profiles route, effort, autocompact override, env isolation, relay files, validation, set/remove/run options, main --autocompact.')

# A rejected key from opencode's auth.json is renewed once per process; other keys are never touched.
import opencode_key
ccx._OPENCODE_CHECKED.clear()
with patch.object(ccx, 'auth_key', return_value='stored'), patch.object(opencode_key, 'stored_key', return_value='stored'), \
        patch.object(opencode_key, 'key_ok', return_value=False), patch.object(opencode_key, 'renew', return_value='stored') as renew, \
        contextlib.redirect_stderr(io.StringIO()):
    ccx.ensure_key('opencode-go')
    ccx.ensure_key('opencode-go')
    assert renew.call_count == 1
ccx._OPENCODE_CHECKED.clear()
with patch.object(ccx, 'auth_key', return_value='env-key'), patch.object(opencode_key, 'stored_key', return_value='stored'), \
        patch.object(opencode_key, 'key_ok') as ok, patch.object(opencode_key, 'renew') as renew:
    ccx.ensure_key('opencode-go')
    assert not ok.called and not renew.called
assert opencode_key.KEY_RE.search('Copy oc_sk_abc123def456_' + 'x' * 30 + ' Copy')
print('opencode key renewal ok')

# OpenCode Go: /messages models go direct, chat-only ones (MiMo, GLM, ...) through the adapter.
dev = {'opencode-go': {'models': {'mimo-v2.6-pro': {'provider': {'api': 'x'}},
                                  'minimax-m3': {'provider': {'npm': '@ai-sdk/anthropic'}}}}}
with patch.object(ccx.catalog, '_models_dev', return_value=dev), patch.object(ccx, 'auth_key', return_value='test'), \
        patch.object(ccx, 'get_models', return_value=[]), patch.object(ccx, 'ensure_anthropic_proxy'), \
        contextlib.redirect_stderr(io.StringIO()):
    assert ccx.route_env('opencode-go', 'mimo-v2.6-pro')['ANTHROPIC_BASE_URL'].endswith('/opencode-go')
    direct = ccx.route_env('opencode-go', 'minimax-m3')
    assert direct['ANTHROPIC_BASE_URL'] == 'https://opencode.ai/zen/go' and 'x-opencode-session' in direct['ANTHROPIC_CUSTOM_HEADERS']
    assert ccx.go_messages('qwen3.9-unlisted') and not ccx.go_messages('glm-9-unlisted')
print('opencode-go protocol split ok')

# The /model picker's presets: curated helper defaults first, the model this account picked last
# next, then the provider's first mirrored model. Each row carries provider, model and effort.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, 'helper-defaults.json').write_text(json.dumps({'auto': {
        'openai-oauth': {'model': 'gpt-6.1-terra', 'effort': 'low'},
        'nvidia': {'model': 'deepseek-ai/deepseek-v4.1-flash', 'effort': 'high'}}}), encoding='utf-8')
    with patch.object(ccx, 'CFG_DIR', tmp), \
            patch.object(ccx, 'load_state', return_value={'recent': {'official': ['claude-opus-5-5'], 'minimax': ['MiniMax-M2.7']}}), \
            patch.object(ccx, 'route_providers', return_value=['openai-oauth', 'nvidia', 'minimax', 'gemini']), \
            patch.object(ccx, 'on_adapter', return_value=False):
        rows = {r['provider']: r for r in ccx.preset_targets()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ccx.presets_command([])
    assert rows['official'] == {'provider': 'official', 'model': 'claude-opus-5-5', 'effort': None}
    assert rows['openai-oauth'] == {'provider': 'openai-oauth', 'model': 'gpt-6.1-terra', 'effort': 'low'}
    assert rows['nvidia']['effort'] == 'high'
    assert rows['minimax'] == {'provider': 'minimax', 'model': 'MiniMax-M2.7', 'effort': None}
    assert 'gemini' not in rows  # no default, no recent pick, no mirror to offer
    assert all(len(line.split('\t')) == 5 for line in out.getvalue().splitlines())
    assert out.getvalue().splitlines()[0].startswith('official\tclaude-opus-5-5\t')
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            ccx.presets_command(['extra'])
        except SystemExit:
            pass
        else:
            raise AssertionError('presets rejects extra words')
print('presets ok')

# A preset the person edits wins over the curated default, and resets back to it.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, 'helper-defaults.json').write_text(json.dumps({'auto': {
        'nvidia': {'model': 'deepseek-ai/old', 'effort': 'low'}}}), encoding='utf-8')
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx, 'load_state', return_value={'recent': {}}), \
            patch.object(ccx, 'route_providers', return_value=['nvidia']), \
            patch.object(ccx, 'on_adapter', return_value=True), contextlib.redirect_stdout(io.StringIO()):
        ccx.presets_command(['set', 'nvidia', 'deepseek-ai/new', 'high'])
        assert json.loads(Path(tmp, 'presets.json').read_text(encoding='utf-8'))['nvidia'] == {
            'model': 'deepseek-ai/new', 'effort': 'high'}
        rows = ccx.preset_targets()
        assert rows == [{'provider': 'nvidia', 'model': 'deepseek-ai/new', 'effort': 'high', 'custom': True}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ccx.presets_command([])
        assert out.getvalue().splitlines()[0].split('\t')[4].strip() == '1'
        # invalid provider, model or effort are refused and write nothing
        for args in (['set', 'nope', 'm'], ['set', 'nvidia', 'bad model'], ['set', 'nvidia', 'm', 'ultra']):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    ccx.presets_command(args)
                except SystemExit:
                    pass
                else:
                    raise AssertionError(args)
        assert json.loads(Path(tmp, 'presets.json').read_text(encoding='utf-8'))['nvidia']['model'] == 'deepseek-ai/new'
        ccx.presets_command(['reset', 'nvidia'])
        assert 'nvidia' not in json.loads(Path(tmp, 'presets.json').read_text(encoding='utf-8'))
        assert ccx.preset_targets() == [{'provider': 'nvidia', 'model': 'deepseek-ai/old', 'effort': 'low'}]
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.presets_command(['reset', 'nvidia'])
            except SystemExit:
                pass
            else:
                raise AssertionError('reset without a custom preset accepted')
print('preset edits ok')

# Bundles keep all five categories, preserve legacy main presets, and validate before writing.
with tempfile.TemporaryDirectory() as tmp:
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx, 'load_state', return_value={}), \
            patch.object(ccx, 'route_providers', return_value=['nvidia']), \
            patch.object(ccx, 'on_adapter', return_value=True):
        ccx.presets_command(['set', 'nvidia', 'deepseek-ai/main', 'high'])
        bundle = ccx.preset_bundle(ccx.preset_targets()[0])
        assert set(bundle) == {'model', 'subagent', 'fallback', 'auto', 'tools'}
        assert bundle['model']['model'] == 'deepseek-ai/main'
        assert bundle['subagent'] == bundle['model']
        assert bundle['fallback'] == {'provider': 'off'}
        bundle['auto'] = {'provider': 'nvidia', 'model': 'helper-model', 'effort': 'low'}
        ccx.presets_command(['set-bundle', 'nvidia', json.dumps(bundle)])
        assert ccx.preset_bundle(ccx.preset_targets()[0]) == bundle
        before = Path(tmp, 'presets.json').read_bytes()
        invalid = {**bundle, 'tools': {'provider': 'nope', 'model': 'm'}}
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.presets_command(['set-bundle', 'nvidia', json.dumps(invalid)])
            except SystemExit:
                pass
            else:
                raise AssertionError('invalid bundle accepted')
        assert Path(tmp, 'presets.json').read_bytes() == before
        ccx.presets_command(['set', 'nvidia', 'deepseek-ai/new'])
        assert ccx.preset_bundle(ccx.preset_targets()[0])['auto'] == bundle['auto']
        # a named preset lists beside the provider rows, keeps its own bundle, and deletes cleanly
        named = {**bundle, 'model': {'provider': 'nvidia', 'model': 'named-main'}}
        ccx.presets_command(['set-bundle', 'Fast', json.dumps(named)])
        row = next(r for r in ccx.preset_targets() if r.get('name') == 'fast')
        assert (row['provider'], row['model'], row['custom']) == ('nvidia', 'named-main', True)
        assert ccx.preset_bundle(row) == named
        assert ccx.preset_targets()[0]['model'] == 'deepseek-ai/new'  # the provider row is untouched
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.presets_command(['set-bundle', 'bad name!', json.dumps(named)])
            except SystemExit:
                pass
            else:
                raise AssertionError('invalid preset name accepted')
        ccx.presets_command(['reset', 'fast'])
        assert not any(r.get('name') == 'fast' for r in ccx.preset_targets())
print('preset bundles ok')

with tempfile.TemporaryDirectory() as tmp:
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx, 'model_efforts', return_value=['low', 'high']), \
            patch.object(ccx, 'route_providers', return_value=['nvidia']), contextlib.redirect_stdout(io.StringIO()):
        bundle = {key: {'provider': 'nvidia', 'model': 'model', 'effort': 'low'} for key in ccx.PRESET_CATEGORIES}
        for key, target in [('fallback', {'provider': 'official', 'model': 'model'}),
                            ('auto', {'provider': 'official', 'model': 'model'}),
                            ('tools', {'provider': 'nvidia', 'model': 'model', 'effort': 'max'})]:
            invalid = {**bundle, key: target}
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    ccx.presets_command(['set-bundle', 'nvidia', json.dumps(invalid)])
                except SystemExit:
                    pass
                else:
                    raise AssertionError(f'incompatible {key} accepted')
        assert not Path(tmp, 'presets.json').exists()
    with patch.dict(os.environ, {'CCX_LAUNCH_ID': 'test-official', 'CCX_MAIN_PROVIDER': 'official'}), \
            patch.object(ccx, 'on_adapter', return_value=True), patch.object(ccx, 'set_fallback') as clear, \
            patch.object(ccx, 'load_fallbacks', return_value={}), contextlib.redirect_stdout(io.StringIO()):
        ccx.fallback_command(['off'])
        clear.assert_called_once_with('test-official')
print('preset compatibility ok')

# Concurrent launches recheck under a startup lock, rather than killing each other's listener.
with tempfile.TemporaryDirectory() as tmp, patch.object(ccx, 'CFG_DIR', tmp):
    import threading
    entered, released, order = threading.Event(), threading.Event(), []
    def first():
        with ccx.adapter_start_lock():
            order.append('first')
            entered.set()
            released.wait(3)
    def second():
        entered.wait(3)
        with ccx.adapter_start_lock():
            order.append('second')
    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    assert entered.wait(3)
    assert order == ['first']
    released.set()
    a.join(4)
    b.join(4)
    assert order == ['first', 'second'] and not a.is_alive() and not b.is_alive()
print('adapter startup serialization ok')

# ccx facts carries the launch menu's facts for the picker's sorts.
with patch.object(ccx, 'ensure_key'), patch.object(ccx, 'get_models', return_value=[
        {'id': 'priced', 'context': 1000}, {'id': 'bare'}]), \
        patch.object(ccx.catalog, '_models_dev', return_value={}), \
        patch.object(ccx.catalog, '_artificial_analysis', return_value=True), \
        patch.object(ccx.catalog, '_model_facts', side_effect=lambda _p, mid: {
            'priced': {'limit': {'context': 1000}, 'cost': {'input': 1, 'output': 2}, 'intelligence': 50},
            'bare': {}}[mid]), contextlib.redirect_stdout(io.StringIO()):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ccx.model_facts_command(['nvidia'])
facts = [json.loads(line) for line in out.getvalue().splitlines()]
assert facts[0]['id'] == 'priced' and facts[0]['ctx'] == 1000 and facts[0]['in'] == 1 and facts[0]['out'] == 2
assert facts[0]['aa'] == 50 and facts[0]['missing'] == 0 and facts[0]['score'] > 0
assert facts[1] == {'id': 'bare', 'ctx': None, 'in': None, 'out': None, 'aa': None, 'aa_efforts': {}, 'score': 0.0, 'missing': 3}
print('facts ok')

# The model cache: a listing under a day old comes from disk; a stale check refetches in the background.
with tempfile.TemporaryDirectory() as tmp:
    ccx.MODELS_CACHE_FILE, ccx.MODELS_TTL = os.path.join(tmp, 'models.json'), 86400
    fetched, spawned = [], []
    with patch.object(ccx, 'fetch_models', side_effect=lambda n: (fetched.append(n) or [{'id': 'm1'}], True)),             patch.object(ccx.subprocess, 'Popen', side_effect=lambda *a, **k: spawned.append(a[0])):
        assert ccx.get_models('nvidia') == [{'id': 'm1'}] and fetched == ['nvidia']
        assert ccx.get_models('nvidia') == [{'id': 'm1'}] and fetched == ['nvidia'], 'served from disk'
        assert not spawned, 'fresh: no background check'
        data = json.load(open(ccx.MODELS_CACHE_FILE))
        data['nvidia']['checked'] -= ccx.MODELS_RECHECK + 1
        json.dump(data, open(ccx.MODELS_CACHE_FILE, 'w'))
        assert ccx.get_models('nvidia') == [{'id': 'm1'}] and len(spawned) == 1 and spawned[0][-2:] == ['models-refresh', 'nvidia']
        ccx.get_models('nvidia')
        assert len(spawned) == 1, 'one background check at a time'
        data = json.load(open(ccx.MODELS_CACHE_FILE))
        data['nvidia']['at'] -= 86401
        json.dump(data, open(ccx.MODELS_CACHE_FILE, 'w'))
        ccx.get_models('nvidia')
        assert fetched == ['nvidia', 'nvidia'], 'a day old: fetched live'
    with patch.object(ccx, 'fetch_models', return_value=([{'id': 'fallback'}], False)):
        assert ccx.get_models('gemini') == [{'id': 'fallback'}]
    assert 'gemini' not in json.load(open(ccx.MODELS_CACHE_FILE)), 'a failed fetch is never cached'
    ccx.MODELS_CACHE_FILE, ccx.MODELS_TTL = os.path.join(tempfile.mkdtemp(), 'models-cache.json'), 0
print('model cache ok')

# A child Popen already waited on is dead, even on Windows, where the handle can outlive it.
finished = subprocess.Popen([ccx.sys.executable, '-c', 'pass'],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
finished.wait()
assert not ccx.process_alive(finished.pid), 'a reaped child still reads as alive'

# ccx gui opens the Tkinter picker detached, on the requested tab, and refuses an unknown tab.
# It reports success only once the window registers itself, so a picker that cannot start falls
# back to the in-terminal grid instead of pretending it opened.
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, 'ccx-gui.py').write_text('', encoding='utf-8')
    lock = str(Path(tmp, 'gui.pid'))
    Path(lock).write_text(str(os.getpid()), encoding='utf-8')
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx.subprocess, 'Popen') as popen, \
            patch.object(ccx, 'gui_lock_path', return_value=lock), \
            patch.object(ccx, 'process_alive', return_value=True), \
            contextlib.redirect_stdout(io.StringIO()):
        popen.return_value.poll.return_value = None
        ccx.gui_command([])
        args = popen.call_args.args[0]
        assert args[1:3] == ['-B', os.path.join(tmp, 'ccx-gui.py')], args
        assert args[-2:] == ['--tab', 'model'], args
        assert popen.call_args.kwargs['close_fds'] is True
        ccx.gui_command(['fallbacks'])
        assert popen.call_args.args[0][-2:] == ['--tab', 'fallback']
        assert popen.call_count == 2
    # A picker that dies at once, or never registers, is a failure the mod can fall back on.
    Path(tmp, 'gui-error.log').write_text('TclError: stale failure from an old run', encoding='utf-8')
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx.subprocess, 'Popen') as popen, \
            patch.object(ccx, 'gui_lock_path', return_value=str(Path(tmp, 'no-such.pid'))), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
        popen.return_value.poll.return_value = 1
        popen.return_value.returncode = 1
        try:
            ccx.gui_command([])
        except SystemExit:
            pass
        else:
            raise AssertionError('a picker that exited was reported as opened')
    assert 'stale failure' not in stderr.getvalue() and 'exited 1' in stderr.getvalue(), stderr.getvalue()
    with patch.object(ccx, 'CFG_DIR', tmp), patch.object(ccx.subprocess, 'Popen') as popen:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.gui_command(['nope'])
            except SystemExit:
                pass
            else:
                raise AssertionError('unknown gui tab accepted')
        assert not popen.called
    with patch.object(ccx, 'CFG_DIR', tempfile.mkdtemp()), patch.object(ccx.subprocess, 'Popen') as popen:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                ccx.gui_command([])
            except SystemExit:
                pass
            else:
                raise AssertionError('missing picker script accepted')
        assert not popen.called
print('gui launcher ok')

# `ccx` with no provider opens on the preset list: a preset launches at once, edit saves one, manual walks the menus.
_bundle = {'model': {'provider': 'official', 'model': 'claude-opus-5-5', 'effort': 'medium'},
           'subagent': {'provider': 'official', 'model': 'claude-haiku-5-5', 'effort': 'xhigh'},
           'fallback': {'provider': 'off'}, 'auto': {'provider': 'off'}, 'tools': {'provider': 'off'}}
with tempfile.TemporaryDirectory() as _d, patch.object(ccx, 'presets_path', return_value=os.path.join(_d, 'p.json')), \
     patch.object(ccx, 'load_state', return_value={}), patch.object(ccx, 'apply') as _apply, \
     patch.object(ccx.sys, 'argv', ['ccx']), contextlib.redirect_stdout(io.StringIO()):
    ccx.save_presets({'official': {'model': 'claude-opus-5-5', 'settings': _bundle}})
    with patch.object(ccx, 'preset_menu', return_value='official'), patch.object(ccx, 'provider_menu') as _pm:
        ccx.main()
    assert not _pm.called and _apply.call_args.args[:2] == ('official', 'claude-opus-5-5') \
        and _apply.call_args.kwargs['effort'] == 'medium', 'A preset pick launches without the other menus'
    with patch.object(ccx, 'preset_menu', return_value='official') as _menu, patch.object(ccx.sys, 'argv', ['ccx', 'official']):
        ccx.main()
    assert _menu.call_args.args == ('official',), '`claude` (ccx official) opens on the presets too'
    with patch.object(ccx, 'preset_menu', side_effect=['edit', 'official']), \
         patch.object(ccx, 'provider_menu', side_effect=['official', 'official']), \
         patch.object(ccx, 'model_menu', side_effect=['claude-sonnet-5-5', 'claude-haiku-5-5']), \
         patch.object(ccx, 'effort_menu', side_effect=['high', 'low']):
        ccx.main()
    saved = ccx.saved_bundle('official')
    assert saved['model'] == {'provider': 'official', 'model': 'claude-sonnet-5-5', 'effort': 'high'} \
        and saved['subagent']['effort'] == 'low' and _apply.call_args.args[1] == 'claude-sonnet-5-5', 'Edit saves, then the edited preset launches'
    with patch.object(ccx, 'preset_menu', return_value='manual'), patch.object(ccx, 'provider_menu', side_effect=ccx.catalog.MenuBack):
        with patch.object(ccx, 'preset_menu', side_effect=['manual', ccx.catalog.MenuBack]):
            ccx.main()
print('preset launch menu ok')
