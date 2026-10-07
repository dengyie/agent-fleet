"""Verify a clean CI test artifact against the checkout and journey contract."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LAYERS = {"browser", "integration", "contract"}


class EvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _read_json(path: Path, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise EvidenceError(code) from None
    if not isinstance(value, dict):
        raise EvidenceError(code)
    return value


def _verify_checkout(root: Path, expected_revision: str) -> None:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
            check=True, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        raise EvidenceError("checkout_unavailable") from None
    if revision != expected_revision:
        raise EvidenceError("revision_mismatch")
    if status:
        raise EvidenceError("dirty_checkout")


def _validate_matrix(matrix: dict) -> None:
    if type(matrix.get("schema_version")) is not int or matrix["schema_version"] != 1:
        raise EvidenceError("matrix_invalid")
    journeys = matrix.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        raise EvidenceError("matrix_invalid")
    seen_journeys: set[str] = set()
    for journey in journeys:
        if not isinstance(journey, dict):
            raise EvidenceError("matrix_invalid")
        journey_id = journey.get("id")
        tests = journey.get("tests")
        if (not isinstance(journey_id, str) or not journey_id or journey_id in seen_journeys
                or not isinstance(journey.get("acceptance"), str) or not journey["acceptance"]
                or not isinstance(journey.get("layer"), str)
                or journey["layer"] not in _LAYERS
                or not isinstance(tests, list) or not tests
                or any(not isinstance(test, str) or not test.startswith("tests/")
                       or "::test_" not in test for test in tests)
                or len(set(tests)) != len(tests)):
            raise EvidenceError("matrix_invalid")
        seen_journeys.add(journey_id)
    external = matrix.get("external_checks", [])
    if not isinstance(external, list):
        raise EvidenceError("matrix_invalid")
    external_ids: set[str] = set()
    for check in external:
        if not isinstance(check, dict):
            raise EvidenceError("matrix_invalid")
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id or check_id in external_ids:
            raise EvidenceError("matrix_invalid")
        external_ids.add(check_id)


def _verify_journeys(matrix: dict, report: dict) -> tuple[int, int]:
    expected_rows = matrix.get("journeys")
    actual_rows = report.get("journeys")
    if not isinstance(expected_rows, list) or not expected_rows or not isinstance(actual_rows, list):
        raise EvidenceError("journey_incomplete")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in actual_rows):
        raise EvidenceError("journey_incomplete")
    expected_by_id = {row["id"]: row for row in expected_rows}
    actual_by_id = {row["id"]: row for row in actual_rows}
    if (len(expected_by_id) != len(expected_rows) or len(actual_by_id) != len(actual_rows)
            or set(expected_by_id) != set(actual_by_id)):
        raise EvidenceError("journey_incomplete")
    selectors_total = 0
    for journey_id, expected in expected_by_id.items():
        actual = actual_by_id[journey_id]
        expected_selectors = expected.get("tests")
        selectors = actual.get("selectors")
        if (actual.get("status") != "passed" or not isinstance(expected_selectors, list)
                or not expected_selectors or not isinstance(selectors, list)
                or len(selectors) != len(expected_selectors)):
            raise EvidenceError("journey_incomplete")
        if any(not isinstance(row, dict) or not isinstance(row.get("selector"), str)
               for row in selectors):
            raise EvidenceError("journey_incomplete")
        selector_map = {row["selector"]: row for row in selectors}
        if len(selector_map) != len(selectors) or set(selector_map) != set(expected_selectors):
            raise EvidenceError("journey_incomplete")
        for selector in expected_selectors:
            row = selector_map[selector]
            collected, passed = row.get("collected"), row.get("passed")
            if (row.get("status") != "passed" or isinstance(collected, bool)
                    or isinstance(passed, bool) or not isinstance(collected, int)
                    or not isinstance(passed, int) or collected <= 0 or passed != collected
                    or row.get("nonpassing_cases") != []):
                raise EvidenceError("journey_incomplete")
        selectors_total += len(selectors)
    return len(expected_rows), selectors_total


def _verify_junit(path: Path) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        raise EvidenceError("junit_invalid") from None
    if root.tag not in {"testsuites", "testsuite"}:
        raise EvidenceError("junit_invalid")
    cases = list(root.iter("testcase"))
    if not cases or list(root.iter("failure")) or list(root.iter("error")) or list(root.iter("skipped")):
        raise EvidenceError("junit_incomplete")
    # pytest's JUnit tests includes subtests, which are counted but not
    # emitted as additional testcase elements. Require the summary count to
    # cover explicit cases; failures, errors and skips must remain zero.
    summaries = list(root.iter("testsuite"))
    if root.tag == "testsuites":
        summaries.insert(0, root)
    for suite in summaries:
        suite_cases = list(suite.iter("testcase"))
        raw_tests = suite.get("tests")
        if raw_tests is not None:
            try:
                test_count = int(raw_tests)
            except (TypeError, ValueError):
                raise EvidenceError("junit_invalid") from None
            if test_count < 0:
                raise EvidenceError("junit_invalid")
            if test_count < len(suite_cases):
                raise EvidenceError("junit_incomplete")
        for attribute in ("failures", "errors", "skipped"):
            raw_count = suite.get(attribute)
            if raw_count is not None:
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    raise EvidenceError("junit_invalid") from None
                if count < 0:
                    raise EvidenceError("junit_invalid")
                if count != 0:
                    raise EvidenceError("junit_incomplete")
    return len(cases)


def _verify_external_checks(matrix: dict, report: dict) -> None:
    expected = matrix.get("external_checks", [])
    actual = report.get("external_checks")
    if not isinstance(expected, list) or not isinstance(actual, list):
        raise EvidenceError("external_status_invalid")
    if any(not isinstance(row, dict) for row in actual):
        raise EvidenceError("external_status_invalid")
    actual_ids = [row.get("id") for row in actual]
    expected_ids = [row["id"] for row in expected]
    if (any(not isinstance(check_id, str) for check_id in actual_ids)
            or len(actual_ids) != len(expected_ids)
            or len(set(actual_ids)) != len(actual_ids)
            or set(actual_ids) != set(expected_ids)
            or any(row.get("status") != "not_run" for row in actual)):
        raise EvidenceError("external_status_invalid")


def verify_release_evidence(evidence_dir: Path | str, *, expected_revision: str,
                            matrix_path: Path | str, checkout_root: Path | str | None = None) -> dict[str, int]:
    """Verify immutable CI evidence and return bounded counts for reporting."""
    if not isinstance(expected_revision, str) or not _REVISION.fullmatch(expected_revision):
        raise EvidenceError("revision_invalid")
    evidence = Path(evidence_dir)
    matrix_file = Path(matrix_path)
    root = Path(checkout_root) if checkout_root else matrix_file.parent.parent
    try:
        matrix_raw = matrix_file.read_bytes()
        matrix = json.loads(matrix_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise EvidenceError("matrix_invalid") from None
    if not isinstance(matrix, dict):
        raise EvidenceError("matrix_invalid")
    _validate_matrix(matrix)
    digest = hashlib.sha256(matrix_raw).hexdigest()
    report = _read_json(evidence / "journeys.json", "journey_report_invalid")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise EvidenceError("journey_report_invalid")
    if report.get("revision") != expected_revision:
        raise EvidenceError("revision_mismatch")
    if report.get("working_tree_dirty") is not False:
        raise EvidenceError("dirty_checkout")
    report_digest = report.get("matrix_sha256")
    if (not isinstance(report_digest, str) or not _DIGEST.fullmatch(report_digest)
            or report_digest != digest):
        raise EvidenceError("matrix_mismatch")
    if report.get("journeys") is not None:
        rows = report["journeys"]
        if not isinstance(rows, list):
            raise EvidenceError("journey_incomplete")
        # Keep validation order useful: a corrupt matrix is a matrix error,
        # even if it also invalidates the report's matrix digest.
    if report.get("ci_status") != "passed":
        raise EvidenceError("journey_incomplete")
    journey_count, selector_count = _verify_journeys(matrix, report)
    junit_cases = _verify_junit(evidence / "results.xml")
    _verify_external_checks(matrix, report)
    browser_dir = evidence / "browser"
    screenshots = [path for path in browser_dir.rglob("*.png") if path.is_file()] if browser_dir.is_dir() else []
    if not screenshots:
        raise EvidenceError("browser_evidence_missing")
    try:
        for path in screenshots:
            with path.open("rb") as image:
                if image.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise EvidenceError("browser_evidence_invalid")
    except OSError:
        raise EvidenceError("browser_evidence_invalid") from None
    _verify_checkout(root, expected_revision)
    return {"journeys": journey_count, "selectors": selector_count, "junit_cases": junit_cases}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--matrix", type=Path, default=Path("docs/testing/journeys.json"))
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        result = verify_release_evidence(
            args.evidence, expected_revision=args.revision, matrix_path=args.matrix,
            checkout_root=args.checkout)
    except (EvidenceError, OSError) as exc:
        code = exc.code if isinstance(exc, EvidenceError) else "evidence_unavailable"
        parser.exit(1, f"release evidence rejected: {code}\n")
    print("Verified {journeys} journeys, {selectors} selectors, and {junit_cases} JUnit cases; external checks remain not_run".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
