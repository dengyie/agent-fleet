"""Fixed Git objects, bounded source packages and verified safe extraction."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Protocol

from fleet_core.workflow.contract import reference_values
from fleet_core.workflow.contract_types import ArtifactReference, CodeRevisionReference, NamedReference
from fleet_core.workflow.errors import WorkflowValidationError
from fleet_core.workflow.identity import validate_reference
from fleet_core.async_io import run_owned_io
from fleet_core.atomic_directory import rename_directory_no_replace
from fleet_core.process.local_process import NO_WINDOW
from fleet_core.artifact_contract import ARTIFACT_MAX_BYTES, ArtifactError, SourceArtifactStore as ArtifactStore

MAX_FILES = 4096
MAX_BYTES = ARTIFACT_MAX_BYTES
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MANIFEST = '.fleet-source.json'
_OBJECT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_FORBIDDEN = frozenset({'.git', '.gitconfig', '.git-credentials', '.netrc', '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.docker',
                        '.env', '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa'})
_CREDENTIAL_STEMS = frozenset({'credential', 'credentials', 'secret', 'secrets'})
_WINDOWS_RESERVED = re.compile(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', re.I)


class SourceExportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceExportResult:
    revision: CodeRevisionReference
    manifest: dict[str, Any]
    included_paths: tuple[str, ...]
    omitted_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceArchiveResult:
    manifest: dict[str, Any]
    included_paths: tuple[str, ...]
    omitted_paths: tuple[str, ...]


def _object(value: str) -> str:
    if type(value) is not str or _OBJECT.fullmatch(value) is None:
        raise SourceExportError('invalid_git_object')
    return value


def _parts(name: str) -> tuple[str, ...]:
    if type(name) is not str or not name or len(name.encode('utf-8')) > 512 or '\\' in name or ':' in name:
        raise SourceExportError('invalid_archive_path')
    parts = tuple(name.split('/'))
    if len(parts) > 32 or any(not p or p in ('.', '..') or p[-1:] in (' ', '.') or _WINDOWS_RESERVED.fullmatch(p) or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts):
        raise SourceExportError('archive_path_escape')
    return parts


def _approved(name: str) -> bool:
    for part in _parts(name):
        lowered = part.lower()
        if lowered in _FORBIDDEN or lowered.partition('.')[0] in _CREDENTIAL_STEMS or lowered.startswith('.env.') or lowered.endswith(('.pem', '.key', '.p12', '.pfx', '.kdbx')):
            return False
    return name != _MANIFEST


def _git_hash(kind: str, raw: bytes, length: int) -> str:
    digest = hashlib.sha1() if length == 40 else hashlib.sha256()
    digest.update(f'{kind} {len(raw)}\x00'.encode('ascii'))
    digest.update(raw)
    return digest.hexdigest()


def _tree_hash(entries: list[dict[str, Any]], length: int) -> str:
    root: dict[str, Any] = {}
    for entry in entries:
        parts = _parts(entry['path'])
        node = root
        for part in parts[:-1]:
            if part in node and type(node[part]) is not dict:
                raise SourceExportError('source_tree_conflict')
            node = node.setdefault(part, {})
        if parts[-1] in node:
            raise SourceExportError('source_tree_conflict')
        node[parts[-1]] = (entry['mode'], entry['oid'])

    def tree(node: dict[str, Any]) -> str:
        records: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            directory = type(value) is dict
            mode, oid = ('40000', tree(value)) if directory else value
            encoded = name.encode('utf-8')
            records.append((encoded + (b'/' if directory else b''), mode.encode() + b' ' + encoded + b'\x00' + bytes.fromhex(oid)))
        records.sort(key=lambda item: item[0])
        return _git_hash('tree', b''.join(item[1] for item in records), length)

    return tree(root)


class _BoundedFile:
    def __init__(self, output: BinaryIO) -> None:
        self.output = output
        self.written = 0

    def write(self, raw: bytes) -> int:
        if self.written + len(raw) > MAX_BYTES:
            raise SourceExportError('source_archive_too_large')
        count = self.output.write(raw)
        self.written += count
        return count


class SourceExporter:
    def __init__(self, repository_root: Path, artifact_store: ArtifactStore | None = None, *, command_timeout: float = 30,
                 object_directory: Path | None = None) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.artifact_store = artifact_store
        self.object_directory = Path(object_directory).resolve() if object_directory is not None else None
        self.alternate_object_directory = None
        if type(command_timeout) not in (int, float) or not 1 <= command_timeout <= 300:
            raise SourceExportError('invalid_command_timeout')
        self.command_timeout = float(command_timeout)

    async def _git(self, *args: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
        process = None
        try:
            environment = {'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                     'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0'}
            if self.object_directory is not None and self.alternate_object_directory is not None:
                environment['GIT_OBJECT_DIRECTORY'] = str(self.object_directory)
                environment['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = str(self.alternate_object_directory)
            process = await asyncio.create_subprocess_exec('git', '--no-replace-objects', *args, cwd=self.repository_root,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            async def collect() -> bytes:
                chunks: list[bytes] = []
                remaining = limit
                while True:
                    chunk = await process.stdout.read(min(65536, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise SourceExportError('source_limit')
                    chunks.append(chunk)
                if await process.wait() != 0:
                    raise SourceExportError('git_object_unavailable')
                return b''.join(chunks)
            return await asyncio.wait_for(collect(), timeout=self.command_timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise SourceExportError('git_unavailable') from exc
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    # The child exited between the returncode check and kill.
                    await process.wait()
                await asyncio.shield(process.wait())

    async def _entries(self, tree: str) -> list[dict[str, Any]]:
        raw = await self._git('ls-tree', '-r', '-z', '-l', tree)
        entries: list[dict[str, Any]] = []
        total = 0
        for record in raw.split(b'\x00'):
            if not record:
                continue
            if len(entries) >= MAX_FILES:
                raise SourceExportError('source_file_limit')
            metadata, name = record.split(b'\t', 1)
            mode, kind, oid, size = metadata.split()
            path = name.decode('utf-8')
            _parts(path)
            included = _approved(path)
            if mode not in (b'100644', b'100755', b'120000', b'160000'):
                raise SourceExportError('unsupported_source_entry')
            if included and (kind != b'blob' or mode not in (b'100644', b'100755')):
                raise SourceExportError('unsupported_source_entry')
            length = int(size) if size != b'-' else 0
            total += length if included else 0
            if total > MAX_BYTES:
                raise SourceExportError('source_archive_too_large')
            entries.append({'path': path, 'mode': mode.decode(), 'oid': _object(oid.decode()), 'size': length, 'included': included})
        if _tree_hash(entries, len(tree)) != tree:
            raise SourceExportError('source_tree_mismatch')
        return entries

    async def export(self, owner_id: str, workspace_id: str, *, repository_id: str, base_commit: str, head_commit: str, tree_id: str) -> SourceExportResult:
        if self.artifact_store is None:
            raise SourceExportError('artifact_store_required')
        try:
            with tempfile.TemporaryDirectory(prefix='workflow-source-') as temp_name:
                path = Path(temp_name) / 'source.tar'
                result = await self.export_archive(repository_id=repository_id, base_commit=base_commit,
                                                   head_commit=head_commit, tree_id=tree_id, destination=path)
                manifest = await run_owned_io(self.artifact_store.put_file, owner_id, workspace_id, path,
                                              name=f"source-{head_commit[:12]}.tar", content_type='application/x-tar')
            artifact = ArtifactReference(manifest['artifact_id'], manifest['sha256'], manifest['size'])
            return SourceExportResult(CodeRevisionReference(repository_id, base_commit, head_commit, tree_id, artifact), manifest,
                                      result.included_paths, result.omitted_paths)
        except SourceExportError:
            raise
        except (ArtifactError, WorkflowValidationError, ValueError, UnicodeError, OSError, tarfile.TarError) as exc:
            raise SourceExportError(getattr(exc, 'code', 'source_export_failed')) from exc

    async def export_archive(self, *, repository_id: str, base_commit: str, head_commit: str, tree_id: str,
                             destination: Path) -> SourceArchiveResult:
        destination = Path(destination)
        try:
            if self.object_directory is not None and self.alternate_object_directory is None:
                alternate = Path((await self._git('rev-parse', '--git-path', 'objects', limit=4096)).decode().strip())
                self.alternate_object_directory = (self.repository_root / alternate).resolve()
            validate_reference(repository_id)
            base, head, tree = (_object(value) for value in (base_commit, head_commit, tree_id))
            if len({len(base), len(head), len(tree)}) != 1:
                raise SourceExportError('git_object_length_mismatch')
            base_value = (await self._git('rev-parse', '--verify', f'{base}^{{commit}}', limit=128)).decode().strip()
            head_value = (await self._git('rev-parse', '--verify', f'{head}^{{commit}}', limit=128)).decode().strip()
            tree_value = (await self._git('rev-parse', '--verify', f'{head}^{{tree}}', limit=128)).decode().strip()
            if (base, head, tree) != (base_value, head_value, tree_value):
                raise SourceExportError('git_revision_mismatch')
            entries = await self._entries(tree)
            metadata = json.dumps({'schema_version': 1, 'repository_id': repository_id, 'base_commit': base, 'head_commit': head,
                                   'tree_id': tree, 'entries': entries}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
            if len(metadata) > MAX_MANIFEST_BYTES:
                raise SourceExportError('source_manifest_too_large')
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as output, tarfile.open(fileobj=_BoundedFile(output), mode='w|', format=tarfile.PAX_FORMAT) as archive:
                self._add(archive, _MANIFEST, metadata, '100644')
                for entry in entries:
                    if not entry['included']:
                        continue
                    blob = await self._git('cat-file', 'blob', entry['oid'], limit=min(MAX_BYTES, entry['size']))
                    if len(blob) != entry['size'] or _git_hash('blob', blob, len(tree)) != entry['oid']:
                        raise SourceExportError('source_blob_mismatch')
                    self._add(archive, entry['path'], blob, entry['mode'])
            return SourceArchiveResult({"schema_version": 1, "repository_id": repository_id, "base_commit": base,
                "head_commit": head, "tree_id": tree, "entries": entries},
                tuple(entry['path'] for entry in entries if entry['included']),
                tuple(entry['path'] for entry in entries if not entry['included']))
        except SourceExportError:
            raise
        except (ArtifactError, WorkflowValidationError, ValueError, UnicodeError, OSError, tarfile.TarError) as exc:
            raise SourceExportError(getattr(exc, 'code', 'source_export_failed')) from exc

    @staticmethod
    def _add(archive: tarfile.TarFile, name: str, raw: bytes, mode: str) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(raw)
        info.mode = 0o755 if mode == '100755' else 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(raw))


def _manifest(archive: tarfile.TarFile, expected: CodeRevisionReference | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    first = archive.next()
    if first is None:
        raise SourceExportError('source_manifest_missing')
    _parts(first.name)
    if (first.name != _MANIFEST or not first.isfile() or first.issparse()
            or set(first.pax_headers) - {'path'} or not 0 < first.size <= MAX_MANIFEST_BYTES):
        raise SourceExportError('source_manifest_missing')
    handle = archive.extractfile(first)
    if handle is None:
        raise SourceExportError('source_manifest_missing')
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceExportError('duplicate_manifest_key')
            result[key] = value
        return result
    value = json.loads(handle.read(MAX_MANIFEST_BYTES + 1).decode('utf-8'), object_pairs_hook=unique)
    if type(value) is not dict:
        raise SourceExportError('invalid_source_manifest')
    if set(value) != {'schema_version', 'repository_id', 'base_commit', 'head_commit', 'tree_id', 'entries'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise SourceExportError('invalid_source_manifest')
    validate_reference(value['repository_id'])
    for key in ('base_commit', 'head_commit', 'tree_id'):
        _object(value[key])
    if expected is not None and any(value[key] != getattr(expected, key) for key in ('repository_id', 'base_commit', 'head_commit', 'tree_id')):
        raise SourceExportError('source_revision_mismatch')
    entries = value['entries']
    if type(entries) is not list or len(entries) > MAX_FILES:
        raise SourceExportError('source_file_limit')
    for item in entries:
        if type(item) is not dict or set(item) != {'path', 'mode', 'oid', 'size', 'included'}:
            raise SourceExportError('invalid_source_manifest')
        _parts(item['path'])
        _object(item['oid'])
        if len(item['oid']) != len(value['tree_id']) or item['mode'] not in ('100644', '100755', '120000', '160000'):
            raise SourceExportError('invalid_source_manifest')
        if type(item['size']) is not int or item['size'] < 0 or type(item['included']) is not bool or item['included'] != _approved(item['path']):
            raise SourceExportError('invalid_source_manifest')
        if item['included'] and item['mode'] not in ('100644', '100755'):
            raise SourceExportError('unsupported_source_entry')
    if _tree_hash(entries, len(value['tree_id'])) != value['tree_id']:
        raise SourceExportError('source_tree_mismatch')
    return value, entries


def _remaining_members(archive: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    while True:
        member = archive.next()
        if member is None:
            return
        yield member


def verify_source_archive(raw: bytes, *, repository_id: str) -> SourceArchiveResult:
    """Validate an uploaded F3 source archive before assigning artifact identity."""
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES:
        raise SourceExportError('invalid_source_archive')
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            manifest, entries = _manifest(archive, None)
            if manifest['repository_id'] != repository_id:
                raise SourceExportError('source_repository_mismatch')
            expected = {item['path']: item for item in entries if item['included']}
            seen: set[str] = set()
            for member in _remaining_members(archive):
                _parts(member.name)
                item = expected.get(member.name)
                if item is None or member.name in seen or not member.isfile() or member.issparse() or member.size != item['size']:
                    raise SourceExportError('unsupported_source_entry')
                if set(member.pax_headers) - {'path'}:
                    raise SourceExportError('unsupported_source_entry')
                handle = archive.extractfile(member)
                if handle is None:
                    raise SourceExportError('source_read_failed')
                contents = handle.read(item['size'] + 1)
                if len(contents) != item['size'] or _git_hash('blob', contents, len(manifest['tree_id'])) != item['oid']:
                    raise SourceExportError('source_blob_mismatch')
                seen.add(member.name)
            if seen != set(expected):
                raise SourceExportError('source_tree_mismatch')
            return SourceArchiveResult(manifest, tuple(sorted(seen)),
                tuple(item['path'] for item in entries if not item['included']))
    except SourceExportError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, tarfile.TarError) as exc:
        raise SourceExportError('invalid_source_archive') from exc


class SourceArchiveStream(Protocol):
    def read(self, size: int) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...


def verify_source_stream(handle: SourceArchiveStream, *, expected: CodeRevisionReference) -> SourceArchiveResult:
    """Check the original F3 archive with fixed-size reads and no extraction."""
    reference_values((NamedReference('source', expected),))
    if not 1 <= expected.source_bundle.size <= MAX_BYTES:
        raise SourceExportError('source_archive_too_large')
    try:
        handle.seek(0)
        with tarfile.open(fileobj=handle, mode='r|') as archive:
            manifest, entries = _manifest(archive, expected)
            selected = {item['path']: item for item in entries if item['included']}
            if len(selected) > MAX_FILES or sum(item['size'] for item in selected.values()) > MAX_BYTES:
                raise SourceExportError('source_expansion_too_large')
            seen: set[str] = set()
            for member in _remaining_members(archive):
                _parts(member.name)
                item = selected.get(member.name)
                if (item is None or member.name in seen or not member.isfile() or member.issparse()
                        or member.size != item['size'] or member.size > MAX_BYTES
                        or set(member.pax_headers) - {'path'}):
                    raise SourceExportError('unsupported_source_entry')
                content = archive.extractfile(member)
                if content is None:
                    raise SourceExportError('source_read_failed')
                digest = hashlib.sha1() if len(manifest['tree_id']) == 40 else hashlib.sha256()
                digest.update(f'blob {member.size}\x00'.encode('ascii'))
                remaining = member.size
                with content:
                    while remaining:
                        chunk = content.read(min(65536, remaining))
                        if not chunk:
                            raise SourceExportError('source_read_failed')
                        remaining -= len(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != item['oid']:
                    raise SourceExportError('source_blob_mismatch')
                seen.add(member.name)
            if seen != set(selected):
                raise SourceExportError('source_tree_mismatch')
            while tail := archive.fileobj.read(65536):
                if any(tail):
                    raise SourceExportError('unsupported_source_entry')
            return SourceArchiveResult(manifest, tuple(sorted(seen)),
                tuple(item['path'] for item in entries if not item['included']))
    except SourceExportError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, tarfile.TarError) as exc:
        raise SourceExportError('invalid_source_archive') from exc


def extract_source_bundle(store: ArtifactStore, owner_id: str, workspace_id: str, artifact_id: str, destination: Path, *,
                          expected_sha256: str | None = None, expected_revision: CodeRevisionReference | None = None,
                          max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> tuple[str, ...]:
    """Verify before publication; caller supplies a private local destination parent."""
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise SourceExportError('invalid_source_limit')
    try:
        if expected_revision is not None:
            reference_values((NamedReference('source', expected_revision),))
            if expected_revision.source_bundle.artifact_id != artifact_id:
                raise SourceExportError('artifact_hash_mismatch')
            expected_sha256 = expected_revision.source_bundle.sha256
        raw = store.read(owner_id, workspace_id, artifact_id)
        if len(raw) > MAX_BYTES or expected_revision is not None and len(raw) != expected_revision.source_bundle.size:
            raise SourceExportError('artifact_hash_mismatch')
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise SourceExportError('artifact_hash_mismatch')
        destination = Path(destination).absolute()
        if destination.is_symlink() or destination.exists():
            raise SourceExportError('destination_exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.workflow-source-', dir=destination.parent) as temporary:
            staging = Path(temporary) / 'tree'
            staging.mkdir(mode=0o700)
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
                metadata, entries = _manifest(archive, expected_revision)
                by_name = {item['path']: item for item in entries if item['included']}
                if len(by_name) > max_files or sum(item['size'] for item in by_name.values()) > max_bytes:
                    raise SourceExportError('source_expansion_too_large')
                seen: set[str] = set()
                for member in _remaining_members(archive):
                    _parts(member.name)
                    item = by_name.get(member.name)
                    if item is None or member.name in seen or not member.isfile() or member.issparse() or member.size != item['size']:
                        raise SourceExportError('unsupported_source_entry')
                    if set(member.pax_headers) - {'path'}:
                        raise SourceExportError('unsupported_source_entry')
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise SourceExportError('source_read_failed')
                    contents = handle.read(item['size'] + 1)
                    if len(contents) != item['size'] or _git_hash('blob', contents, len(metadata['tree_id'])) != item['oid']:
                        raise SourceExportError('source_blob_mismatch')
                    target = staging.joinpath(*_parts(member.name))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as output:
                        output.write(contents)
                    target.chmod(0o755 if item['mode'] == '100755' else 0o644)
                    seen.add(member.name)
                if seen != set(by_name):
                    raise SourceExportError('source_tree_mismatch')
            if destination.is_symlink() or destination.exists():
                raise SourceExportError('destination_exists')
            try:
                rename_directory_no_replace(staging, destination)
            except FileExistsError as exc:
                raise SourceExportError('destination_exists') from exc
            return tuple(sorted(seen))
    except SourceExportError:
        raise
    except (ArtifactError, WorkflowValidationError, OSError, ValueError, RecursionError, tarfile.TarError) as exc:
        raise SourceExportError(getattr(exc, 'code', 'source_extract_failed')) from exc


def extract_source_stream(handle: SourceArchiveStream, destination: Path, *, expected: CodeRevisionReference,
                          should_cancel: Callable[[], bool]) -> tuple[str, ...]:
    """Extract a descriptor-pinned source with bounded reads into a private parent."""
    reference_values((NamedReference('source', expected),))
    if not callable(should_cancel) or not 1 <= expected.source_bundle.size <= MAX_BYTES:
        raise SourceExportError('invalid_source_limit')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise SourceExportError('destination_exists')
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.workflow-source-', dir=destination.parent) as temporary:
            staging = Path(temporary) / 'tree'
            staging.mkdir(mode=0o700)
            handle.seek(0)
            with tarfile.open(fileobj=handle, mode='r|') as archive:
                metadata, entries = _manifest(archive, expected)
                selected = {item['path']: item for item in entries if item['included']}
                if len(selected) > MAX_FILES or sum(item['size'] for item in selected.values()) > MAX_BYTES:
                    raise SourceExportError('source_expansion_too_large')
                seen: set[str] = set()
                for member in _remaining_members(archive):
                    _extract_stream_member(archive, member, selected, seen, staging, len(metadata['tree_id']), should_cancel)
                if seen != set(selected):
                    raise SourceExportError('source_tree_mismatch')
                while tail := archive.fileobj.read(65536):
                    if should_cancel():
                        raise SourceExportError('source_cancelled')
                    if any(tail):
                        raise SourceExportError('unsupported_source_entry')
            if should_cancel():
                raise SourceExportError('source_cancelled')
            rename_directory_no_replace(staging, destination)
            return tuple(sorted(seen))
    except SourceExportError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, tarfile.TarError) as exc:
        raise SourceExportError('source_extract_failed') from exc


def _extract_stream_member(archive: tarfile.TarFile, member: tarfile.TarInfo, selected: dict[str, Any],
                           seen: set[str], staging: Path, hash_length: int, should_cancel: Callable[[], bool]) -> None:
    if should_cancel():
        raise SourceExportError('source_cancelled')
    parts = _parts(member.name)
    item = selected.get(member.name)
    if (item is None or member.name in seen or not member.isfile() or member.issparse()
            or member.size != item['size'] or set(member.pax_headers) - {'path'}):
        raise SourceExportError('unsupported_source_entry')
    content = archive.extractfile(member)
    if content is None:
        raise SourceExportError('source_read_failed')
    target = staging.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1() if hash_length == 40 else hashlib.sha256()
    digest.update(f'blob {member.size}\x00'.encode('ascii'))
    remaining = member.size
    with content, target.open('xb') as output:
        while remaining:
            if should_cancel():
                raise SourceExportError('source_cancelled')
            chunk = content.read(min(65536, remaining))
            if not chunk:
                raise SourceExportError('source_read_failed')
            digest.update(chunk)
            output.write(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != item['oid']:
        raise SourceExportError('source_blob_mismatch')
    target.chmod(0o755 if item['mode'] == '100755' else 0o644)
    seen.add(member.name)


__all__ = ['SourceArchiveResult', 'SourceExportError', 'SourceExportResult', 'SourceExporter', 'extract_source_bundle',
           'extract_source_stream', 'verify_source_archive', 'verify_source_stream']
