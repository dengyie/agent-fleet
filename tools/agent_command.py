"""Resolve known locally installed CLI launchers without Windows batch parsing.

Only family defaults are resolved. Custom commands retain their exact meaning.
The prompt is never inspected, shell-interpreted or read to select a path.
"""
from __future__ import annotations

import os
import platform as host_platform
import shutil
from pathlib import Path

_NPM = {
    'codex': ('codex', (('@openai/codex', 'bin/codex.js'),)),
    'claude_code': ('claude', (('@anthropic-ai/claude-code', 'cli.js'),)),
    'pi': ('pi', (('@earendil-works/pi-coding-agent', 'dist/bundle/cli.js'),
                  ('@mariozechner/pi-coding-agent', 'dist/cli.js'))),
}


def _member(root, relative):
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def resolve_command(family, command, environment, *, platform=None):
    """Return an argv template using existing, family-specific local files."""
    result = list(command)
    if not result:
        return result
    platform = os.name if platform is None else platform
    if family == 'zcode' and result[0] == 'zcode' and platform != 'nt':
        if shutil.which('zcode', path=environment.get('PATH')):
            return result
        script = Path('/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs')
        node = shutil.which('node', path=environment.get('PATH'))
        if script.is_file() and node:
            return [node, str(script), *result[1:]]
    definition = _NPM.get(family)
    if platform != 'nt' or not definition or result[0] != definition[0]:
        return result
    name, packages = definition
    found = shutil.which(name, path=environment.get('PATH'))
    if found and Path(found).suffix.lower() not in {'.cmd', '.bat'}:
        return [found, *result[1:]]
    roots = []
    if found:
        roots.append(Path(found).parent / 'node_modules')
    if environment.get('APPDATA'):
        roots.append(Path(environment['APPDATA']) / 'npm/node_modules')
    for base in roots:
        for package, entry in packages:
            root = base / package
            binaries = ['bin/claude.exe'] if family == 'claude_code' else []
            if family == 'codex':
                arch = 'arm64' if host_platform.machine().lower() in {'arm64', 'aarch64'} else 'x64'
                triple = 'aarch64' if arch == 'arm64' else 'x86_64'
                binaries = [f'node_modules/@openai/codex-win32-{arch}/vendor/{triple}-pc-windows-msvc/bin/codex.exe',
                            f'vendor/{triple}-pc-windows-msvc/bin/codex.exe']
            for relative in binaries:
                binary = _member(root, relative)
                if binary:
                    return [str(binary), *result[1:]]
            script = _member(root, entry)
            node = shutil.which('node', path=environment.get('PATH'))
            if script and node:
                return [node, str(script), *result[1:]]
    return result
