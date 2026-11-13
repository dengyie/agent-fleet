"""Strict review evidence and registered test recipes behind the existing sandbox."""
from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fleet_core.workflow.contract import reference_values
from fleet_core.workflow.contract_types import CodeRevisionReference, NamedReference
from fleet_core.workflow.errors import WorkflowValidationError
from fleet_core.workflow.identity import canonical_digest, canonical_json, validate_reference
from fleet_core.workflow.json import decode_snapshot
from fleet_core.workflow.review_protocol import ReviewIdentity
from fleet_core.async_io import run_owned_io
from fleet_core.backends.base import ExecutionBackend
from fleet_core.backends.sandbox import SandboxBackend
from fleet_core.process.bounded_process import ProcessStopUnconfirmed

MAX_REPORT_BYTES = 65536
_JUNIT_PATH = '.fleet-workflow-results.xml'
_DIGEST = re.compile(r'[0-9a-f]{64}')


class WorkflowValidatorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise WorkflowValidatorError('invalid_subject_digest')
    return value


def revision_payload(reference: CodeRevisionReference) -> dict[str, str]:
    try:
        reference_values((NamedReference('source', reference),))
    except WorkflowValidationError as exc:
        raise WorkflowValidatorError('invalid_source_revision') from exc
    return {'repository_id': reference.repository_id, 'base_commit': reference.base_commit,
            'head_commit': reference.head_commit, 'tree_id': reference.tree_id}


@dataclass(frozen=True, slots=True)
class ReviewValidation:
    subject_digest: str
    verdict: str
    executor: ReviewIdentity
    raw_report: bytes
    report_hash: str
    content_digest: str


def validate_review_report(raw: bytes, *, source_revision: CodeRevisionReference, subject_digest: str,
                           executor: ReviewIdentity, developer_executor_id: str) -> ReviewValidation:
    """Execution identity comes from the adapter, never from model self-report."""
    _digest(subject_digest)
    if type(executor) is not ReviewIdentity or executor.executor_id == developer_executor_id:
        raise WorkflowValidatorError('independent_reviewer_required')
    try:
        validate_reference(developer_executor_id)
        value = decode_snapshot(raw, max_bytes=MAX_REPORT_BYTES)
        expected = {'schema_version', 'source_revision', 'subject_digest', 'verdict', 'findings', 'executor'}
        if set(value) != expected or type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise WorkflowValidatorError('invalid_review_report')
        if value['source_revision'] != revision_payload(source_revision) or value['subject_digest'] != subject_digest:
            raise WorkflowValidatorError('review_subject_mismatch')
        if value['executor'] != executor.payload():
            raise WorkflowValidatorError('executor_identity_mismatch')
        if type(value['verdict']) is not str or value['verdict'] not in ('passed', 'failed', 'inconclusive'):
            raise WorkflowValidatorError('invalid_review_verdict')
        findings = value['findings']
        if type(findings) is not list or len(findings) > 128:
            raise WorkflowValidatorError('invalid_findings')
        for finding in findings:
            _finding(finding)
        return ReviewValidation(subject_digest, value['verdict'], executor, raw, hashlib.sha256(raw).hexdigest(), canonical_digest(value))
    except WorkflowValidationError as exc:
        raise WorkflowValidatorError(exc.code) from exc


def _finding(value: Any) -> None:
    if type(value) is not dict or set(value) != {'code', 'severity', 'message'}:
        raise WorkflowValidatorError('invalid_finding')
    for field in ('code', 'severity', 'message'):
        if type(value[field]) is not str or not value[field] or len(value[field].encode('utf-8')) > 2048 or '\x00' in value[field]:
            raise WorkflowValidatorError('invalid_finding')
    if value['severity'] not in ('info', 'warning', 'error'):
        raise WorkflowValidatorError('invalid_finding')


@dataclass(frozen=True, slots=True)
class TestRecipe:
    recipe_id: str
    revision: int
    argv: tuple[str, ...]
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        validate_reference(self.recipe_id)
        if type(self.revision) is not int or not 1 <= self.revision <= 9007199254740991:
            raise WorkflowValidatorError('invalid_recipe_revision')
        if type(self.argv) is not tuple or not 1 <= len(self.argv) <= 32 or any(type(arg) is not str or not arg or len(arg) > 4096 or '\x00' in arg for arg in self.argv):
            raise WorkflowValidatorError('invalid_recipe')
        if type(self.timeout_seconds) not in (int, float) or not 1 <= self.timeout_seconds <= 300:
            raise WorkflowValidatorError('invalid_recipe_timeout')


CODING_DELIVERY_TEST_RECIPE = TestRecipe('coding-delivery-tests', 1, (sys.executable, '-m', 'pytest', '-q', '--junitxml=' + _JUNIT_PATH))
FIXED_TEST_RECIPES = {CODING_DELIVERY_TEST_RECIPE.recipe_id: CODING_DELIVERY_TEST_RECIPE}


