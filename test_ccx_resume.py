"""Offline CLI and PowerShell resume regression checks."""
import contextlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('ccx_resume_switch', ROOT / 'ccx-switch.py')
ccx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccx)
ccx.load_presets = lambda: {}  # the person's saved presets must not steer menu defaults here
SESSION = '00000000-0000-4000-8000-000000000001'
# Keep real credentials out of mock failure output, and make runs inside Claude Code match plain runs.
for _name in [k for k in os.environ if k == 'CLAUDECODE' or re.search('KEY|TOKEN|SECRET|PASSWORD', k.upper())]:
    del os.environ[_name]


def runpy_upstreams():
    spec = importlib.util.spec_from_file_location('ccx_proxy', ROOT / 'ccx-anthropic-proxy.py')
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    return proxy.UPSTREAMS


def proxy_identity():
    """The adapter's own /health answer, so a version bump cannot leave this suite behind."""
    spec = importlib.util.spec_from_file_location('ccx_proxy_identity', ROOT / 'ccx-anthropic-proxy.py')
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    return dict(proxy.IDENTITY)


class ResumeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'PowerShell launcher')
    def test_adapter_launch_has_no_console(self):
        with patch.object(ccx, 'fetch', side_effect=[OSError(), proxy_identity()]), patch.object(ccx.subprocess, 'Popen') as launch:
            ccx.ensure_anthropic_proxy()
        flags = launch.call_args.kwargs['creationflags']
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW, 'no console window for the adapter')
        self.assertTrue(flags & getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0), 'the adapter never starves the desktop')

    def test_outdated_adapter_is_replaced_and_other_services_are_left_alone(self):
        current = proxy_identity()
        with patch.object(ccx, 'fetch', side_effect=[{**current, 'version': 1}, current]), patch.object(ccx, 'stop_port_owner') as stop, patch.object(ccx.subprocess, 'Popen') as launch:
            ccx.ensure_anthropic_proxy()
        stop.assert_called_once_with(ccx.ADAPTER_PORT)
        launch.assert_called_once()
        with patch.object(ccx, 'fetch', return_value={'service': 'something-else'}), patch.object(ccx, 'stop_port_owner') as stop, patch.object(ccx.subprocess, 'Popen') as launch, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ccx.ensure_anthropic_proxy()
        stop.assert_not_called()
        launch.assert_not_called()

    def test_install_preserves_utf16_profiles(self):
        spec = importlib.util.spec_from_file_location('ccx_install', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'ccx'
            for encoding in ('utf-16-le', 'utf-16-be'):
                with self.subTest(encoding=encoding):
                    profile = Path(tmp)/encoding/'profile.ps1'
                    profile.parent.mkdir()
                    original = ('\ufeff# Existing settings\r\n').encode(encoding)
                    profile.write_bytes(original)
                    with contextlib.redirect_stdout(io.StringIO()):
                        installer.install(target, [profile])
                        first = profile.read_bytes()
                        installer.install(target, [profile])
                    self.assertTrue(first.startswith(original))
                    self.assertIn('function ccx {', first.decode(encoding))
                    self.assertEqual(profile.read_bytes(), first)

    def test_install_registers_missing_profiles_and_preserves_existing(self):
        spec = importlib.util.spec_from_file_location('ccx_install', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'ccx'
            existing = Path(tmp)/'WindowsPowerShell'/'profile.ps1'
            missing = Path(tmp)/'PowerShell'/'profile.ps1'
            existing.parent.mkdir()
            original = b"# Preserve existing settings\r\nfunction ccx { Write-Output 'existing' }\r\n"
            existing.write_bytes(original)
            commands = Path(tmp)/'commands'
            commands.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install(target, [existing, missing], commands)
                first = missing.read_bytes()
                installer.install(target, [existing, missing], commands)
            self.assertEqual(existing.read_bytes(), original)
            self.assertEqual(missing.read_bytes(), first)
            self.assertIn(b'function ccx {', first)
            self.assertIn(b'ccx-switch.ps1" @args', first)
            for name in installer.COMMAND_FILES:
                self.assertEqual((commands/name).read_bytes(), (ROOT/'commands'/name).read_bytes(),
                                 f'{name} ships with ccx')
            self.assertTrue(list(target.glob('backup-resume-*/commands/fallback.md')),
                            'Reinstalling backs up the shipped commands')
            for name in installer.FILES:
                self.assertEqual((target/name).read_bytes(), (ROOT/name).read_bytes())
            self.assertTrue((target/'ccx-anthropic-proxy.py').is_file(),
                            'Fresh installs need the OpenAI-format adapter')
            self.assertTrue((target/'ai_suite/service.py').is_file(),
                            'Fresh installs need the shared AI-suite transport')
            code = "import sys, runpy; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[1] + '/ccx-anthropic-proxy.py'); import ai_suite.service as s; assert s.__file__.startswith(sys.argv[1])"
            subprocess.run([sys.executable, '-I', '-S', '-c', code, str(target)],
                           check=True, capture_output=True, **ccx.background_process_options())

    @unittest.skipUnless(os.name == 'nt', 'PowerShell launcher')
    def test_install_replaces_pre_rename_cc_function(self):
        spec = importlib.util.spec_from_file_location('ccx_install', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        legacy = ('# Claude Code custom-provider launcher.\nfunction cc {\n'
                  '    python "$env:USERPROFILE\\.config\\cc\\cc-switch.py" @args\n'
                  '    if ($LASTEXITCODE -eq 0) { . "$env:USERPROFILE\\.config\\cc\\apply.ps1" }\n}\n')
        keep = 'Set-Alias -Name keepme -Value Get-Date\nfunction mine { "cc" }\n'
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'ccx'
            for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
                with self.subTest(encoding=encoding):
                    profile = Path(tmp) / encoding / 'profile.ps1'
                    profile.parent.mkdir()
                    bom = '\ufeff' if encoding.startswith('utf-16') else ''
                    profile.write_bytes((bom + keep + legacy).encode(encoding))
                    with contextlib.redirect_stdout(io.StringIO()):
                        installer.install(target, [profile])
                        first = profile.read_bytes()
                        installer.install(target, [profile])
                    self.assertEqual(profile.read_bytes(), first, 'Installation must be idempotent')
                    text = first.decode(encoding).lstrip('\ufeff')
                    self.assertTrue(first.startswith((bom + keep).encode(encoding)), 'Preserve unrelated profile bytes')
                    self.assertNotIn('function cc ', text)
                    self.assertIn('function ccx {', text)
                    check = Path(tmp) / 'check.ps1'
                    check.write_text('. $env:CCX_TEST_PROFILE\nGet-Command ccx | ForEach-Object { $_.CommandType.ToString() }\n'
                                     '(Get-Alias keepme).Definition\n[bool](Get-Command cc -CommandType Function -ErrorAction Ignore)\n',
                                     encoding='utf-8')
                    result = subprocess.run(['powershell', '-NoProfile', '-File', str(check)],
                        env=dict(os.environ, CCX_TEST_PROFILE=str(profile)), capture_output=True,
                        text=True, check=True, **ccx.background_process_options())
                    self.assertEqual(result.stdout.splitlines(), ['Function', 'Get-Date', 'False'])

    def test_migrate_copies_pre_rename_data_without_overwriting(self):
        spec = importlib.util.spec_from_file_location('ccx_install', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            legacy, target = Path(tmp) / 'cc', Path(tmp) / 'ccx'
            for name in ('keys.json', 'state.json', 'cc-switch.py', 'backup-1/x', 'logos/a.svg', 'oauth/cc-helper.mjs'):
                (legacy / name).parent.mkdir(parents=True, exist_ok=True)
                (legacy / name).write_text('old', encoding='utf-8')
            target.mkdir()
            (target / 'state.json').write_text('new', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                installer.migrate(legacy, target)
            self.assertEqual(sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()),
                             ['keys.json', 'logos/a.svg', 'oauth/cc-helper.mjs', 'state.json'])
            self.assertEqual((target / 'state.json').read_text(encoding='utf-8'), 'new')

    def test_install_registers_posix_shell_function_once(self):
        spec = importlib.util.spec_from_file_location('ccx_install', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            target, rc = Path(tmp) / 'ccx', Path(tmp) / '.zshrc'
            rc.write_bytes(b'export KEEP=1\n')
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install(target, [rc])
                first = rc.read_bytes()
                installer.install(target, [rc])
            self.assertTrue(first.startswith(b'export KEEP=1\n'))
            self.assertIn(b'ccx() {', first)
            self.assertIn(b'. "$HOME/.config/ccx/apply.sh"', first)
            self.assertEqual(rc.read_bytes(), first, 'Installation must be idempotent')
            self.assertTrue((target / 'subagent-picker' / 'hooks' / 'register.tsx').is_file(), 'The /model mod ships too')
            self.assertTrue((target / 'opencode_key.py').is_file(), 'ccx-switch imports opencode_key')

    def test_resume_cli_always_asks_provider_and_model(self):
        cases = [
            (['--resume', SESSION], ['--resume', SESSION]),
            (['--resume'], ['--resume']),
            (['-r', 'named session'], ['-r', 'named session']),
            (['--continue'], ['--continue']),
            (['-c'], ['-c']),
            ([], []),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for argv, forwarded in cases:
                with self.subTest(argv=argv), patch.object(ccx, 'APPLY_FILE', str(Path(tmp)/'apply.ps1')), patch.object(ccx, 'load_state', return_value={'provider': 'nvidia', 'model': 'test/model'}), patch.object(ccx, 'apply') as apply, patch.object(ccx, 'provider_menu', return_value='official') as menu, patch.object(ccx, 'model_menu', return_value='claude-opus-5-5') as models, patch.object(ccx, 'effort_menu', return_value='high'), patch.object(ccx, 'autocompact_menu', return_value=70), patch.object(ccx, 'startup_subagent') as subagents, patch.object(sys, 'argv', ['ccx', *argv]):
                    ccx.main()
                    menu.assert_called_once()
                    models.assert_called_once_with('official')
                    subagents.assert_called_once()
                    apply.assert_called_once_with('official', 'claude-opus-5-5', False, claude_args=forwarded, effort='high', autocompact=70, fallback=None)

    def test_explicit_provider_and_model_skip_menus(self):
        cases = [
            (['official', 'opus', '--resume', SESSION], 'official', 'opus'),
            (['nvidia', 'other/model', '--resume', SESSION], 'nvidia', 'other/model'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for argv, provider, model in cases:
                with self.subTest(argv=argv), patch.object(ccx, 'APPLY_FILE', str(Path(tmp)/'apply.ps1')), patch.object(ccx, 'apply') as apply, patch.object(ccx, 'provider_menu', side_effect=AssertionError('Unexpected menu')), patch.object(ccx, 'model_menu', side_effect=AssertionError('Unexpected model menu')), patch.object(ccx, 'startup_subagent', side_effect=AssertionError('Unexpected subagent menu')), patch.object(sys, 'argv', ['ccx', *argv]):
                    ccx.main()
                    apply.assert_called_once_with(provider, model, False, claude_args=['--resume', SESSION], effort=None, autocompact=None, fallback=None)

    def test_provider_only_still_asks_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ccx, 'APPLY_FILE', str(Path(tmp)/'apply.ps1')), patch.object(ccx, 'provider_menu', side_effect=AssertionError('Unexpected menu')), patch.object(ccx, 'model_menu', return_value='test/model') as models, patch.object(ccx, 'effort_menu', return_value='') as efforts, patch.object(ccx, 'autocompact_menu', return_value=''), patch.object(ccx, 'fallback_menu', return_value=None), patch.object(ccx, 'startup_subagent'), patch.object(ccx, 'apply') as apply, patch.object(sys, 'argv', ['ccx', 'nvidia', '--resume', SESSION]):
            ccx.main()
            models.assert_called_once_with('nvidia')
            efforts.assert_called_once()
            apply.assert_called_once_with('nvidia', 'test/model', False, claude_args=['--resume', SESSION], effort='', autocompact='', fallback=None)

    @unittest.skipUnless(os.name == 'nt', 'PowerShell launcher')
    def test_launch_env_is_restored_so_plain_claude_stays_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp)/'apply.ps1'
            with patch.object(ccx, 'APPLY_FILE', str(script)):
                ccx.write_apply({'ANTHROPIC_BASE_URL': 'http://gateway', 'ANTHROPIC_MODEL': 'test/model'}, ['ANTHROPIC_API_KEY'], True, [], 'test/model')
            check = Path(tmp)/'check.ps1'
            check.write_text("$env:ANTHROPIC_API_KEY = 'original'\nfunction claude { \"during=$env:ANTHROPIC_BASE_URL|$env:ANTHROPIC_API_KEY\" }\n. $env:CCX_TEST_APPLY\n\"after=$env:ANTHROPIC_BASE_URL|$env:ANTHROPIC_MODEL|$env:ANTHROPIC_API_KEY\"\n", encoding='utf-8')
            env = {k: v for k, v in os.environ.items() if not k.startswith('ANTHROPIC_')}
            result = subprocess.run(['powershell', '-NoProfile', '-File', str(check)], env=dict(env, CCX_TEST_APPLY=str(script)), capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.stdout.split()[-2:], ['during=http://gateway|', 'after=||original'])

    def test_commandcode_non_claude_routes_through_adapter(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ccx, 'APPLY_FILE', str(Path(tmp)/'apply.ps1')), patch.object(ccx, 'STATE_FILE', str(Path(tmp)/'state.json')), patch.object(ccx, 'auth_key', return_value='test'), patch.object(ccx, 'ensure_anthropic_proxy') as proxy, patch.object(ccx, 'get_models', return_value=[]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ccx.apply('commandcode', 'deepseek/deepseek-v4.1-flash', no_run=True)
            text = (Path(tmp)/'apply.ps1').read_text()
        proxy.assert_called_once()
        self.assertIn(f"'http://127.0.0.1:{ccx.ADAPTER_PORT}/commandcode'", text)
        self.assertIn('commandcode', runpy_upstreams())

    @unittest.skipUnless(os.name == 'nt', 'PowerShell launcher')
    def test_quoted_arguments_reach_claude(self):
        tricky = "session'$(throw 'must not execute'); with spaces"
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp)/'apply.ps1'
            with patch.object(ccx, 'APPLY_FILE', str(script)):
                ccx.write_apply({'ANTHROPIC_MODEL': 'test/model'}, [], True, ['--resume', tricky], 'test/model')
            check = Path(tmp)/'check.ps1'
            check.write_text("function claude { ConvertTo-Json -InputObject @($args) -Compress }\n. $env:CCX_TEST_APPLY\n", encoding='utf-8')
            result = subprocess.run(['powershell', '-NoProfile', '-File', str(check)], env=dict(os.environ, CCX_TEST_APPLY=str(script)), capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(json.loads(result.stdout.splitlines()[-1]), ['--model', 'test/model', '--resume', tricky])
            self.assertFalse(script.exists())

    @unittest.skipUnless(os.name == 'nt', 'PowerShell launcher')
    def test_compatibility_entry_forwards_native_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(ROOT/'ccx-switch.ps1', root/'ccx-switch.ps1')
            (root/'ccx-switch.py').write_text("import json, sys\nfrom pathlib import Path\nPath(__file__).with_name('args.json').write_text(json.dumps(sys.argv[1:]))\n", encoding='utf-8')
            result = subprocess.run(['powershell', '-NoProfile', '-File', str(root/'ccx-switch.ps1'), '--resume', SESSION, '--continue'], capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root/'args.json').read_text()), ['--resume', SESSION, '--continue'])


class AgentRoutingTests(unittest.TestCase):
    PROFILES = {'sub': {'provider': 'openai-oauth', 'model': 'gpt-test', 'effort': 'xhigh'}}

    def setUp(self):
        # The relay path: a session running through the adapter would send ccx-* in-process instead.
        direct = patch.dict(os.environ, {'ANTHROPIC_BASE_URL': ''})
        direct.start()
        self.addCleanup(direct.stop)

    def test_install_preserves_settings_and_registers_live_routing_once(self):
        spec = importlib.util.spec_from_file_location('ccx_install_routing', ROOT / 'install-resume.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / 'claude'
            claude.mkdir()
            # The pre-rename install: hooks on ~/.config/cc/cc-switch.py and the mod loaded from there.
            old = 'python -B "C:/Users/u/.config/cc/cc-switch.py" '
            existing = {'permissions': {'deny': ['Agent(Explore)'], 'defaultMode': 'auto'},
                        'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'rtk hook claude'}]},
                                                 {'matcher': 'Agent|Task', 'hooks': [{'type': 'command', 'command': old + 'hook-agent'}]}],
                                  'SubagentStop': [{'matcher': '^cc-.*', 'hooks': [{'type': 'command', 'command': old + 'hook-subagent-stop'}]}]},
                        'env': {'CLAUDE_CODE_PLUGIN_DIRS': os.pathsep.join(['/mods/keep', '/home/u/.config/cc/subagent-picker'])},
                        'statusLine': {'type': 'command', 'command': 'old', 'refreshInterval': 60}}
            settings = claude / 'settings.json'
            settings.write_text(json.dumps(existing), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install(root / 'ccx', [], claude=claude)
                first = json.loads(settings.read_text(encoding='utf-8'))
                installer.install(root / 'ccx', [], claude=claude)
            self.assertEqual(json.loads(settings.read_text(encoding='utf-8')), first)
            self.assertEqual(first['permissions'], existing['permissions'])
            self.assertEqual(first['hooks']['PreToolUse'][0], existing['hooks']['PreToolUse'][0])
            for event, command in [('PreToolUse', 'hook-agent'), ('SessionStart', 'hook-context'), ('UserPromptSubmit', 'hook-context')]:
                hooks = [h for item in first['hooks'][event] for h in item['hooks'] if command in h.get('command', '')]
                self.assertEqual(len(hooks), 1)
                self.assertNotIn('allow', hooks[0])
            self.assertNotIn('cc-switch.py', json.dumps(first['hooks']))
            self.assertEqual([g['matcher'] for g in first['hooks']['SubagentStop']], ['^ccx-.*'])
            self.assertEqual(first['env']['CLAUDE_CODE_PLUGIN_DIRS'].split(os.pathsep),
                             ['/mods/keep', (root / 'ccx' / 'subagent-picker').resolve().as_posix()])
            self.assertNotIn('subagentStatusLine', first)
            self.assertEqual(first['statusLine']['refreshInterval'], 60)
            self.assertEqual((claude / 'statusline.py').read_bytes(), (ROOT / 'statusline.py').read_bytes())
            self.assertTrue(list((root / 'ccx').glob('backup-resume-*/claude/settings.json')))

    def test_broken_fork_cutoff_never_launches_a_fresh_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / (SESSION + '.jsonl')
            parent.write_text('{"type":"user","uuid":"real","message":{"content":"parent"}}\n', encoding='utf-8')
            child = root / SESSION / 'subagents/agent-old.jsonl'
            child.parent.mkdir(parents=True)
            child.write_text(json.dumps({'type': 'fork-context-ref', 'parentSessionId': SESSION,
                                         'parentLastUuid': 'missing', 'contextLength': 1}) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'cutoff'):
                with ccx._restorable_context(str(child)):
                    self.fail('Unverified history must never reach launch')

    def test_resuming_relay_uses_latest_worker_context_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / 'relay.jsonl'
            contexts = root / 'contexts'
            contexts.mkdir()
            entries = []
            for number in (1, 2):
                token = f'00000000-0000-4000-8000-{number:012d}'
                record = {'profile': 'sub', 'session_id': token, 'cwd': str(root)}
                (contexts / (token + '.json')).write_text(json.dumps(record), encoding='utf-8')
                entries.append({'type': 'user', 'message': {'content': ccx.CONTEXT_PREFIX + json.dumps({'token': token}) + '\ntask'}})
            transcript.write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf-8')
            with patch.object(ccx, 'CFG_DIR', str(root)):
                self.assertEqual(ccx._recorded_context(transcript)['session_id'], token,
                                 'Resuming the first worker loses work from later continuations')
                (contexts / '00000000-0000-4000-8000-000000000001.json').unlink()
                self.assertEqual(ccx._recorded_context(transcript)['session_id'], token,
                                 'An expired older record must not hide the valid latest worker')

    def test_relay_refuses_dropped_context_instead_of_starting_fresh(self):
        with patch.object(ccx, 'load_agents', return_value=self.PROFILES), \
             patch.object(ccx.subprocess, 'run') as launch, self.assertRaises(SystemExit):
            ccx.run_agent('sub', 'task without required context', require_context=True)
        launch.assert_not_called()
        with patch.dict(os.environ, {'CLAUDECODE': '1', 'CCX_AGENT_CHILD': ''}), \
             patch.object(ccx, 'route_env') as route, \
             patch.object(ccx, 'load_agents', return_value=self.PROFILES), \
             patch.object(ccx.subprocess, 'run') as launch, self.assertRaises(SystemExit):
            ccx.run_agent('sub', 'legacy relay omitted its context')
        route.assert_not_called()
        launch.assert_not_called()

    def test_read_only_worker_stays_read_only_after_relay_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'agent-old'
            base.with_suffix('.meta.json').write_text('{"agentType":"ccx-sub"}', encoding='utf-8')
            base.with_suffix('.jsonl').write_text('{"type":"user","message":{"content":"old"}}\n', encoding='utf-8')
            saved = {'profile': 'sub', 'session_id': SESSION, 'read_only': True}
            payload = {'tool_name': 'Agent', 'permission_mode': 'default', 'transcript_path': str(base),  # auto: resubmit (test_ccx_session)
                       'tool_input': {'subagent_type': 'ccx-sub', 'resume': 'old', 'prompt': 'continue'}}
            with patch.object(ccx, 'load_agents', return_value=self.PROFILES), \
                 patch.object(ccx, '_agent_files', return_value=base), \
                 patch.object(ccx, '_recorded_context', return_value=saved):
                output = ccx.agent_hook(payload)['hookSpecificOutput']
            header = output['updatedInput']['prompt'].partition('\n')[0]
            self.assertNotIn('\\', header, 'the relay model collapses JSON backslash escapes')
            context = json.loads(header[len(ccx.CONTEXT_PREFIX):])
            self.assertIs(context.get('read_only'), True)

    def test_context_workers_reject_profile_privilege_and_read_only_loader_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'parent.jsonl'
            source.write_text('{"type":"user","uuid":"p1","message":{"content":"parent"}}\n', encoding='utf-8')
            for flag, read_only in [('--dangerously-skip-permissions', False), ('--allowedTools=Bash', False),
                                    ('--allow-dangerously-skip-permissions', False), ('--permission-prompt-tool', False),
                                    ('--mcp-config=unsafe.json', True), ('--plugin-dir', True), ('--plugin-url', True),
                                    ('--settings', False), ('--agent', True)]:
                with self.subTest(flag=flag):
                    context = {'token': SESSION, 'source': str(source), 'permission_mode': 'default', 'read_only': read_only}
                    profile = {**self.PROFILES['sub'], 'args': [flag]}
                    with patch.object(ccx, 'load_agents', return_value={'sub': profile}), \
                         patch.object(ccx, 'route_env', return_value={}), patch.object(ccx, 'model_efforts', return_value=None), \
                         patch.object(ccx.subprocess, 'run') as launch, \
                         contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        ccx.run_agent('sub', ccx.CONTEXT_PREFIX + json.dumps(context) + '\ntask')
                    launch.assert_not_called()

    def test_slash_commands_run_without_a_model_turn(self):
        with patch.object(ccx, 'load_agents', return_value=self.PROFILES), patch.object(ccx.subprocess, 'run') as run:
            run.return_value = Mock(stdout='changed', stderr='')
            out = ccx.hook_command({'command_name': 'fallback', 'command_args': 'nvidia a/b'})
            self.assertEqual(out['decision'], 'block')
            self.assertIn('changed', out['reason'])
            self.assertEqual(run.call_args.args[0][-3:], ['fallback', 'nvidia', 'a/b'])
            run.reset_mock()
            # /subagent-autocompact is /model's Compact column now; Claude Code's own commands pass
            for name in ('subagent-autocompact', 'subagent-model', 'review'):
                self.assertEqual(ccx.hook_command({'command_name': name, 'command_args': 'sub 70'}), {})
            run.assert_not_called()

    def test_adapter_workers_tag_requests_for_live_effort(self):
        route = {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{ccx.ADAPTER_PORT}/openai-oauth'}
        with patch.object(ccx, 'load_agents', return_value=self.PROFILES),              patch.object(ccx, 'route_env', return_value=route), patch.object(ccx, 'model_efforts', return_value=None),              patch.object(ccx.subprocess, 'run') as launch,              patch.dict(os.environ, {'CCX_AGENT_CHILD': '1'}), self.assertRaises(SystemExit):
            launch.return_value.returncode = 0
            ccx.run_agent('sub', 'task')
        headers = launch.call_args.kwargs['env']['ANTHROPIC_CUSTOM_HEADERS']
        self.assertIn('x-ccx-worker-profile: sub', headers, 'the adapter retags live effort per request')
        self.assertIn('x-ccx-launch: agent-sub', headers, "the adapter resolves this worker's fallback by launch id")

    def hook(self, command, payload, profiles=None, child=False, resume_type=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles_file = root / 'agents.json'
            profiles_file.write_text(json.dumps(self.PROFILES if profiles is None else profiles), encoding='utf-8')
            pending = root / 'apply.ps1'
            pending.write_text('pending launch', encoding='utf-8')
            payload = {**payload, 'transcript_path': str(root / 'session.jsonl')}
            if resume_type:
                metadata = root / 'session/subagents/agent-old.meta.json'
                metadata.parent.mkdir(parents=True)
                metadata.write_text(json.dumps({'agentType': resume_type}), encoding='utf-8')
                metadata.with_name('agent-old.jsonl').write_text('{"type":"user","message":{"content":"old context"}}\n', encoding='utf-8')
            with patch.object(ccx, 'AGENTS_FILE', str(profiles_file)), \
                 patch.object(ccx, 'APPLY_FILE', str(pending)), \
                 patch.object(ccx.sys, 'argv', ['ccx', command]), \
                 patch.object(ccx.sys, 'stdin', io.StringIO(json.dumps(payload))), \
                 patch.dict(os.environ, {'CCX_AGENT_CHILD': '1' if child else ''}), \
                 patch.object(ccx, 'apply', side_effect=AssertionError('Hook must not launch a client')), \
                 contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                try:
                    ccx.main()
                except SystemExit as exc:
                    self.fail(f'Routing command must handle the hook before launch cleanup: exit {exc.code}')
            self.assertEqual(pending.read_text(encoding='utf-8'), 'pending launch')
            return output.getvalue()

    def test_old_and_new_sessions_block_native_workers_without_silent_fallback(self):
        for kind in ('fork', 'general-purpose', 'Explore', 'Plan', 'custom-agent'):
            with self.subTest(kind=kind):
                payload = {'tool_name': 'Agent', 'permission_mode': 'auto',
                           'tool_input': {'subagent_type': kind, 'prompt': 'task', 'description': 'work'}}
                result = json.loads(self.hook('hook-agent', payload))['hookSpecificOutput']
                self.assertEqual(result['permissionDecision'], 'deny')
                self.assertIn('ccx-sub', result['permissionDecisionReason'])
                self.assertNotIn('updatedInput', result, 'Do not silently discard native role/tool restrictions')

    def test_configured_relay_keeps_normal_permissions_and_input(self):
        payload = {'tool_name': 'Agent', 'permission_mode': 'default', 'tool_input': {  # auto: resubmit (test_ccx_session)
            'subagent_type': 'ccx-sub', 'prompt': 'complete task', 'description': 'work',
            'isolation': 'worktree', 'run_in_background': True}}
        result = json.loads(self.hook('hook-agent', payload))['hookSpecificOutput']
        self.assertNotIn('permissionDecision', result, 'Context handoff must not auto-approve the tool')
        updated = result['updatedInput']
        self.assertEqual({k: v for k, v in updated.items() if k != 'prompt'},
                         {k: v for k, v in payload['tool_input'].items() if k != 'prompt'})
        self.assertEqual(updated['prompt'].partition('\n')[2], 'complete task')
        self.assertEqual(json.loads(self.hook('hook-agent', payload, child=True)), {})
        payload['permission_mode'] = 'plan'
        result = json.loads(self.hook('hook-agent', payload))['hookSpecificOutput']
        context = json.loads(result['updatedInput']['prompt'].split('\n', 1)[0][len(ccx.CONTEXT_PREFIX):])
        self.assertTrue(context['read_only'])
        self.assertEqual(context['permission_mode'], 'plan')

    def test_old_native_resumes_cannot_masquerade_as_configured_workers(self):
        payload = {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'ccx-sub', 'resume': 'old', 'prompt': 'continue'}}
        for prior in ('fork', 'general-purpose'):
            with self.subTest(prior=prior):
                result = json.loads(self.hook('hook-agent', payload, resume_type=prior))['hookSpecificOutput']
                self.assertNotIn('permissionDecision', result)
                self.assertNotIn('resume', result['updatedInput'])
                self.assertIn('agent-old.jsonl', result['updatedInput']['prompt'])
        result = json.loads(self.hook('hook-agent', payload))['hookSpecificOutput']
        self.assertEqual(result['permissionDecision'], 'deny')
        self.assertIn('self-contained', result['permissionDecisionReason'])
        result = json.loads(self.hook('hook-agent', payload, resume_type='ccx-sub'))['hookSpecificOutput']
        self.assertEqual(result['updatedInput']['resume'], 'old')

    def test_missing_profiles_block_delegation_and_context_names_current_route(self):
        payload = {'tool_name': 'Task', 'tool_input': {'prompt': 'task'}}
        result = json.loads(self.hook('hook-agent', payload, profiles={}))['hookSpecificOutput']
        self.assertEqual(result['permissionDecision'], 'deny')
        for event in ('SessionStart', 'UserPromptSubmit'):
            result = json.loads(self.hook('hook-context', {'hook_event_name': event}))['hookSpecificOutput']
            self.assertEqual(result['hookEventName'], event)
            self.assertIn('ccx-sub', result['additionalContext'])
            self.assertIn('openai-oauth/gpt-test', result['additionalContext'])

    def test_worker_marks_child_to_prevent_recursive_routing(self):
        with patch.object(ccx, 'load_agents', return_value=self.PROFILES), \
             patch.object(ccx, 'route_env', return_value={}), \
             patch.object(ccx, 'model_efforts', return_value=None), \
             patch.object(ccx.subprocess, 'run') as launch, \
             patch.dict(os.environ, {'CCX_AGENT_CHILD': '', 'CCX_MAIN_PROVIDER': 'official'}):
            launch.return_value.returncode = 0
            with self.assertRaises(SystemExit):
                ccx.run_agent('sub', 'task')
        self.assertEqual(launch.call_args.kwargs['env'].get('CCX_AGENT_CHILD'), '1')

    def test_worker_restores_parent_and_old_fork_messages_as_normal_history(self):
        parent_id = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / (parent_id + '.jsonl')
            parent.write_text(json.dumps({'type': 'user', 'uuid': 'p1', 'sessionId': parent_id,
                              'message': {'content': 'PARENT_CONTEXT_SENTINEL'}}) + '\n', encoding='utf-8')
            child = root / parent_id / 'subagents/agent-old.jsonl'
            child.parent.mkdir(parents=True)
            entries = [{'type': 'fork-context-ref', 'parentSessionId': parent_id, 'parentLastUuid': 'p1', 'contextLength': 1},
                       {'type': 'user', 'uuid': 'c1', 'parentUuid': None, 'isSidechain': True, 'agentId': 'old',
                        'sessionId': parent_id, 'message': {'content': 'OLD_FORK_ONLY_SENTINEL'}}]
            child.write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf-8')
            original_parent, original_child = parent.read_bytes(), child.read_bytes()
            context = {'token': '00000000-0000-4000-8000-000000000002', 'source': str(child), 'permission_mode': 'plan', 'read_only': True}

            def launched(command, **kwargs):
                restored = Path(command[command.index('--resume') + 1])
                records = [json.loads(line) for line in restored.read_text(encoding='utf-8').splitlines()]
                text = json.dumps(records)
                self.assertIn('PARENT_CONTEXT_SENTINEL', text)
                self.assertIn('OLD_FORK_ONLY_SENTINEL', text)
                self.assertNotIn('fork-context-ref', text)
                old = next(e for e in records if e.get('uuid') == 'c1')
                self.assertIs(old.get('isSidechain'), False, 'Claude silently drops sidechain history otherwise')
                self.assertEqual(old['parentUuid'], 'p1')
                self.assertNotIn('agentId', old)
                self.assertIn('--fork-session', command)
                efforts = [command[i + 1] for i, value in enumerate(command) if value == '--effort']
                self.assertEqual(efforts[-1], 'xhigh', 'Profile args must not override the configured effort')
                self.assertEqual(command[-2:], ['--permission-mode', 'plan'])
                self.assertNotIn('CLAUDECODE', kwargs['env'])
                saved = json.loads((root / 'ccx/contexts' / (context['token'] + '.json')).read_text(encoding='utf-8'))
                self.assertIs(saved.get('read_only'), True)
                return Mock(returncode=0)

            with patch.object(ccx, 'CFG_DIR', str(root / 'ccx')), \
                 patch.dict(os.environ, {'CLAUDECODE': '1', 'CCX_AGENT_CHILD': ''}), \
                 patch.object(ccx, 'load_agents', return_value={'sub': {**self.PROFILES['sub'], 'args': ['--effort', 'low']}}), \
                 patch.object(ccx, 'route_env', return_value={}), \
                 patch.object(ccx, 'model_efforts', return_value=None), \
                 patch.object(ccx.subprocess, 'run', side_effect=launched), self.assertRaises(SystemExit):
                ccx.run_agent('sub', ccx.CONTEXT_PREFIX + json.dumps(context) + '\nContinue the task')
            self.assertEqual(parent.read_bytes(), original_parent)
            self.assertEqual(child.read_bytes(), original_child)


class NavigationTests(unittest.TestCase):
    PROMPTS = ['Provider', 'Model', 'Effort', 'Auto-compact', 'Subagent provider',
               'Subagent model', 'Subagent effort', 'Subagent auto-compact']

    def run_keys(self, keys, argv=(), agents=None):
        """Exercise the real menus and launcher with only keyboard and external work stubbed."""
        seen = []
        stream = iter(keys)
        screen = io.StringIO()
        screen.isatty = lambda: True
        arrow_menu = ccx.catalog._arrow_menu

        def getch():
            try:
                return next(stream)
            except StopIteration:
                self.fail(f'Unexpected extra key read at {seen[-1][0]}; Escape did not go back')

        def menu(title, rows, default, *args, **kwargs):
            seen.append((kwargs.get('name') or title, default))
            save.assert_not_called()  # Backtracking must not save an unfinished profile.
            return arrow_menu(title, rows, default, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ccx, 'APPLY_FILE', str(Path(tmp) / 'apply.ps1')))
            stack.enter_context(patch.object(ccx, 'load_state', return_value={}))
            # never the real presets.json: an Enter on the preset screen's edit row would overwrite it
            stack.enter_context(patch.object(ccx, 'presets_path', return_value=str(Path(tmp) / 'presets.json')))
            stack.enter_context(patch.object(ccx, 'load_agents', return_value=agents or {}))
            stack.enter_context(patch.object(ccx, '_latest_session_agents', return_value=None))
            save = stack.enter_context(patch.object(ccx, 'save_agents'))
            stack.enter_context(patch.object(ccx, 'sync_agents'))
            stack.enter_context(patch.object(ccx, 'quit_on_ctrl_c'))
            apply = stack.enter_context(patch.object(ccx, 'apply'))
            stack.enter_context(patch.object(ccx, 'ensure_key'))
            stack.enter_context(patch.object(ccx, 'get_models', side_effect=lambda p: [{'id': p + '/test'}]))
            stack.enter_context(patch.object(ccx.catalog, '_loading', return_value=contextlib.nullcontext()))
            stack.enter_context(patch.object(ccx.catalog, '_models_dev', return_value={}))
            stack.enter_context(patch.object(ccx.catalog, '_artificial_analysis', return_value=[]))
            stack.enter_context(patch.object(ccx.catalog, '_model_facts', return_value={}))
            stack.enter_context(patch.object(ccx.catalog, '_refresh_missing_intelligence'))
            stack.enter_context(patch.object(ccx.catalog, '_enable_ansi'))
            stack.enter_context(patch.object(ccx.catalog, '_arrow_menu', side_effect=menu))
            stack.enter_context(patch.object(ccx.catalog, '_key_reader', return_value=getch))
            stack.enter_context(patch.object(sys, 'stdin', Mock(isatty=lambda: True)))
            stack.enter_context(patch.object(sys, 'stdout', screen))
            stack.enter_context(patch.object(sys, 'argv', ['ccx', *argv]))
            ccx.main()
        return seen, apply, save, screen.getvalue()

    def test_escape_returns_to_each_previous_main_and_subagent_prompt(self):
        for at in range(1, len(self.PROMPTS)):
            with self.subTest(prompt=self.PROMPTS[at]):
                seen, apply, save, _ = self.run_keys('\r' * at + '\x1b' + '\r' * (9 - at))
                self.assertEqual([name for name, _ in seen],
                                 self.PROMPTS[:at + 1] + self.PROMPTS[at - 1:])
                apply.assert_called_once_with('official', 'official/test', False,
                                              claude_args=[], effort='', autocompact='', fallback=None)
                save.assert_called_once()

    def test_escape_at_first_prompt_and_ctrl_c_cancel_without_saving(self):
        cases = ['\x1b'] + ['\r' * at + '\x03' for at in range(len(self.PROMPTS))]
        for keys in cases:
            with self.subTest(keys=repr(keys)):
                _, apply, save, output = self.run_keys(keys)
                apply.assert_not_called()
                save.assert_not_called()
                self.assertIn('No change.', output)

    def test_backtracking_keeps_answers_and_changing_provider_drops_old_model(self):
        seen, apply, save, _ = self.run_keys('\r\rhigh\r\x1b\r80\r\x1b\rskip\r')
        self.assertEqual([default for name, default in seen if name == 'Effort'], ['default', 'high'])
        self.assertEqual([default for name, default in seen if name == 'Auto-compact'],
                         ['default', 'default', '80'])
        # A direct route has nothing to fall back to, so it is not asked; the question's default is no.
        self.assertNotIn('Add a fallback model?', [name for name, _ in seen])
        apply.assert_called_once_with('official', 'official/test', False,
                                      claude_args=[], effort='high', autocompact=80, fallback=None)
        save.assert_not_called()
        seen, apply, _, _ = self.run_keys('\r\r\x1b\x1bnvidia\r\r\r\r\rskip\r')
        self.assertEqual([default for name, default in seen if name == 'Model'],
                         ['official/test', 'official/test', 'nvidia/test'])
        self.assertEqual(apply.call_args.args[:2], ('nvidia', 'nvidia/test'))
        # An adapter route asks the same question; Escape there returns to auto-compact.
        self.assertEqual([default for name, default in seen if name == 'Add a fallback model?'], ['no'])
        self.assertIsNone(apply.call_args.kwargs['fallback'])
        seen, apply, _, _ = self.run_keys('nvidia\r\r\r\ryes\r\x1b\r\rskip\r')
        at = [name for name, _ in seen].index('Fallback provider')
        self.assertEqual([name for name, _ in seen][at:at + 2], ['Fallback provider', 'Auto-compact'])
        self.assertIsNone(apply.call_args.kwargs['fallback'])
        seen, apply, _, _ = self.run_keys('nvidia\r\r\r\ryes\rgemini\r\rskip\r')
        self.assertEqual(apply.call_args.kwargs['fallback'], {'provider': 'gemini', 'model': 'gemini/test'})

    def test_back_skips_prompts_supplied_by_launch_flags(self):
        seen, apply, _, _ = self.run_keys('\r\r\x1b\rskip\r',
                                         ['--effort', 'high', '--autocompact', '80', '--resume', SESSION])
        self.assertEqual([name for name, _ in seen],
                         ['Provider', 'Model', 'Subagent provider', 'Model', 'Subagent provider'])
        apply.assert_called_once_with('official', 'official/test', False,
                                      claude_args=['--effort', 'high', '--resume', SESSION],
                                      effort=None, autocompact=80, fallback=None)

    def test_subagent_provider_back_returns_to_profile_selection(self):
        agents = {'one': {}, 'two': {}}
        seen, apply, save, _ = self.run_keys('\r' * 5 + '\x1btwo\rskip\r', agents=agents)
        self.assertEqual([name for name, _ in seen], self.PROMPTS[:4] +
                         ['Subagent', 'Subagent provider', 'Subagent', 'Subagent provider'])
        apply.assert_called_once()
        save.assert_not_called()

    def test_agent_editor_back_returns_to_action_then_profile_list(self):
        seen, apply, save, _ = self.run_keys('alpha\r\r\x1b\x1bdone\r', ['agents'], {'alpha': {}})
        self.assertEqual([name for name, _ in seen],
                         ['Subagents', 'alpha', 'Subagent provider', 'alpha', 'Subagents'])
        apply.assert_not_called()
        save.assert_not_called()

    def test_subagent_answers_survive_backtracking_into_main_settings(self):
        keys = '\r' * 4 + 'nvidia\r\rhigh\r' + '\x1b' * 4 + '\r\x03'
        seen, apply, save, _ = self.run_keys(keys)
        self.assertEqual([default for name, default in seen if name == 'Subagent provider'],
                         ['official', 'nvidia', 'nvidia'])
        apply.assert_not_called()
        save.assert_not_called()

    def test_profile_draft_survives_backtracking_through_action_menu(self):
        keys = 'alpha\r\rnvidia\r' + '\x1b' * 2 + '\r\x03'
        seen, apply, save, _ = self.run_keys(keys, ['agents'], {'alpha': {}})
        self.assertEqual([default for name, default in seen if name == 'Subagent provider'],
                         ['official', 'nvidia', 'nvidia'])
        apply.assert_not_called()
        save.assert_not_called()

    def test_profile_selection_survives_backtracking_into_main_settings(self):
        keys = '\r' * 4 + 'two\r' + '\x1b' * 2 + '\r\x03'
        seen, apply, save, _ = self.run_keys(keys, agents={'one': {}, 'two': {}})
        self.assertEqual([default for name, default in seen if name == 'Subagent'],
                         ['one', 'two', 'two'])
        apply.assert_not_called()
        save.assert_not_called()

    def test_editor_list_keeps_selected_profile_when_action_goes_back(self):
        seen, apply, save, _ = self.run_keys('alpha\r\x1b\x03', ['agents'], {'alpha': {}})
        self.assertEqual([default for name, default in seen if name == 'Subagents'],
                         ['done', 'alpha'])
        apply.assert_not_called()
        save.assert_not_called()


class FallbackTests(unittest.TestCase):
    def test_fallback_is_off_until_set_and_per_launch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ccx, 'FALLBACKS_FILE', os.path.join(tmp, 'f.json')), \
                patch.dict(os.environ, {'CCX_LAUNCH_ID': 'one', 'CCX_MAIN_PROVIDER': 'gemini'}), contextlib.redirect_stdout(io.StringIO()) as out:
            ccx.fallback_command([])
            ccx.fallback_command(['NVIDIA', 'deepseek-ai/deepseek-v4-pro'])
            os.environ['CCX_LAUNCH_ID'] = 'two'
            ccx.fallback_command([])
            os.environ['CCX_LAUNCH_ID'] = 'one'
            ccx.fallback_command(['off'])
            self.assertEqual(out.getvalue().splitlines(), ['Fallback: off', 'Fallback: nvidia/deepseek-ai/deepseek-v4-pro', 'Fallback: off', 'Fallback: off'])
            with self.assertRaises(SystemExit):
                ccx.fallback_command(['official', 'claude-opus-5-5'])  # not an adapter route

    def test_fallback_and_route_keep_an_effort(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ccx, 'FALLBACKS_FILE', os.path.join(tmp, 'f.json')),                 patch.object(ccx, 'ROUTES_FILE', os.path.join(tmp, 'r.json')),                 patch.dict(os.environ, {'CCX_LAUNCH_ID': 'one', 'CCX_MAIN_PROVIDER': 'gemini'}), contextlib.redirect_stdout(io.StringIO()) as out:
            ccx.fallback_command(['nvidia', 'x/y', 'high'])
            ccx.route_command(['nvidia', 'x/y', 'default'])
            ccx.route_command(['nvidia', 'x/y', 'low'])
            self.assertEqual(out.getvalue().splitlines(), ['Fallback: nvidia/x/y', 'Effort: high', 'Route: nvidia/x/y',
                                                           'Route: nvidia/x/y', 'Effort: low'])
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ccx.route_command(['nvidia', 'x/y', 'lo w;'])

    def test_picker_lists_routes_without_the_session_own(self):
        with patch.dict(os.environ, {'CCX_MAIN_PROVIDER': 'nvidia', 'CCX_LAUNCH_ID': ''}), contextlib.redirect_stdout(io.StringIO()) as out:
            ccx.fallback_command(['routes'])
        names = [line.split('\t')[0] for line in out.getvalue().splitlines()]
        self.assertEqual(names, [n for n in ccx.FALLBACK_ROUTES if n != 'nvidia'])
        with patch.object(ccx, 'get_models', return_value=[{'id': 'a'}, {'id': 'b'}]), contextlib.redirect_stdout(io.StringIO()) as out:
            ccx.fallback_command(['models', 'gemini'])
        self.assertEqual(out.getvalue().split(), ['a', 'b'])

    def test_fallback_needs_a_ccx_launch(self):
        with patch.dict(os.environ, {'CCX_LAUNCH_ID': ''}), self.assertRaises(SystemExit):
            ccx.fallback_command([])

    def test_slash_fallback_is_answered_without_a_model_turn(self):
        with patch.object(ccx.subprocess, 'run', return_value=Mock(stdout='Fallback: off', stderr='')) as run:
            out = ccx.hook_command({'command_name': 'fallback', 'command_args': 'off'})
        self.assertEqual(run.call_args.args[0][-2:], ['fallback', 'off'])
        self.assertEqual(out['decision'], 'block')
        self.assertIn('Fallback: off', out['reason'])


if __name__ == '__main__':
    unittest.main()
