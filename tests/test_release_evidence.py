import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from tools.testing.release_evidence import EvidenceError, verify_release_evidence


def _write_evidence(tmp_path: Path):
    evidence = tmp_path / 'evidence'
    (evidence / 'browser').mkdir(parents=True)
    (evidence / 'browser' / 'page.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    matrix = {
        'schema_version': 1,
        'journeys': [{
            'id': 'SAMPLE-01',
            'layer': 'contract',
            'acceptance': 'sample is complete',
            'tests': ['tests/test_sample.py::test_sample'],
        }],
        'external_checks': [{'id': 'MODEL-LIVE'}],
    }
    checkout = tmp_path / 'checkout'
    matrix_path = checkout / 'docs/testing/journeys.json'
    matrix_path.parent.mkdir(parents=True)
    raw_matrix = json.dumps(matrix, separators=(',', ':')).encode()
    matrix_path.write_bytes(raw_matrix)
    report = {
        'schema_version': 1,
        'revision': '',
        'working_tree_dirty': False,
        'matrix_sha256': hashlib.sha256(raw_matrix).hexdigest(),
        'ci_status': 'passed',
        'journeys': [{
            'id': 'SAMPLE-01',
            'status': 'passed',
            'selectors': [{
                'selector': 'tests/test_sample.py::test_sample',
                'status': 'passed',
                'collected': 1,
                'passed': 1,
                'nonpassing_cases': [],
            }],
        }],
        'external_checks': [{'id': 'MODEL-LIVE', 'status': 'not_run'}],
    }
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', 'config', 'user.name', 'Evidence Test'], cwd=checkout, check=True)
    subprocess.run(['git', 'config', 'user.email', 'evidence@example.test'], cwd=checkout, check=True)
    subprocess.run(['git', 'add', 'docs/testing/journeys.json'], cwd=checkout, check=True)
    subprocess.run(['git', 'commit', '-qm', 'matrix'], cwd=checkout, check=True)
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout, check=True,
                              capture_output=True, text=True).stdout.strip()
    report['revision'] = revision
    (evidence / 'journeys.json').write_text(json.dumps(report))
    junit = ET.Element('testsuites')
    suite = ET.SubElement(junit, 'testsuite', tests='1', failures='0', errors='0', skipped='0')
    ET.SubElement(suite, 'testcase', classname='sample', name='test_sample')
    ET.ElementTree(junit).write(evidence / 'results.xml', encoding='utf-8', xml_declaration=True)
    return evidence, matrix_path, checkout, revision


def test_verifies_clean_complete_release_evidence(tmp_path):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    result = verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                    checkout_root=checkout)
    assert result == {'journeys': 1, 'selectors': 1, 'junit_cases': 1}


def test_accepts_root_and_nested_junit_summaries_with_subtests(tmp_path):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    junit_path = evidence / 'results.xml'
    root = ET.Element('testsuites', tests='2', failures='0', errors='0', skipped='0')
    suite = ET.SubElement(root, 'testsuite', tests='2', failures='0', errors='0', skipped='0')
    ET.SubElement(suite, 'testcase', classname='sample', name='test_sample')
    ET.ElementTree(root).write(junit_path, encoding='utf-8', xml_declaration=True)
    result = verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                     checkout_root=checkout)
    assert result['junit_cases'] == 1


def test_rejects_negative_junit_test_summary_even_without_suite_cases(tmp_path):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    junit_path = evidence / 'results.xml'
    root = ET.Element('testsuites')
    empty = ET.SubElement(root, 'testsuite', tests='-1', failures='0', errors='0', skipped='0')
    ET.SubElement(empty, 'properties')
    suite = ET.SubElement(root, 'testsuite', tests='1', failures='0', errors='0', skipped='0')
    ET.SubElement(suite, 'testcase', classname='sample', name='test_sample')
    ET.ElementTree(root).write(junit_path, encoding='utf-8', xml_declaration=True)
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'junit_invalid'


@pytest.mark.parametrize('layer', [[], {}, True])
def test_rejects_non_string_matrix_layer(tmp_path, layer):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    payload = json.loads(matrix.read_text())
    payload['journeys'][0]['layer'] = layer
    matrix.write_text(json.dumps(payload, separators=(',', ':')))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'matrix_invalid'


@pytest.mark.parametrize('check_id', [[], {}, True])
def test_rejects_non_string_external_check_ids(tmp_path, check_id):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    payload = json.loads(matrix.read_text())
    payload['external_checks'][0]['id'] = check_id
    matrix.write_text(json.dumps(payload, separators=(',', ':')))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'matrix_invalid'


