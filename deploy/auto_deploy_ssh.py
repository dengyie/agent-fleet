#!/usr/bin/env python3
"""Root-owned forced SSH command: bounded upload, then a detached systemd job."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile

try:
    from .auto_release import DeployError, Deployment, MAX_ARCHIVE, atomic_json, safe_extract, build_bundle
except ImportError:  # Installed standalone helper.
    from auto_release import DeployError, Deployment, MAX_ARCHIVE, atomic_json, safe_extract, build_bundle

CONFIG = Path('/etc/agent-fleet-deploy.json')


def parse_command(command):
    match = re.fullmatch(r'(check|deploy) ([0-9a-f]{40}) ([1-9][0-9]{0,19}) ([0-9a-f]{64}) ([1-9][0-9]{0,9})', command)
    if not match or int(match[5]) > MAX_ARCHIVE:
        raise DeployError('invalid_ssh_command')
    return match[1], match[2], match[3], match[4], int(match[5])


def receive(stream, target, size, digest):
    actual = hashlib.sha256()
    left = size
    with target.open('xb') as out:
        while left:
            chunk = stream.read(min(left, 65536))
            if not chunk:
                raise DeployError('truncated_upload')
            out.write(chunk)
            actual.update(chunk)
            left -= len(chunk)
    if actual.hexdigest() != digest:
        raise DeployError('upload_digest_mismatch')


def interrupted(*_):
    raise DeployError('deployment_interrupted')


def execute_job(job_path):
    try:
        from .auto_deploy_host import HostHooks
    except ImportError:
        from auto_deploy_host import HostHooks
    config = json.loads(CONFIG.read_text())
    job = json.loads(job_path.read_text())
    result_path = job_path.parent / 'result.json'
    try:
        stage = job_path.parent / 'stage'
        stage.mkdir()
        safe_extract(job_path.parent / 'bundle.tgz', stage, bundle=True)
        hooks = HostHooks(config)
        result = Deployment(config, hooks).apply(stage, job['sha'], job['run'], check_only=job['mode'] == 'check')
    except Exception as exc:
        result = {'status': 'failed', 'error': str(exc) if isinstance(exc, DeployError) else type(exc).__name__,
                  'sha': job['sha'], 'run': job['run']}
    atomic_json(result_path, result)
    # Keep the small job/result records; code bundles are already archived by CI.
    shutil.rmtree(job_path.parent / 'stage', ignore_errors=True)
    (job_path.parent / 'bundle.tgz').unlink(missing_ok=True)
    return 0 if result['status'] in ('deployed', 'already_deployed', 'checked') else 1


def main():
    # Packaging runs on the unprivileged GitHub runner without host config.
    if len(sys.argv) > 1 and sys.argv[1] == 'bundle':
        parser = argparse.ArgumentParser()
        parser.add_argument('bundle')
        for name in ('backend', 'frontend', 'output', 'sha', 'run'):
            parser.add_argument('--' + name, required=True)
        args = parser.parse_args()
        build_bundle(Path(args.backend), Path(args.frontend), Path(args.output), args.sha, args.run)
        return 0
    if os.geteuid() != 0 or CONFIG.stat().st_uid != 0 or CONFIG.stat().st_mode & 0o077:
        raise DeployError('private_root_configuration_required')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if len(sys.argv) == 3 and sys.argv[1] == '--job':
        return execute_job(Path(sys.argv[2]))
    if len(sys.argv) != 2:
        raise DeployError('forced_command_required')
    mode, sha, run, digest, size = parse_command(sys.argv[1])
    config = json.loads(CONFIG.read_text())
    inbox = Path(config['state']) / 'incoming'
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(inbox).free < MAX_ARCHIVE * 8:
        raise DeployError('insufficient_deployment_disk')
    directory = Path(tempfile.mkdtemp(prefix=run + '-', dir=inbox))
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(120)
    try:
        receive(sys.stdin.buffer, directory / 'bundle.tgz', size, digest)
    except BaseException:
        shutil.rmtree(directory)
        raise
    finally:
        signal.alarm(0)
    job = directory / 'job.json'
    atomic_json(job, {'mode': mode, 'sha': sha, 'run': run})
    # systemd owns the transaction even if Actions loses its SSH connection.
    process = subprocess.run([
        'systemd-run', '--quiet', '--wait', '--collect',
        '--unit=agent-fleet-deploy-' + directory.name,
        '--property=Type=exec', '--property=RuntimeMaxSec=600', '--property=TimeoutStopSec=120',
        sys.executable, str(Path(__file__).resolve()), '--job', str(job),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = directory / 'result.json'
    if result.exists():
        outcome = json.loads(result.read_text())
    else:
        outcome = {'status': 'unconfirmed', 'sha': sha, 'run': run}
    print(json.dumps(outcome))
    return process.returncode or (0 if outcome.get('status') in ('checked', 'deployed', 'already_deployed') else 1)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc) if isinstance(exc, DeployError) else type(exc).__name__}))
        raise SystemExit(1)
