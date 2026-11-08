"""Install ccx into ~/.config/ccx and register its shell function, with backups.

Windows: PowerShell 5 and 7 profiles. macOS/Linux: ~/.bashrc and ~/.zshrc.
"""
from datetime import datetime
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
FILES = ('ccx-switch.py', 'ccx-switch.ps1', 'ccx-anthropic-proxy.py', 'opencode_key.py', 'patch-oauth-quota.py',
         'oauth-errors.mjs', 'test_ccx_switch.py', 'test_ccx_resume.py', 'test_ccx_proxy.py',
         'statusline.py', 'test_statusline.py', 'critic_mock_check.py', 'install-resume.py', 'README.md',
         'ccx-gui.py', 'test_ccx_gui.py', 'ccx.ico')
# Claude Code slash commands shipped with ccx.
COMMAND_FILES = ('fallback.md',)
# The /model picker mod, loaded through CLAUDE_CODE_PLUGIN_DIRS.
PLUGIN = 'subagent-picker'
PYTHON = 'python' if os.name == 'nt' else 'python3'
FUNCTION = b'''\r\n# Claude Code custom-provider launcher.\r\nfunction ccx {\r\n    & "$env:USERPROFILE\\.config\\ccx\\ccx-switch.ps1" @args\r\n}\r\n'''
SH_FUNCTION = '''
# Claude Code custom-provider launcher.
ccx() {
    python3 "$HOME/.config/ccx/ccx-switch.py" "$@" || return
    if [ -f "$HOME/.config/ccx/apply.sh" ]; then
        . "$HOME/.config/ccx/apply.sh"
        rm -f "$HOME/.config/ccx/apply.sh"
    fi
}
'''

# Replaces the pre-rename ~/.config/cc/cc-switch.py: terminals opened before the upgrade still hold the old
# `cc` function, which runs that file and then applies ~/.config/cc/apply.ps1.
LEGACY_SHIM = """# ccx moved to ~/.config/ccx; this forwards the old `cc` function there.
import os, runpy, shutil, sys
new = os.path.join(os.path.expanduser('~'), '.config', 'ccx')
sys.argv[0] = os.path.join(new, 'ccx-switch.py')
sys.path.insert(0, new)
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    for name in ('apply.ps1', 'apply.sh'):
        if os.path.exists(os.path.join(new, name)):
            shutil.move(os.path.join(new, name), os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
"""
# The launcher's function before the ccx rename, named cc and pointing at ~/.config/cc.
LEGACY_FUNCTION = re.compile(r'(?im)^(?:# Claude Code custom-provider launcher\.\r?\n)?[ \t]*function[ \t]+cc[ \t]*\{')


