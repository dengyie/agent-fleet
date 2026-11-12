import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tracemalloc
from types import TracebackType
from typing import BinaryIO, TextIO
import xml.etree.ElementTree as ET
import zlib

import pytest

from tools.testing.release_evidence import EvidenceError, verify_release_evidence
from tools.testing.png_evidence import CHUNK_BYTES, verify_png


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_HEADER = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)
PNG_PIXELS = b'\x00\xff\x00\x00\xff'


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


def _png_image(*, header: bytes = PNG_HEADER, pixels: bytes = PNG_PIXELS) -> bytes:
    return (PNG_SIGNATURE + _png_chunk(b'IHDR', header)
            + _png_chunk(b'IDAT', zlib.compress(pixels)) + _png_chunk(b'IEND', b''))


def _write_evidence(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    evidence = tmp_path / 'evidence'
    (evidence / 'browser').mkdir(parents=True)
    (evidence / 'browser' / 'page.png').write_bytes(_png_image())
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


@pytest.mark.parametrize('payload', [
    pytest.param(PNG_SIGNATURE, id='signature-only'),
    pytest.param(PNG_SIGNATURE + b'fixture', id='signature-and-garbage'),
    pytest.param(_png_image()[:20], id='truncated-header'),
    pytest.param(_png_image()[:-1], id='truncated-end-crc'),
    pytest.param(_png_image()[:-12], id='missing-end'),
    pytest.param(_png_image() + b'trailing-data', id='trailing-data'),
    pytest.param(_png_image()[:-1] + bytes([_png_image()[-1] ^ 1]), id='crc-mismatch'),
    pytest.param(PNG_SIGNATURE + _png_chunk(b'IHDR', PNG_HEADER)
                 + _png_chunk(b'IDAT', b'invalid-deflate') + _png_chunk(b'IEND', b''),
                 id='invalid-compressed-data'),
])
def test_rejects_incomplete_or_corrupt_png_evidence(tmp_path: Path, payload: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(payload)
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'


@pytest.mark.parametrize('header', [
    pytest.param(struct.pack('>IIBBBBB', 0, 1, 8, 6, 0, 0, 0), id='zero-width'),
    pytest.param(struct.pack('>IIBBBBB', 1, 0, 8, 6, 0, 0, 0), id='zero-height'),
    pytest.param(struct.pack('>IIBBBBB', 0x80000000, 1, 8, 6, 0, 0, 0), id='invalid-width'),
    pytest.param(struct.pack('>IIBBBBB', 1, 0x80000000, 8, 6, 0, 0, 0), id='invalid-height'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 4, 6, 0, 0, 0), id='invalid-depth'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 1, 0, 0, 0), id='invalid-color'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 6, 1, 0, 0), id='invalid-compression'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 1, 0), id='invalid-filter-method'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 2), id='invalid-interlace'),
])
def test_rejects_invalid_png_headers(tmp_path: Path, header: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(_png_image(header=header))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'
    assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.parametrize('chunks', [
    pytest.param(_png_chunk(b'IEND', b''), id='missing-image-data'),
    pytest.param(_png_chunk(b'IHDR', PNG_HEADER), id='duplicate-header'),
    pytest.param(_png_chunk(b'IDAT', b'') + _png_chunk(b'IEND', b''), id='empty-image-data'),
    pytest.param(_png_chunk(b'IDAT', zlib.compress(PNG_PIXELS))
                 + _png_chunk(b'IEND', b'x'), id='nonempty-end'),
    pytest.param(_png_chunk(b'IDAT', zlib.compress(PNG_PIXELS))
                 + _png_chunk(b'PLTE', b'\x00\x00\x00') + _png_chunk(b'IEND', b''),
                 id='palette-after-image-data'),
    pytest.param(_png_chunk(b'IDAT', zlib.compress(PNG_PIXELS)[:2])
                 + _png_chunk(b'tEXt', b'key\x00value')
                 + _png_chunk(b'IDAT', zlib.compress(PNG_PIXELS)[2:])
                 + _png_chunk(b'IEND', b''), id='nonconsecutive-image-data'),
    pytest.param(_png_chunk(b'ABCD', b''), id='unknown-critical-chunk'),
    pytest.param(_png_chunk(b'tE1t', b''), id='invalid-chunk-name'),
    pytest.param(_png_chunk(b'tExt', b''), id='invalid-reserved-bit'),
    pytest.param(struct.pack('>I4s', 0x80000000, b'IDAT'), id='invalid-chunk-length'),
    pytest.param(_png_chunk(b'PLTE', b''), id='empty-palette'),
    pytest.param(_png_chunk(b'PLTE', b'\x00\x00'), id='incomplete-palette-entry'),
    pytest.param(_png_chunk(b'PLTE', b'\x00' * 771), id='oversized-palette'),
    pytest.param(_png_chunk(b'PLTE', b'\x00' * 3) + _png_chunk(b'PLTE', b'\x00' * 3),
                 id='duplicate-palette'),
])
def test_rejects_invalid_png_chunk_order(tmp_path: Path, chunks: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', PNG_HEADER) + chunks)
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'


@pytest.mark.parametrize(('header', 'palette'), [
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 3, 0, 0, 0), b'', id='indexed-without-palette'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0), _png_chunk(b'PLTE', b'\x00' * 3),
                 id='palette-with-grayscale'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 4, 0, 0, 0), _png_chunk(b'PLTE', b'\x00' * 3),
                 id='palette-with-grayscale-alpha'),
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 1, 3, 0, 0, 0), _png_chunk(b'PLTE', b'\x00' * 9),
                 id='palette-exceeds-index-depth'),
])
def test_rejects_invalid_png_palette(tmp_path: Path, header: bytes, palette: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', header) + palette
        + _png_chunk(b'IDAT', zlib.compress(b'\x00\x00')) + _png_chunk(b'IEND', b''))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'