@pytest.mark.parametrize('actual', [[{'id': []}], [{'id': {}, 'status': 'not_run'}], [None]])
def test_rejects_malformed_external_status_rows(tmp_path, actual):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    report_path = evidence / 'journeys.json'
    report = json.loads(report_path.read_text())
    report['external_checks'] = actual
    report_path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'external_status_invalid'


def test_rejects_invalid_png_when_another_screenshot_is_valid(tmp_path):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'broken.png').write_bytes(b'not a PNG')
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'


@pytest.mark.parametrize(('change', 'expected_code'), [
    ('stale_revision', 'revision_mismatch'),
    ('dirty_checkout', 'dirty_checkout'),
    ('matrix_digest', 'matrix_mismatch'),
    ('incomplete_selector', 'journey_incomplete'),
    ('junit_skip', 'junit_incomplete'),
    ('junit_failure', 'junit_incomplete'),
    ('junit_error', 'junit_incomplete'),
    ('missing_screenshot', 'browser_evidence_missing'),
    ('invalid_screenshot', 'browser_evidence_invalid'),
    ('invalid_matrix_version', 'matrix_invalid'),
    ('external_pass', 'external_status_invalid'),
    ('missing_selector', 'journey_incomplete'),
    ('report_schema_version', 'journey_report_invalid'),
    ('checkout_revision', 'revision_mismatch'),
    ('checkout_dirty', 'dirty_checkout'),
])
def test_rejects_release_evidence_that_does_not_meet_contract(tmp_path, change, expected_code):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    report_path = evidence / 'journeys.json'
    report = json.loads(report_path.read_text())
    junit_path = evidence / 'results.xml'
    junit = ET.parse(junit_path).getroot()
    if change == 'stale_revision':
        report['revision'] = 'b' * 40
    elif change == 'dirty_checkout':
        report['working_tree_dirty'] = True
    elif change == 'matrix_digest':
        report['matrix_sha256'] = '0' * 64
    elif change == 'incomplete_selector':
        report['journeys'][0]['selectors'][0]['passed'] = 0
        report['journeys'][0]['status'] = 'incomplete'
    elif change == 'junit_skip':
        suite = next(junit.iter('testsuite'))
        suite.set('skipped', '1')
        ET.SubElement(next(junit.iter('testcase')), 'skipped', message='unexpected skip')
    elif change == 'junit_failure':
        ET.SubElement(next(junit.iter('testcase')), 'failure', message='failure')
    elif change == 'junit_error':
        ET.SubElement(next(junit.iter('testcase')), 'error', message='error')
    elif change == 'missing_screenshot':
        (evidence / 'browser' / 'page.png').unlink()
    elif change == 'invalid_screenshot':
        (evidence / 'browser' / 'page.png').write_bytes(b'not a PNG')
    elif change == 'invalid_matrix_version':
        matrix_payload = json.loads(matrix.read_text())
        matrix_payload['schema_version'] = 2
        matrix.write_text(json.dumps(matrix_payload, separators=(',', ':')))
    elif change == 'external_pass':
        report['matrix_sha256'] = hashlib.sha256(matrix.read_bytes()).hexdigest()
        report['external_checks'][0]['status'] = 'passed'
    elif change == 'missing_selector':
        report['journeys'][0]['selectors'].clear()
    elif change == 'report_schema_version':
        report['schema_version'] = 99
    elif change == 'checkout_revision':
        report['revision'] = 'c' * 40
    elif change == 'checkout_dirty':
        (checkout / 'untracked.txt').write_text('dirty')
    report_path.write_text(json.dumps(report))
    ET.ElementTree(junit).write(junit_path, encoding='utf-8', xml_declaration=True)

    with pytest.raises(EvidenceError) as error:
        expected_revision = revision if change == 'stale_revision' else report['revision']
        verify_release_evidence(evidence, expected_revision=expected_revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == expected_code


@pytest.mark.parametrize(('attribute', 'value'), [
    ('tests', 'not-an-integer'),
    ('tests', 'true'),
    ('tests', '-1'),
    ('failures', '1.5'),
    ('errors', '-1'),
    ('skipped', 'false'),
])
def test_rejects_malformed_junit_summary_attributes(tmp_path, attribute, value):
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    junit_path = evidence / 'results.xml'
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', tests='1', failures='0', errors='0', skipped='0')
    suite.set(attribute, value)
    ET.SubElement(suite, 'testcase', classname='sample', name='test_sample')
    ET.ElementTree(root).write(junit_path, encoding='utf-8', xml_declaration=True)
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'junit_invalid'
