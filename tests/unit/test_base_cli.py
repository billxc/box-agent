import sys
from pathlib import Path
from unittest.mock import patch

from boxagent.agent.base_cli import BaseCLIProcess


def test_resolve_windows_claude_cmd_bypasses_cmd_shim(tmp_path):
    if sys.platform != 'linux':
        # test patches sys.platform internally; guard only for lint clarity
        pass

    root = tmp_path / 'npm-global'
    root.mkdir()
    claude_cmd = root / 'claude.CMD'
    claude_cmd.write_text('shim')
    node_exe = root / 'node.exe'
    node_exe.write_text('node')
    cli_js = root / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'cli.js'
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text('// cli')

    with patch('boxagent.agent.base_cli.sys.platform', 'win32'), \
         patch('boxagent.agent.base_cli.shutil.which', side_effect=lambda name: str(claude_cmd) if name == 'claude' else None):
        args = BaseCLIProcess._resolve_args(['claude', '-p', 'line1\nline2'])

    assert args[:2] == [str(node_exe), str(cli_js)]
    assert args[2:] == ['-p', 'line1\nline2']


def test_resolve_windows_codex_cmd_bypasses_cmd_shim(tmp_path):
    root = tmp_path / 'npm-global'
    root.mkdir()
    codex_cmd = root / 'codex.CMD'
    codex_cmd.write_text('shim')
    cli_js = root / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text('// cli')

    def which(name: str):
        if name == 'codex':
            return str(codex_cmd)
        if name == 'node':
            return r'C:\\Program Files\\nodejs\\node.exe'
        return None

    with patch('boxagent.agent.base_cli.sys.platform', 'win32'), \
         patch('boxagent.agent.base_cli.shutil.which', side_effect=which):
        args = BaseCLIProcess._resolve_args(['codex', 'exec', 'hello\nworld'])

    assert args[:2] == [r'C:\\Program Files\\nodejs\\node.exe', str(cli_js)]
    assert args[2:] == ['exec', 'hello\nworld']