@pytest.mark.parametrize('compressed', [
    pytest.param(zlib.compress(PNG_PIXELS)[:-1], id='incomplete-zlib'),
    pytest.param(zlib.compress(b''), id='no-scanline'),
    pytest.param(zlib.compress(PNG_PIXELS[:-1]), id='short-scanline'),
    pytest.param(zlib.compress(PNG_PIXELS + b'\x00'), id='long-scanline'),
    pytest.param(zlib.compress(b'\x05' + PNG_PIXELS[1:]), id='invalid-row-filter'),
    pytest.param(zlib.compress(PNG_PIXELS) + b'extra', id='trailing-compressed-data'),
    pytest.param(zlib.compress(PNG_PIXELS) + zlib.compress(PNG_PIXELS), id='second-zlib-stream'),
])
def test_rejects_invalid_png_pixel_stream(tmp_path: Path, compressed: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', PNG_HEADER)
        + _png_chunk(b'IDAT', compressed) + _png_chunk(b'IEND', b''))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'


@pytest.mark.parametrize(('color', 'depth', 'row_bytes'), [
    (0, 1, 2), (0, 2, 2), (0, 4, 2), (0, 8, 2), (0, 16, 3),
    (2, 8, 4), (2, 16, 7), (3, 1, 2), (3, 2, 2), (3, 4, 2), (3, 8, 2),
    (4, 8, 3), (4, 16, 5), (6, 8, 5), (6, 16, 9),
])
def test_accepts_valid_png_formats(tmp_path: Path, color: int, depth: int, row_bytes: int) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    header = struct.pack('>IIBBBBB', 1, 1, depth, color, 0, 0, 0)
    palette = _png_chunk(b'PLTE', b'\x00' * 3) if color == 3 else b''
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', header) + palette
        + _png_chunk(b'IDAT', zlib.compress(b'\x00' * row_bytes)) + _png_chunk(b'IEND', b''))
    result = verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                     checkout_root=checkout)
    assert result == {'journeys': 1, 'selectors': 1, 'junit_cases': 1}