def copy_tree(source, target, backup):
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if not path.is_file() or any(part == '__pycache__' or part.endswith(('.pyc', '.local.json')) or 'state' in part
                                     for part in relative.parts):
            continue
        destination = target / relative
        if destination.exists():
            saved = backup / target.name / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def install(target, profiles, commands=None, claude=None):
    target = Path(target).resolve()
    # Validate user settings before changing any installed files.
    settings = None
    if claude is not None:
        claude = Path(claude).resolve()
        settings_path = claude / 'settings.json'
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
        if not isinstance(settings, dict) or not isinstance(settings.get('hooks', {}), dict):
            raise ValueError('Claude settings must contain objects, not arrays or strings')
    backup = target / ('backup-resume-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    for name in FILES:
        destination = target / name
        if destination.exists():
            shutil.copy2(destination, backup / name)
        shutil.copy2(ROOT / name, destination)
        assert destination.read_bytes() == (ROOT / name).read_bytes(), name
    if commands:
        commands.mkdir(parents=True, exist_ok=True)
        for name in COMMAND_FILES:
            destination = commands / name
            if destination.exists():
                saved = backup / 'commands' / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
            shutil.copy2(ROOT / 'commands' / name, destination)
            assert destination.read_bytes() == (ROOT / 'commands' / name).read_bytes(), name
    if (ROOT / PLUGIN).is_dir():
        copy_tree(ROOT / PLUGIN, target / PLUGIN, backup)
    if settings is not None:
        claude.mkdir(parents=True, exist_ok=True)
        saved = backup / 'claude'
        saved.mkdir()
        for name in ('settings.json', 'statusline.py'):
            if (claude / name).exists():
                shutil.copy2(claude / name, saved / name)
        shutil.copy2(ROOT / 'statusline.py', claude / 'statusline.py')
        command = PYTHON + ' -B "' + (target / 'ccx-switch.py').as_posix() + '" '
        hooks = settings.setdefault('hooks', {})
        for event, matcher, entry in [('PreToolUse', 'Agent|Task', 'hook-agent'),
                                      ('SessionStart', None, 'hook-context'),
                                      ('UserPromptSubmit', None, 'hook-context'),
                                      ('SubagentStop', '^ccx-.*', 'hook-subagent-stop'),
                                      ('PostToolUse', 'Agent|Task', 'hook-review'),
                                      ('UserPromptExpansion', 'fallback', 'hook-command')]:
            groups = hooks.setdefault(event, [])
            # Replace only our own entries; preserve every unrelated hook and matcher.
            for group in groups:
                group['hooks'] = [h for h in group.get('hooks', []) if not (
                    re.search(r'ccx?-switch\.py', h.get('command', '')) and h.get('command', '').rstrip().endswith(entry))]
            groups[:] = [group for group in groups if group.get('hooks')]
            group = {'hooks': [{'type': 'command', 'command': command + entry, 'timeout': 20}]}
            if matcher:
                group['matcher'] = matcher
            groups.append(group)
        settings.setdefault('statusLine', {}).update(
            type='command', command=PYTHON + ' -B "' + (claude / 'statusline.py').as_posix() + '"')
        # Claude Code draws its own agent panel rows; drop ccx's former override of them.
        if 'agent-statusline' in str((settings.get('subagentStatusLine') or {}).get('command', '')):
            del settings['subagentStatusLine']
        if (ROOT / PLUGIN).is_dir():
            env = settings.setdefault('env', {})
            plugin = (target / PLUGIN).as_posix()
            # Drops the pre-rename ~/.config/cc copy of the mod.
            dirs = [d for d in str(env.get('CLAUDE_CODE_PLUGIN_DIRS') or '').split(os.pathsep)
                    if d and not d.replace('\\', '/').endswith('/.config/cc/' + PLUGIN)]
            env['CLAUDE_CODE_PLUGIN_DIRS'] = os.pathsep.join(dirs + [plugin] * (plugin not in dirs))
        temporary = settings_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
        temporary.replace(settings_path)
    # The vendored package is generated by ai-suite/sync.py, never edited here.
    copy_tree(ROOT / 'ai_suite', target / 'ai_suite', backup)
    for profile in profiles:
        register(profile, backup)
    print('Installed ccx. Open a new terminal, then run: ccx   Backup:', backup)


def register(profile, backup):
    """Add the ccx function to one shell profile, once."""
    original = profile.read_bytes() if profile.exists() else b''
    if profile.suffix == '.ps1':
        encoding = ('utf-16-le' if original.startswith(b'\xff\xfe') else
                    'utf-16-be' if original.startswith(b'\xfe\xff') else
                    'utf-8' if original.startswith(b'\xef\xbb\xbf') else 'latin-1')
        bom = next((b for b in (b'\xff\xfe', b'\xfe\xff', b'\xef\xbb\xbf') if original.startswith(b)), b'')
        text = original.decode(encoding).lstrip('﻿')
        addition = '' if re.search(r'(?im)^\s*function\s+ccx\s*\{', text) else FUNCTION.decode('ascii')
        cleaned = drop_legacy(text)
        if cleaned != text:
            original = bom + cleaned.encode(encoding)
    else:
        encoding = 'utf-8'
        text = original.decode(encoding, errors='replace')
        addition = '' if re.search(r'(?m)^\s*ccx\s*\(\)', text) else SH_FUNCTION
    if not addition and (not profile.exists() or original == profile.read_bytes()):
        return
    profile.parent.mkdir(parents=True, exist_ok=True)
    if profile.exists():
        shutil.copy2(profile, backup / (profile.parent.name + '-' + profile.name))
    profile.write_bytes(original + addition.encode(encoding))
    print('Registered ccx:', profile)


def drop_legacy(text):
    """Text without the pre-rename `function cc` block, when it ran ~/.config/cc."""
    match = LEGACY_FUNCTION.search(text)
    if not match:
        return text
    depth = 0
    for end in range(match.end() - 1, len(text)):
        depth += {'{': 1, '}': -1}.get(text[end], 0)
        if depth == 0:
            break
    else:
        return text
    if '.config\\cc\\' not in text[match.start():end]:
        return text
    rest = text[end + 1:]
    return text[:match.start()] + rest[2 if rest.startswith('\r\n') else 1 if rest.startswith('\n') else 0:]


def migrate(legacy, target):
    """Copy keys, saved picks, profiles and the OAuth build from a pre-rename install; never overwrite. The old folder stays."""
    def skip(directory, names):
        top = Path(directory) == legacy
        return {n for n in names if n.startswith('backup') or n == '__pycache__' or '.bak' in n or n.endswith(('.pid', '.lock'))
                or top and (n.startswith(('cc-', 'test_cc_', 'apply.')) or n in ('cc.ico', '~'))}
    try:
        shutil.copytree(legacy, target, ignore=skip, dirs_exist_ok=True,
                        copy_function=lambda src, dst: None if os.path.exists(dst) else shutil.copy2(src, dst))
    except shutil.Error as exc:
        print(f'Could not copy {len(exc.args[0])} files from {legacy}; copy them by hand if needed.')
    print('Copied settings from', legacy, 'to', target)


def shell_profiles(home):
    if os.name == 'nt':
        return [home / 'Documents' / shell / 'Microsoft.PowerShell_profile.ps1'
                for shell in ('WindowsPowerShell', 'PowerShell')]
    # bash and zsh; macOS's default shell is zsh, most Linux desktops use bash.
    return [home / '.bashrc', home / '.zshrc']


def main():
    if sys.version_info < (3, 10):
        sys.exit('ccx needs Python 3.10 or newer.')
    if not shutil.which('claude'):
        print('Note: `claude` is not on PATH yet; install Claude Code before running ccx.')
    home = Path.home()
    commands = home / '.claude' / 'commands'
    commands.mkdir(parents=True, exist_ok=True)
    legacy, target = home / '.config/cc', home / '.config/ccx'
    if legacy.is_dir() and not (target / 'ccx-switch.py').exists():
        migrate(legacy, target)
    if (legacy / 'cc-switch.py').is_file():
        (legacy / 'cc-switch.py').write_text(LEGACY_SHIM, encoding='utf-8')
    install(target, shell_profiles(home), commands, home / '.claude')
    # Rewrites ~/.claude/agents so the ccx-<name> subagent types exist right after an upgrade.
    subprocess.run([sys.executable, '-B', '-c', 'import runpy, sys; runpy.run_path(sys.argv[1])["sync_agents"]()',
                    str(target / 'ccx-switch.py')], check=False)


if __name__ == '__main__':
    main()
