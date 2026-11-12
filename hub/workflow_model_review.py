"""Deployable isolated-script command peer for registered Workflow model reviews."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

# -I permits execution from a leased source directory with no cwd imports.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fleet_core.workflow.errors import WorkflowValidationError
from fleet_core.workflow.linux_launcher import _mounts
from fleet_core.workflow.validators import WorkflowValidatorError
from hub.platform.providers.openai_compatible import ProviderError
from hub.platform.workflow_model_reviewer import ModelReviewError, load_config, review


def _launch(options, command: list[str]) -> None:
    config = load_config(options.config, options.config_sha256)
    script = Path(__file__).resolve()
    expected = [sys.executable, '-I', str(script), 'run', '--config', options.config,
        '--config-sha256', options.config_sha256]
    if command != expected:
        raise ModelReviewError('review_command_registration_mismatch')
    bwrap = Path(options.bwrap)
    if not bwrap.is_absolute() or not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        raise ModelReviewError('review_bubblewrap_invalid')
    source, control, scratch = (Path(value).resolve(strict=True) for value in
        (options.source, options.control, options.scratch))
    # Package code/config must never alias the untrusted frozen source.
    readonly = [_ROOT / 'hub', _ROOT / 'fleet_core', config.path, config.api_key_file]
    for path in [*readonly, control, scratch]:
        if path.resolve(strict=True).is_relative_to(source) or source.is_relative_to(path.resolve(strict=True)):
            raise ModelReviewError('review_source_mount_overlap')
    mounts = _mounts({'--bwrap': str(bwrap), '--base': str(Path(sys.base_prefix).resolve()),
        '--venv': str(Path(sys.prefix).resolve()), '--python': str(Path(sys.executable).absolute()),
        '--source': str(source), '--control': str(control), '--scratch': str(scratch),
        '--reviewer': str(script)}, 'review')
    # Fixed tests and old offline reviewers retain their existing network deny.
    # This peer performs HTTP itself; it never executes source or model tools.
    mounts.remove('--unshare-net')
    extra = []
    for path in readonly:
        extra += ['--ro-bind', str(path), str(path)]
    for path in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl/certs',
                 '/usr/share/ca-certificates'):
        if Path(path).exists():
            extra += ['--ro-bind', path, path]
    environment = {'PATH': os.defpath, 'HOME': str(scratch), 'TMPDIR': str(scratch),
        'PYTHONDONTWRITEBYTECODE': '1', 'FLEET_REVIEW_SOURCE': str(source),
        'FLEET_REVIEW_REQUEST': str(control / 'request.json')}
    boundary = mounts.index('--remount-ro')
    argv = [*mounts[:boundary], *extra, *mounts[boundary:], '--clearenv']
    for name, value in environment.items():
        argv += ['--setenv', name, value]
    os.execv(str(bwrap), [*argv, '--', *command])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Single-request Workflow model reviewer and Linux launcher.')
    modes = parser.add_subparsers(dest='mode', required=True)
    for name in ('run', 'launch'):
        child = modes.add_parser(name)
        child.add_argument('--config', required=True)
        child.add_argument('--config-sha256', required=True)
        if name == 'launch':
            for flag in ('bwrap', 'source', 'control', 'scratch'):
                child.add_argument('--' + flag, required=True)
            child.add_argument('command', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if options.mode == 'launch':
            command = options.command
            if not command or command[0] != '--':
                raise ModelReviewError('review_command_delimiter_required')
            _launch(options, command[1:])
            return 1  # execv replaces the process on success.
        config = load_config(options.config, options.config_sha256)
        raw = review(config, Path(os.environ['FLEET_REVIEW_SOURCE']), Path(os.environ['FLEET_REVIEW_REQUEST']))
        sys.stdout.buffer.write(raw)
        return 0
    except (ModelReviewError, WorkflowValidationError, WorkflowValidatorError, ProviderError) as exc:
        code = exc.code if hasattr(exc, 'code') else str(exc)
        print('workflow_model_review_failed:' + code, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError):
        # No upstream text, credential bytes or source are printed on failure.
        print('workflow_model_review_failed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