@pytest.mark.parametrize(('header', 'pixels'), [
    pytest.param(struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 1), PNG_PIXELS, id='one-pixel'),
    pytest.param(struct.pack('>IIBBBBB', 8, 8, 8, 0, 0, 0, 1), b'\x00' * 79, id='all-seven-passes'),
])
def test_accepts_interlaced_png_evidence(tmp_path: Path, header: bytes, pixels: bytes) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(_png_image(header=header, pixels=pixels))
    assert verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                   checkout_root=checkout)['junit_cases'] == 1


@pytest.mark.parametrize('split', [1, 2, 12])
def test_accepts_fragmented_png_data_and_ancillary_chunks(tmp_path: Path, split: int) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    compressed = zlib.compress(PNG_PIXELS)
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', PNG_HEADER)
        + _png_chunk(b'tEXt', b'fixture\x00temporary')
        + _png_chunk(b'PLTE', b'\x00' * 3)
        + _png_chunk(b'IDAT', b'') + _png_chunk(b'IDAT', compressed[:split])
        + _png_chunk(b'IDAT', compressed[split:]) + _png_chunk(b'IDAT', b'')
        + _png_chunk(b'vpAg', b'fixture') + _png_chunk(b'IEND', b''))
    assert verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                   checkout_root=checkout)['journeys'] == 1


class _PngReader:
    def __init__(self, source: BinaryIO, *, partial: int = CHUNK_BYTES,
                 error: OSError | None = None) -> None:
        self.source = source
        self.partial = partial
        self.error = error
        self.max_request = 0
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= CHUNK_BYTES, 'PNG reads must be bounded'
        self.max_request = max(self.max_request, size)
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.source.read(min(size, self.partial))

    def __enter__(self) -> '_PngReader':
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.source.close()


@pytest.mark.parametrize('partial', [7, CHUNK_BYTES])
def test_png_file_reads_are_bounded_and_allow_partial_results(tmp_path: Path, partial: int) -> None:
    # Both stored IDAT and decoded scanlines exceed the read/output window.
    row = b'\x00' + bytes(range(256)) * 256
    path = tmp_path / 'wide.png'
    path.write_bytes(PNG_SIGNATURE + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', 16384, 2, 8, 6, 0, 0, 0))
                     + _png_chunk(b'IDAT', zlib.compress(row * 2, level=0)) + _png_chunk(b'IEND', b''))
    with _PngReader(path.open('rb'), partial=partial) as reader:
        verify_png(reader)
    assert reader.source.closed
    assert reader.max_request == CHUNK_BYTES
    assert reader.reads > 2


@pytest.mark.parametrize('parent_noise_bytes', [0, 3 * 1024 * 1024])
def test_png_decoded_memory_is_independent_of_image_size(tmp_path: Path, parent_noise_bytes: int) -> None:
    # Sixteen MiB of pixels are checked without retaining decoded rows.
    row = b'\x00' + b'\xff\x00\x00\xff' * 1024
    path = tmp_path / 'tall.png'
    path.write_bytes(PNG_SIGNATURE + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1024, 4096, 8, 6, 0, 0, 0))
                     + _png_chunk(b'IDAT', zlib.compress(row * 4096)) + _png_chunk(b'IEND', b''))
    tracemalloc.start()
    try:
        unrelated = bytearray(parent_noise_bytes)
        probe = Path(__file__).resolve().parent / 'fixtures/png_memory_probe.py'
        result = subprocess.run([sys.executable, '-I', str(probe), str(path)],
                                capture_output=True, text=True, timeout=20)
    finally:
        tracemalloc.stop()
    assert result.returncode == 0, result.stdout + result.stderr
    measurement = json.loads(result.stdout)
    assert measurement['pid'] != os.getpid()
    assert measurement['limit_bytes'] == 2 * 1024 * 1024
    assert measurement['peak_bytes'] < measurement['limit_bytes']
    assert len(unrelated) == parent_noise_bytes