@dataclass(frozen=True, slots=True)
class TestValidation:
    source_revision_digest: str
    subject_digest: str
    recipe_id: str
    recipe_revision: int
    exit_code: int | None
    timed_out: bool
    passed: bool
    raw_report: bytes
    junit: bytes | None
    report_hash: str
    executor_id: str
    environment_snapshot: bytes


def _environment(backend: ExecutionBackend, recipe: TestRecipe) -> bytes:
    return canonical_json({"scope": "adapter_host", "os": platform.system(), "architecture": platform.machine(),
        "python_version": platform.python_version(), "backend": type(backend).__module__ + "." + type(backend).__qualname__,
        "recipe_argv_sha256": canonical_digest(list(recipe.argv)),
        "launcher_sha256": canonical_digest(list(backend.launcher)) if isinstance(backend, SandboxBackend) else None})


def _environment_value(raw: bytes) -> dict[str, Any]:
    value = decode_snapshot(raw)
    if set(value) != {"scope", "os", "architecture", "python_version", "backend", "recipe_argv_sha256", "launcher_sha256"} or value["scope"] != "adapter_host":
        raise WorkflowValidatorError('invalid_test_environment')
    for key in ('os', 'architecture', 'python_version', 'backend'):
        if type(value[key]) is not str or not value[key] or len(value[key].encode('utf-8')) > 256 or '\x00' in value[key]:
            raise WorkflowValidatorError('invalid_test_environment')
    _digest(value['recipe_argv_sha256'])
    if value['launcher_sha256'] is not None:
        _digest(value['launcher_sha256'])
    return value


def _read_junit(workdir: Path) -> bytes | None:
    path = workdir / _JUNIT_PATH
    if not hasattr(os, 'O_NOFOLLOW'):
        raise WorkflowValidatorError('junit_boundary_unavailable')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowValidatorError('invalid_junit') from exc
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
            raise WorkflowValidatorError('invalid_junit')
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES or b'\x00' in raw or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I):
        raise WorkflowValidatorError('invalid_junit')
    return raw


def _junit_passed(raw: bytes | None) -> bool:
    if raw is None:
        return False
    if type(raw) is not bytes or len(raw) > MAX_REPORT_BYTES or b'\x00' in raw or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I):
        raise WorkflowValidatorError('invalid_junit')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WorkflowValidatorError('invalid_junit') from exc
    if root.tag not in ('testsuite', 'testsuites'):
        raise WorkflowValidatorError('invalid_junit')
    cases = tuple(root.iter('testcase'))
    return bool(cases) and any(case.find('skipped') is None for case in cases) and not any(
        node.tag in ('failure', 'error') or node.get('failures', '0') != '0' or node.get('errors', '0') != '0' for node in root.iter())


class FixedTestRunner:
    """A missing sandbox launcher fails closed using SandboxBackend's contract."""
    def __init__(self, recipes: dict[str, TestRecipe] | None = None, *,
                 backend_factory: Callable[[Path], ExecutionBackend] = SandboxBackend) -> None:
        self.recipes = dict(FIXED_TEST_RECIPES if recipes is None else recipes)
        if len(self.recipes) > 32 or any(type(value) is not TestRecipe or key != value.recipe_id for key, value in self.recipes.items()):
            raise WorkflowValidatorError('invalid_recipe_catalog')
        self.backend_factory = backend_factory

    async def run(self, source_revision: CodeRevisionReference, subject_digest: str, workdir: Path, *, recipe_id: str,
                  executor_id: str, should_cancel: Callable[[], bool] | None = None) -> TestValidation:
        _digest(subject_digest)
        revision = revision_payload(source_revision)
        validate_reference(executor_id)
        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            raise WorkflowValidatorError('recipe_not_registered')
        workdir = Path(workdir).resolve()
        if not workdir.is_dir() or (workdir / _JUNIT_PATH).exists() or (workdir / _JUNIT_PATH).is_symlink():
            raise WorkflowValidatorError('test_workdir_not_fresh')
        command_id = canonical_digest({'source': revision, 'subject': subject_digest, 'recipe': recipe.recipe_id, 'revision': recipe.revision})
        try:
            backend = await run_owned_io(self.backend_factory, workdir)
            environment = _environment(backend, recipe)
            control = {'should_cancel': should_cancel} if should_cancel is not None else {}
            receipt = await run_owned_io(backend.execute, command_id, list(recipe.argv), timeout_s=recipe.timeout_seconds, **control)
        except ProcessStopUnconfirmed as exc:
            raise WorkflowValidatorError('test_stop_unconfirmed') from exc
        except (OSError, ValueError) as exc:
            raise WorkflowValidatorError('test_execution_failed') from exc
        if receipt.error_code == 'sandbox_unavailable':
            raise WorkflowValidatorError('sandbox_unavailable')
        if receipt.state == 'cancelled':
            raise WorkflowValidatorError('test_cancelled')
        if receipt.state == 'unknown':
            raise WorkflowValidatorError('test_stop_unconfirmed')
        junit = await run_owned_io(_read_junit, workdir)
        exit_code = receipt.result.get('returncode')
        if exit_code is not None and type(exit_code) is not int:
            raise WorkflowValidatorError('invalid_test_receipt')
        timed_out = receipt.error_code == 'timeout'
        passed = receipt.state == 'succeeded' and exit_code == 0 and _junit_passed(junit)
        raw = canonical_json({'schema_version': 1, 'source_revision': revision, 'subject_digest': subject_digest,
            'recipe': {'id': recipe.recipe_id, 'revision': recipe.revision}, 'executor_id': executor_id,
            'exit_code': exit_code, 'timed_out': timed_out, 'passed': passed,
            'junit_sha256': hashlib.sha256(junit).hexdigest() if junit is not None else None,
            'error_code': receipt.error_code, 'output_truncated': receipt.truncated, 'environment': _environment_value(environment)})
        return TestValidation(canonical_digest(revision), subject_digest, recipe.recipe_id, recipe.revision, exit_code,
                              timed_out, passed, raw, junit, hashlib.sha256(raw).hexdigest(), executor_id, environment)


