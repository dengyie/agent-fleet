"""Independent size, depth and hostile-input boundaries for the domain layer."""
from __future__ import annotations

import hashlib
import json
import sys

import pytest

from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, canonical_json, dispatch_token
from hub.domain.workflow_json import decode_snapshot


@pytest.mark.parametrize("value", ["x" * 65534, "中" * 21844 + "xx", "\n" * 32767])
def test_canonical_limit_counts_exact_encoded_bytes(value: str) -> None:
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(expected) == 65536
    assert canonical_json(value) == expected
    assert canonical_digest(value) == hashlib.sha256(expected).hexdigest()
    with pytest.raises(WorkflowValidationError, match="json_too_large"):
        canonical_digest(value + "x")


def test_containers_have_the_same_exact_byte_boundary() -> None:
    valid = {"x": "a" * 65528}
    assert len(canonical_json(valid)) == 65536
    with pytest.raises(WorkflowValidationError, match="json_too_large"):
        canonical_json({"x": "a" * 65529})
    with pytest.raises(WorkflowValidationError, match="json_too_large"):
        canonical_digest([None] * 65536)


def test_max_depth_shared_references_and_cycles_are_bounded() -> None:
    value: object = None
    for _ in range(32):
        value = [value]
    assert canonical_json(value) == b"[" * 32 + b"null" + b"]" * 32
    with pytest.raises(WorkflowValidationError, match="json_too_deep"):
        canonical_json([value])
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(WorkflowValidationError, match="json_too_deep"):
        canonical_digest(cycle)
    shared = {"a": 1}
    assert canonical_json([shared, shared]) == b'[{"a":1},{"a":1}]'


def test_depth_scanner_ignores_escaped_quotes_brackets_and_backslashes() -> None:
    value = {"text": '[{"quote": "\\\"[]{}"}]' * 100}
    assert decode_snapshot(json.dumps(value).encode("utf-8")) == value


def test_raw_byte_limit_applies_before_whitespace_is_discarded() -> None:
    assert decode_snapshot(b"{}" + b" " * 65534) == {}
    with pytest.raises(WorkflowValidationError, match="json_too_large"):
        decode_snapshot(b"{}" + b" " * 65535)


def test_decimal_generation_overflow_keeps_a_typed_error_and_cause() -> None:
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if limit == 0:
        expected = hashlib.sha256(("workflow-v1\0owner\0workflow\0step\0" + "1" + "0" * 4300).encode("utf-8")).hexdigest()
        assert dispatch_token("owner", "workflow", "step", 10 ** 4300) == expected
        return
    with pytest.raises(WorkflowValidationError, match="invalid_generation") as error:
        dispatch_token("owner", "workflow", "step", 10 ** limit)
    assert isinstance(error.value.__cause__, ValueError)


def test_multibyte_identifiers_are_bounded_without_changing_identity() -> None:
    assert len(dispatch_token("中" * 42 + "xx", "workflow", "step", 1)) == 64
    with pytest.raises(WorkflowValidationError, match="invalid_identifier"):
        dispatch_token("中" * 43, "workflow", "step", 1)
    with pytest.raises(WorkflowValidationError) as error:
        canonical_digest({"invalid": "\ud800"})
    assert isinstance(error.value.__cause__, UnicodeEncodeError)
