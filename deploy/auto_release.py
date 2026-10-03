"""Validated CI bundles and an in-place, data-preserving deployment transaction."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile

MANAGED = (
    'agent_profiles.py', 'README.md', 'requirements.txt', 'requirements-test.txt',
    'report_schema.py', 'session_schema.py', 'platform_schema.py', 'RELEASE_ORIGIN',
    'connectors', 'hub', 'tools', 'deploy', 'docs', 'tests',
)
REQUIRED = ('hub/web.py', 'hub/bootstrap.py', 'requirements.txt', 'platform_schema.py', 'RELEASE_ORIGIN')
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


class DeployError(RuntimeError):
    pass


def identity(sha, run):
    if not re.fullmatch(r'[0-9a-f]{40}', str(sha)) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(run)):
        raise DeployError('invalid_release_identity')


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    with temporary.open('rb') as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_extract(archive, target, *, bundle=False):
    """Reject every link, duplicate and non-regular member before writing."""
    with tarfile.open(archive, 'r:*') as source:
        members, expanded = [], 0
        seen = set()
        for member in source:
            members.append(member)
            expanded += member.size
            if len(members) > 30000 or expanded > MAX_EXPANDED:
                raise DeployError('archive_too_large')
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or not path.parts or path.parts[0] == '.' or member.name in seen
                    or not (member.isfile() or member.isdir())):
                raise DeployError('unsafe_archive_member')
            seen.add(member.name)
            allowed = ('backend', 'frontend', 'release.json') if bundle else (*MANAGED, 'fleet-gates.conf')
            if path.parts[0] not in allowed:
                raise DeployError('unexpected_archive_root')
        for member in members:
            destination = target / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as src, destination.open('xb') as out:
                shutil.copyfileobj(src, out)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)


def validate_stage(stage, sha, run):
    identity(sha, run)
    metadata = json.loads((stage / 'release.json').read_text())
    if metadata != {'sha': sha, 'run': str(run)}:
        raise DeployError('bundle_identity_mismatch')
    backend, frontend = stage / 'backend', stage / 'frontend'
    if any(not (backend / path).is_file() for path in REQUIRED):
        raise DeployError('incomplete_backend')
    if {p.name for p in backend.iterdir()} - set((*MANAGED, 'fleet-gates.conf')):
        raise DeployError('runtime_in_backend')
    origin = dict(line.split(': ', 1) for line in (backend / 'RELEASE_ORIGIN').read_text().splitlines())
    if origin.get('commit') != sha or origin.get('run') != str(run):
        raise DeployError('backend_identity_mismatch')
    manifest = json.loads((frontend / 'manifest.json').read_text())
    files = {p.relative_to(frontend).as_posix() for p in frontend.rglob('*') if p.is_file()}
    if (manifest.get('version') != sha or 'index.html' not in files
            or len(manifest.get('files', [])) != len(set(manifest.get('files', [])))
            or set(manifest.get('files', [])) != files - {'manifest.json'}):
        raise DeployError('frontend_identity_mismatch')
    for relative in files:
        parts = PurePosixPath(relative).parts
        if any(p in ('credentials', '.env', '.git') or p.endswith(('.key', '.pem', '.db', '.sqlite')) for p in parts):
            raise DeployError('runtime_in_frontend')


def build_bundle(backend_archive, frontend, output, sha, run):
    identity(sha, run)
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory)
        safe_extract(backend_archive, stage / 'backend')
        if any(p.is_symlink() for p in frontend.rglob('*')):
            raise DeployError('frontend_link')
        shutil.copytree(frontend, stage / 'frontend')
        (stage / 'release.json').write_text(json.dumps({'sha': sha, 'run': str(run)}))
        validate_stage(stage, sha, run)
        with tarfile.open(output, 'w:gz') as archive:
            for name in ('backend', 'frontend', 'release.json'):
                archive.add(stage / name, arcname=name)
    if output.stat().st_size > MAX_ARCHIVE:
        raise DeployError('archive_too_large')


def remove(path):
    if path.is_symlink():
        raise DeployError('managed_path_is_symlink')
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def copy_managed(source, target, *, uid=None, gid=None):
    for name in MANAGED:
        src, dst = source / name, target / name
        remove(dst)
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
        if uid is not None and dst.exists():
            os.chown(dst, uid, gid)
            if dst.is_dir():
                for path in dst.rglob('*'):
                    os.chown(path, uid, gid)


def backup_databases(live, destination, paths):
    destination.mkdir()
    for relative in paths:
        path = live / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(live.resolve()):
            raise DeployError('database_unavailable')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or dst.execute('PRAGMA foreign_key_check').fetchall():
                    raise DeployError('database_backup_invalid')


def tree_digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class Deployment:
    def __init__(self, config, hooks):
        self.config, self.hooks = config, hooks
        self.live = Path(config['live'])
        self.state = Path(config['state'])
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self):
        with (self.state / 'deploy.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeployError('deployment_busy') from None
            yield

    def apply(self, stage, sha, run, *, check_only=False):
        with self.lock():
            validate_stage(stage, sha, run)
            if self.live.is_symlink() or not self.live.is_dir():
                raise DeployError('live_must_be_directory')
            pending = self.state / 'pending.json'
            if pending.exists():
                raise DeployError('unfinished_deployment_requires_recovery')
            last_path = self.state / 'last-success.json'
            last = json.loads(last_path.read_text()) if last_path.exists() else None
            if last and int(run) < int(last['run']):
                raise DeployError('stale_deployment')
            self.hooks.preflight(stage / 'backend')
            if check_only:
                return {'status': 'checked', 'sha': sha, 'run': str(run)}
            if last and last['sha'] == sha:
                self.hooks.verify(sha)
                self.hooks.public_verify(sha)
                return {'status': 'already_deployed', 'sha': sha, 'run': str(run)}
            backup = Path(tempfile.mkdtemp(prefix=f'{run}-', dir=self.state))
            code = backup / 'code'
            code.mkdir()
            copy_managed(self.live, code)
            old_origin = dict(line.split(': ', 1) for line in (code / 'RELEASE_ORIGIN').read_text().splitlines())
            old_sha = old_origin['commit']
            self.hooks.save(backup)
            record = {'sha': sha, 'run': str(run), 'backup': str(backup), 'phase': 'prepared'}
            atomic_json(pending, record)
            changed = False
            stopping = False
            try:
                self.hooks.maintenance()
                self.hooks.idle()  # Recheck after public admissions have stopped.
                stopping = True
                self.hooks.stop()
                backup_databases(self.live, backup / 'databases', self.config['databases'])
                record['phase'] = 'copying'
                atomic_json(pending, record)
                changed = True  # Copy failure can leave only part of the new tree.
                copy_managed(stage / 'backend', self.live, uid=self.config.get('uid'), gid=self.config.get('gid'))
                static = Path(self.config['frontend_root']) / sha
                if static.exists():
                    if static.is_symlink() or tree_digest(static) != tree_digest(stage / 'frontend'):
                        raise DeployError('frontend_release_conflict')
                else:
                    shutil.copytree(stage / 'frontend', static)
                self.hooks.start()
                self.hooks.verify(sha)
                self.hooks.publish(static)
                self.hooks.public_verify(sha)
                record.update(phase='succeeded', status='deployed')
                atomic_json(backup / 'result.json', record)
                atomic_json(last_path, record)
                pending.unlink()
                return record
            except BaseException as exc:
                record.update(status='failed', error=type(exc).__name__)
                try:
                    if not stopping:
                        self.hooks.resume()
                    else:
                        self.hooks.maintenance()
                        self.hooks.stop()
                        if changed:
                            copy_managed(code, self.live, uid=self.config.get('uid'), gid=self.config.get('gid'))
                        self.hooks.start()
                        self.hooks.verify(old_sha)
                        self.hooks.publish(None)
                    # A failed commit may already have advanced the success record.
                    if last is None:
                        last_path.unlink(missing_ok=True)
                    else:
                        atomic_json(last_path, last)
                    record['phase'] = 'rolled_back'
                    atomic_json(backup / 'result.json', record)
                    pending.unlink()
                except BaseException:
                    record['phase'] = 'rollback_failed'
                    try:
                        atomic_json(pending, record)
                        atomic_json(backup / 'result.json', record)
                    finally:
                        self.hooks.fail_closed()
                raise DeployError(record['phase']) from exc
