"""Optional adapter summaries have a bounded, descriptor-checked read path."""
from pathlib import Path
import json
import os

import pytest

from hub.domain.task_result import TestSummaryError as SummaryError
from tools.result_files import collect_test_summary

LIMIT = 20 * 1024


@pytest.mark.parametrize("extra", [0, 1])
def test_summary_source_exact_byte_limit(tmp_path: Path, extra: int) -> None:
    data = b'{"framework":"pytest","passed":1}'
    (tmp_path / "test-results.json").write_bytes(data + b" " * (LIMIT - len(data) + extra))
    if extra:
        with pytest.raises(ValueError, match="test_summary_too_large"):
            collect_test_summary(tmp_path)
        return
    assert collect_test_summary(tmp_path) == {"framework": "pytest", "passed": 1}


@pytest.mark.parametrize("data", [b"", b"{", b"\xff", b"[]", b"[" * 9000 + b"]" * 9000,
                                  b'{"unknown":NaN}'],
                         ids=["empty", "invalid-json", "invalid-utf8", "not-object", "excessive-nesting", "non-json-constant"])
def test_invalid_present_summary_is_not_silently_missing(tmp_path: Path, data: bytes) -> None:
    (tmp_path / "test-results.json").write_bytes(data)
    with pytest.raises(ValueError, match="invalid_test_summary"):
        collect_test_summary(tmp_path)


@pytest.mark.parametrize(("data", "cause_type"), [
    (b"{", json.JSONDecodeError), (b"\xff", UnicodeDecodeError),
])
def test_invalid_summary_retains_decode_cause(tmp_path: Path, data: bytes, cause_type: type[Exception]) -> None:
    (tmp_path / "test-results.json").write_bytes(data)
    with pytest.raises(SummaryError) as error:
        collect_test_summary(tmp_path)
    assert isinstance(error.value.__cause__, cause_type)


def test_summary_read_error_retains_io_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "test-results.json"
    target.write_text('{"passed":1}')
    real_open = os.open

    def denied_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path) == target:
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied_open)
    with pytest.raises(SummaryError, match="test_summary_read") as error:
        collect_test_summary(tmp_path)
    assert isinstance(error.value.__cause__, PermissionError)

def test_absent_summary_and_symlink_remain_absent(tmp_path: Path) -> None:
    assert collect_test_summary(tmp_path) is None
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"passed": 12}))
    (tmp_path / "test-results.json").symlink_to(outside)
    assert collect_test_summary(tmp_path) is None


def test_alternate_summary_filename_remains_supported(tmp_path: Path) -> None:
    (tmp_path / "test_results.json").write_text(json.dumps({"passed": 2, "unknown": "discard"}))
    assert collect_test_summary(tmp_path) == {"passed": 2}


@pytest.mark.parametrize("replacement", ["symlink", "regular"])
def test_replaced_report_is_rejected_before_read_and_descriptor_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str) -> None:
    target = tmp_path / "test-results.json"
    target.write_text('{"passed":1}')
    outside = tmp_path / "outside.json"
    outside.write_text('{"passed":999}')
    real_open = os.open
    descriptors: list[int] = []

    def swapped_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path) == target:
            target.rename(tmp_path / "original.json")
            if replacement == "symlink":
                target.symlink_to(outside)
            else:
                target.write_text(outside.read_text())
        descriptor = real_open(path, flags, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", swapped_open)
    with pytest.raises(ValueError, match="test_summary_(read|changed)"):
        collect_test_summary(tmp_path)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_growing_report_stops_at_byte_limit_and_closes_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "test-results.json"
    target.write_text('{"passed":1}')
    real_stat = os.fstat
    descriptors: list[int] = []

    def growing_stat(descriptor: int) -> os.stat_result:
        original = real_stat(descriptor)
        if not descriptors:
            with target.open("ab") as stream:
                stream.write(b" " * (LIMIT + 1))
        descriptors.append(descriptor)
        return original

    monkeypatch.setattr(os, "fstat", growing_stat)
    with pytest.raises(ValueError, match="test_summary_too_large"):
        collect_test_summary(tmp_path)
    with pytest.raises(OSError):
        real_stat(descriptors[0])