def test_rejects_invalid_png_filter_across_decode_window(tmp_path: Path) -> None:
    row = b'\x00' + b'\x00' * (16384 * 4)
    path = tmp_path / 'bad-filter.png'
    path.write_bytes(PNG_SIGNATURE + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', 16384, 2, 8, 6, 0, 0, 0))
                     + _png_chunk(b'IDAT', zlib.compress(row + b'\x05' + row[1:])) + _png_chunk(b'IEND', b''))
    with path.open('rb') as source, pytest.raises(ValueError, match='invalid PNG row filter'):
        verify_png(source)


@pytest.mark.parametrize('filter_offset', [2, 4, 7, 13, 23, 43, 70])
def test_rejects_invalid_png_filter_in_interlaced_pass(tmp_path: Path, filter_offset: int) -> None:
    pixels = bytearray(79)
    pixels[filter_offset] = 5
    path = tmp_path / 'interlaced-filter.png'
    path.write_bytes(_png_image(header=struct.pack('>IIBBBBB', 8, 8, 8, 0, 0, 0, 1), pixels=bytes(pixels)))
    with path.open('rb') as source, pytest.raises(ValueError, match='invalid PNG row filter'):
        verify_png(source)


@pytest.mark.parametrize('valid', [True, False])
def test_release_png_descriptor_closes_after_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       valid: bool) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    if not valid:
        (evidence / 'browser' / 'page.png').write_bytes(_png_image()[:-1])
    original_open = Path.open
    readers: list[_PngReader] = []

    def observed_open(path: Path, *args: object, **kwargs: object) -> BinaryIO | TextIO | _PngReader:
        source = original_open(path, *args, **kwargs)
        if path.suffix != '.png':
            return source
        reader = _PngReader(source)
        readers.append(reader)
        return reader

    monkeypatch.setattr(Path, 'open', observed_open)
    if valid:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    else:
        with pytest.raises(EvidenceError):
            verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                    checkout_root=checkout)
    assert len(readers) == 1
    assert readers[0].source.closed


@pytest.mark.parametrize('stage', ['open', 'read'])
def test_release_png_error_preserves_os_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            stage: str) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    original_open = Path.open
    failure = OSError('fixture PNG I/O failure')
    readers: list[_PngReader] = []

    def failing_open(path: Path, *args: object, **kwargs: object) -> BinaryIO | TextIO | _PngReader:
        if path.suffix != '.png':
            return original_open(path, *args, **kwargs)
        if stage == 'open':
            raise failure
        reader = _PngReader(original_open(path, *args, **kwargs), error=failure)
        readers.append(reader)
        return reader

    monkeypatch.setattr(Path, 'open', failing_open)
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'
    assert error.value.__cause__ is failure
    assert all(reader.source.closed for reader in readers)


def test_release_png_error_preserves_zlib_cause(tmp_path: Path) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(
        PNG_SIGNATURE + _png_chunk(b'IHDR', PNG_HEADER)
        + _png_chunk(b'IDAT', b'invalid-deflate') + _png_chunk(b'IEND', b''))
    with pytest.raises(EvidenceError) as error:
        verify_release_evidence(evidence, expected_revision=revision, matrix_path=matrix,
                                checkout_root=checkout)
    assert error.value.code == 'browser_evidence_invalid'
    assert isinstance(error.value.__cause__, ValueError)
    assert isinstance(error.value.__cause__.__cause__, zlib.error)


def test_release_cli_rejects_corrupt_png(tmp_path: Path) -> None:
    evidence, matrix, checkout, revision = _write_evidence(tmp_path)
    (evidence / 'browser' / 'page.png').write_bytes(PNG_SIGNATURE)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'tools.testing.release_evidence', '--evidence', str(evidence),
         '--matrix', str(matrix), '--checkout', str(checkout), '--revision', revision],
        cwd=checkout, env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == ''
    assert result.stderr == 'release evidence rejected: browser_evidence_invalid\n'
