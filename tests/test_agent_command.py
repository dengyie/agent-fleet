"""Installed CLI resolution must never feed an instruction to cmd.exe."""
import json

import pytest

from tools.agent_command import resolve_command


@pytest.mark.parametrize('family,package,entry', [
    ('codex', '@openai/codex', 'bin/codex.js'),
    ('claude_code', '@anthropic-ai/claude-code', 'cli.js'),
    ('pi', '@earendil-works/pi-coding-agent', 'dist/bundle/cli.js'),
    ('pi', '@mariozechner/pi-coding-agent', 'dist/cli.js'),
])
def test_windows_npm_uses_node_and_one_literal_prompt(tmp_path, monkeypatch, family, package, entry):
    root = tmp_path / 'npm' / 'node_modules' / package
    script = root / entry
    script.parent.mkdir(parents=True)
    script.write_text('// installed CLI', encoding='utf-8')
    (root / 'package.json').write_text(json.dumps({'bin': {'pi': entry}}), encoding='utf-8')
    node = tmp_path / 'node.exe'
    node.touch()
    monkeypatch.setattr('tools.agent_command.shutil.which', lambda name, **kw: str(node) if name == 'node' else None)
    text = '读取 中文 🌲 "quoted" & %PATH% $(touch no)\nsecond line'
    name = 'claude' if family == 'claude_code' else family
    assert resolve_command(family, [name, '-p', text], {'APPDATA': str(tmp_path)}, platform='nt') == [
        str(node), str(script.resolve()), '-p', text]


def test_windows_codex_prefers_native_binary(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.agent_command.host_platform.machine', lambda: 'AMD64')
    root = tmp_path / 'npm/node_modules/@openai/codex'
    binary = root / 'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr('tools.agent_command.shutil.which', lambda *a, **kw: None)
    assert resolve_command('codex', ['codex', 'exec', 'hello'], {'APPDATA': str(tmp_path)}, platform='nt') == [
        str(binary.resolve()), 'exec', 'hello']


def test_custom_command_remains_authoritative():
    command = ['/custom/agent.exe', '--custom', '{instruction}']
    assert resolve_command('pi', command, {}, platform='nt') == command
    assert resolve_command('unknown', ['something', 'hello'], {}, platform='nt') == ['something', 'hello']


def test_missing_package_does_not_invent_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.agent_command.shutil.which', lambda *a, **kw: None)
    assert resolve_command('pi', ['pi', '-p', 'hello'], {'APPDATA': str(tmp_path)}, platform='nt') == ['pi', '-p', 'hello']


def test_npm_symlink_cannot_escape_known_package(tmp_path, monkeypatch):
    root = tmp_path / 'npm/node_modules/@earendil-works/pi-coding-agent'
    root.mkdir(parents=True)
    outside = tmp_path / 'outside.js'
    outside.write_text('throw new Error("must not run")', encoding='utf-8')
    (root / 'cli.js').symlink_to(outside)
    (root / 'package.json').write_text(json.dumps({'bin': {'pi': 'cli.js'}}), encoding='utf-8')
    monkeypatch.setattr('tools.agent_command.shutil.which', lambda name, **kw: str(tmp_path / 'node.exe') if name == 'node' else None)
    assert resolve_command('pi', ['pi', 'hello'], {'APPDATA': str(tmp_path)}, platform='nt') == ['pi', 'hello']