def test_report_bytes(result: TestValidation, source_revision: CodeRevisionReference) -> bytes:
    if type(result) is not TestValidation or type(result.raw_report) is not bytes or len(result.raw_report) > MAX_REPORT_BYTES or hashlib.sha256(result.raw_report).hexdigest() != result.report_hash:
        raise WorkflowValidatorError('invalid_test_report')
    try:
        value = decode_snapshot(result.raw_report)
    except WorkflowValidationError as exc:
        raise WorkflowValidatorError('invalid_test_report') from exc
    revision = revision_payload(source_revision)
    expected = {'schema_version', 'source_revision', 'subject_digest', 'recipe', 'executor_id', 'exit_code',
                'timed_out', 'passed', 'junit_sha256', 'error_code', 'output_truncated', 'environment'}
    if set(value) != expected or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise WorkflowValidatorError('invalid_test_report')
    if any(type(value[key]) is not bool for key in ('passed', 'timed_out', 'output_truncated')) or (
            value['exit_code'] is not None and type(value['exit_code']) is not int):
        raise WorkflowValidatorError('invalid_test_report')
    recipe = value['recipe']
    if type(recipe) is not dict or set(recipe) != {'id', 'revision'} or type(recipe['revision']) is not int or not 1 <= recipe['revision'] <= 9007199254740991:
        raise WorkflowValidatorError('invalid_test_report')
    if type(result.recipe_revision) is not int or type(result.environment_snapshot) is not bytes or len(result.environment_snapshot) > 4096:
        raise WorkflowValidatorError('invalid_test_report')
    try:
        validate_reference(result.recipe_id)
        validate_reference(result.executor_id)
        if value['error_code'] is not None:
            validate_reference(value['error_code'])
    except WorkflowValidationError as exc:
        raise WorkflowValidatorError('invalid_test_report') from exc
    if value['source_revision'] != revision or result.source_revision_digest != canonical_digest(revision) or value['subject_digest'] != result.subject_digest:
        raise WorkflowValidatorError('test_subject_mismatch')
    try:
        if value['environment'] != _environment_value(result.environment_snapshot):
            raise WorkflowValidatorError('invalid_test_environment')
    except WorkflowValidationError as exc:
        raise WorkflowValidatorError('invalid_test_environment') from exc
    if (value['recipe'], value['executor_id'], value['exit_code'], value['timed_out'], value['passed']) != (
            {'id': result.recipe_id, 'revision': result.recipe_revision}, result.executor_id, result.exit_code, result.timed_out, result.passed):
        raise WorkflowValidatorError('invalid_test_report')
    if type(result.passed) is not bool or type(result.timed_out) is not bool or result.exit_code is not None and type(result.exit_code) is not int:
        raise WorkflowValidatorError('invalid_test_report')
    if result.timed_out != (value['error_code'] == 'timeout'):
        raise WorkflowValidatorError('invalid_test_report')
    junit_passed = _junit_passed(result.junit)
    junit_hash = hashlib.sha256(result.junit).hexdigest() if type(result.junit) is bytes else None
    if value['junit_sha256'] != junit_hash or result.junit is not None and type(result.junit) is not bytes:
        raise WorkflowValidatorError('invalid_test_report')
    if result.passed and (result.exit_code != 0 or result.timed_out or value['error_code'] is not None or not junit_passed):
        raise WorkflowValidatorError('invalid_test_report')
    return result.raw_report


test_report_bytes.__test__ = False
__all__ = ['CODING_DELIVERY_TEST_RECIPE', 'FixedTestRunner', 'TestRecipe', 'TestValidation', 'ReviewIdentity',
           'ReviewValidation', 'WorkflowValidatorError', 'revision_payload', 'test_report_bytes', 'validate_review_report']
