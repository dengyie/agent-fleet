# Workflow 领域基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `executing-plans` 逐任务实施，先保留失败证据再实现。以 checkbox 跟踪进度。本计划 Task 1–3 已实施并验证；F1.3 与 F2.1 的后续实现见[实施进度](../../platform/workflow-platform/implementation-progress.md)。

**Goal:** 实现稳定派发身份、受控步骤迁移、当前版本证据和交付状态四项纯领域基础。

**Architecture:** 三个无 I/O 模块，不启动 worker，不打开数据库，不改变现有 Run/Task 状态。后续应用服务负责鉴权、核对事实、事务与实际派发；这些函数提供统一、可测试的规则。

**Tech Stack:** Python 3.10+ 标准库；示例测试用 unittest，可由现有 pytest 收集。

## Global Constraints

- 对应[路线图](2026-10-20-workflow-platform-roadmap.md) F1.1–F1.2；F1.3 完整 WorkContract 解析、持久化、API、UI、节点和 MCP 均不在本计划内。
- 以[执行契约](../specs/2026-10-20-workflow-execution-contract.md)为准。执行 succeeded 不代表 delivered；人工接受不能使失败验证通过。
- generation 从 1 开始，bool、浮点、数字字符串均拒绝；coordinator_epoch 不进入派发身份。
- unknown 只允许在应用服务完成受控核对后迁移为已证实终态。新一代是新记录，不重开旧终态。
- 每个摘要对象最多 64 KiB UTF-8、嵌套深度 32；内部 owner/workflow/step ID 最多 128 UTF-8 bytes，禁止空值和 NUL。
- 单 subject 最多 16 个必需 validator、32 条当前 evidence。validator_ref 表示固定 `catalog-id@revision`；目录解析与权限检查留给 F1.3/F2。
- 以下代码是完整的首批实现样例，不是整个工作流引擎。实现前检查工作树/分支；不得编辑有其他未提交工作的主工作树。
- 文件 UTF-8 无 BOM；不安装新依赖，不联网、不删除文件。本任务不执行下方的提交命令。

## 文件与调用关系

| 新文件 | 责任 | 后续调用方 |
|---|---|---|
| `hub/domain/workflow_identity.py` | JSON 摘要及固定派发 token | 合同解析、repository、dispatcher |
| `hub/domain/workflow.py` | 状态集合与允许迁移 | coordinator/control service |
| `hub/domain/workflow_evidence.py` | 当前证据聚合、后继放行、交付投影 | coordinator、HTTP DTO、UI |
| `tests/test_workflow_identity.py` | 独立固定向量与坏输入 | pytest / unittest |
| `tests/test_workflow_state.py` | 合法路径与禁止迁移 | pytest / unittest |
| `tests/test_workflow_evidence.py` | 旧版本、冲突、缺失与结果优先级 | pytest / unittest |

## Task 1：稳定身份与规范摘要

**Files:** Create `hub/domain/workflow_identity.py`；Test `tests/test_workflow_identity.py`。

**Interfaces:**

- Consumes：JSON-compatible Python 值；已完成鉴权/归属解析的 ID；正整数 generation。
- Produces：`canonical_digest(value: object) -> str`；`dispatch_token(owner_id: str, workflow_id: str, step_key: str, generation: int) -> str`，均返回小写 SHA-256 hex；坏输入抛 `ValueError`。

- [x] **Step 1：写入完整失败测试。** 测试的预期向量独立写死，不从被测函数反推。

`tests/test_workflow_identity.py`：

~~~python
import hashlib
import unittest

from hub.domain.workflow_identity import canonical_digest, dispatch_token


class WorkflowIdentityTests(unittest.TestCase):
    def test_digest_has_fixed_utf8_representation(self):
        expected = hashlib.sha256('{"a":"中","b":2}'.encode("utf-8")).hexdigest()
        self.assertEqual(canonical_digest({"b": 2, "a": "中"}), expected)
        self.assertEqual(canonical_digest({"a": "中", "b": 2}), expected)

    def test_dispatch_has_fixed_vector_and_generation_boundary(self):
        first = dispatch_token("owner-a", "wf-a", "develop", 1)
        self.assertEqual(
            first,
            "a1d22a0e30bac2e280aaaf9d1951bee06c971148966ac02f2c7febe6f0f2e9e5",
        )
        self.assertNotEqual(first, dispatch_token("owner-a", "wf-a", "develop", 2))
        self.assertNotEqual(first, dispatch_token("owner-b", "wf-a", "develop", 1))

    def test_generation_is_an_exact_positive_integer(self):
        for value in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dispatch_token("owner-a", "wf-a", "develop", value)

    def test_identifiers_cannot_alias_nul_delimited_parts(self):
        for value in ("", "owner\x00other", "x" * 129, "\ud800", None):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                dispatch_token(value, "wf-a", "develop", 1)

    def test_non_json_values_and_invalid_unicode_are_rejected(self):
        for value in (float("nan"), float("inf"), {1: "x"}, {"x": {1}}, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                canonical_digest(value)

    def test_digest_has_byte_and_depth_limits(self):
        with self.assertRaises(ValueError):
            canonical_digest("x" * 65536)
        value = None
        for _ in range(34):
            value = [value]
        with self.assertRaises(ValueError):
            canonical_digest(value)


if __name__ == "__main__":
    unittest.main()
~~~

- [x] **Step 2：运行失败验证。**

运行 `PYTHONPATH=. python3 -m unittest discover -s tests -p test_workflow_identity.py -v`。实现前预期为 `ModuleNotFoundError: hub.domain.workflow_identity`；若出现其他失败，先定位环境，不能当作有效红灯。

- [x] **Step 3：创建最小纯实现。**

`hub/domain/workflow_identity.py`：

~~~python
"""Stable workflow identities; no I/O or authorization decisions."""
from __future__ import annotations

import hashlib
import json
import math

MAX_CANONICAL_BYTES = 65536
MAX_CANONICAL_DEPTH = 32


def _validate_json(value: object, depth: int = 0) -> None:
    if depth > MAX_CANONICAL_DEPTH:
        raise ValueError("json_too_deep")
    kind = type(value)
    if value is None or kind in (bool, int):
        return
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("invalid_number")
        return
    if kind is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid_unicode") from exc
        return
    if kind is list:
        for item in value:
            _validate_json(item, depth + 1)
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("non_string_key")
            _validate_json(key, depth + 1)
            _validate_json(item, depth + 1)
        return
    raise ValueError("not_json")


def canonical_digest(value: object) -> str:
    _validate_json(value)
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid_json") from exc
    if len(raw) > MAX_CANONICAL_BYTES:
        raise ValueError("json_too_large")
    return hashlib.sha256(raw).hexdigest()


def _identifier(value: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid_identifier")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("invalid_identifier") from exc
    if size > 128:
        raise ValueError("invalid_identifier")
    return value


def dispatch_token(owner_id: str, workflow_id: str, step_key: str, generation: int) -> str:
    if type(generation) is not int or generation < 1:
        raise ValueError("invalid_generation")
    parts = (
        "workflow-v1", _identifier(owner_id), _identifier(workflow_id),
        _identifier(step_key), str(generation),
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
~~~

此函数接受已解析对象；JSON 原文的重复键检测、原始 body 上限和字段规则由 F1.3 在解析前落实，不能声称此 helper 检查了已丢失的重复键。大文件 hash 不使用此函数，仍走产物流式 hash。

- [x] **Step 4：重跑同一命令。** 预期 `Ran 6 tests`、`OK`；generation=2、owner 变化改变 token，协调器没有 epoch 参数可误用。
- [ ] **Step 5：执行范围内验证后审查并提交。** 确认差异仅为本任务两个文件；获准实施时使用：

~~~bash
git add hub/domain/workflow_identity.py tests/test_workflow_identity.py
git commit -m "feat: add stable workflow domain identities"
~~~

## Task 2：步骤迁移原语

**Files:** Create `hub/domain/workflow.py`；Test `tests/test_workflow_state.py`。

**Interfaces:**

- Consumes：当前状态、已核对事实所对应的目标状态，以及内部 `reconciled` 标志。
- Produces：`transition_step(current: str, target: str, *, reconciled: bool = False) -> str`；导出 `STEP_STATES`、`WORKFLOW_EXECUTION_STATES`；非法迁移抛 `ValueError`。

这个原语检查状态图。它不接受原始 HTTP 参数，不授予核对权限，也不替应用服务检查输入/验证/审批、是否已有 binding、资源是否实际停止。waiting_approval/paused 恢复到 ready 还是 running，由 F2 根据原 binding 判定；v1 必需步骤不能迁入 skipped。

- [x] **Step 1：写入完整失败测试。**

`tests/test_workflow_state.py`：

~~~python
import unittest

from hub.domain.workflow import transition_step


class WorkflowStateTests(unittest.TestCase):
    def test_normal_execution_path(self):
        state = "pending"
        for target in ("ready", "dispatching", "running", "succeeded"):
            state = transition_step(state, target)
        self.assertEqual(state, "succeeded")

    def test_wait_resume_and_idempotent_projection(self):
        self.assertEqual(transition_step("ready", "waiting_node"), "waiting_node")
        self.assertEqual(transition_step("waiting_node", "ready"), "ready")
        self.assertEqual(transition_step("running", "waiting_external"), "waiting_external")
        self.assertEqual(transition_step("succeeded", "succeeded"), "succeeded")

    def test_unknown_requires_explicit_reconciliation(self):
        with self.assertRaises(ValueError):
            transition_step("unknown", "succeeded")
        self.assertEqual(
            transition_step("unknown", "succeeded", reconciled=True), "succeeded",
        )
        with self.assertRaises(ValueError):
            transition_step("unknown", "running", reconciled=True)

    def test_dispatched_cancellation_requires_confirmed_boundary(self):
        self.assertEqual(transition_step("pending", "cancelled"), "cancelled")
        with self.assertRaises(ValueError):
            transition_step("running", "cancelled")
        self.assertEqual(transition_step("running", "cancelling"), "cancelling")
        self.assertEqual(transition_step("cancelling", "unknown"), "unknown")
        self.assertEqual(transition_step("cancelling", "cancelled"), "cancelled")
        self.assertEqual(transition_step("cancelling", "succeeded"), "succeeded")

    def test_terminal_and_required_steps_cannot_be_reopened_or_skipped(self):
        for current, target in (
            ("succeeded", "ready"), ("failed", "running"),
            ("pending", "skipped"), ("pending", "succeeded"),
        ):
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                transition_step(current, target)

    def test_bad_states_and_reconciliation_flags_are_rejected(self):
        for current, target, flag in (
            (True, "ready", False), ("ready", {}, False),
            ("queued", "ready", False), ("ready", "running", 1),
            ("running", "succeeded", True),
        ):
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                transition_step(current, target, reconciled=flag)


if __name__ == "__main__":
    unittest.main()
~~~

- [x] **Step 2：运行失败验证。** `PYTHONPATH=. python3 -m unittest discover -s tests -p test_workflow_state.py -v`。预期缺少 `hub.domain.workflow` 的 import error。
- [x] **Step 3：创建以下完整实现。**

`hub/domain/workflow.py`：

~~~python
"""Workflow state vocabulary and internal step transition guards."""
from __future__ import annotations

STEP_STATES = frozenset({
    "pending", "ready", "dispatching", "running", "waiting_node",
    "waiting_external", "waiting_approval", "paused", "cancelling",
    "succeeded", "failed", "unknown", "cancelled", "skipped",
})
WORKFLOW_EXECUTION_STATES = frozenset({
    "queued", "running", "waiting_node", "waiting_external", "waiting_approval",
    "paused", "cancelling", "succeeded", "failed", "unknown", "cancelled",
})

_ALLOWED = {
    "pending": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset({
        "dispatching", "waiting_node", "waiting_approval", "paused", "failed", "cancelled",
    }),
    "dispatching": frozenset({
        "running", "waiting_external", "waiting_approval", "succeeded", "failed",
        "unknown", "cancelling",
    }),
    "running": frozenset({
        "waiting_node", "waiting_external", "waiting_approval", "paused",
        "succeeded", "failed", "unknown", "cancelling",
    }),
    "waiting_node": frozenset({
        "ready", "running", "paused", "succeeded", "failed", "unknown", "cancelling", "cancelled",
    }),
    "waiting_external": frozenset({
        "running", "waiting_approval", "succeeded", "failed", "unknown", "cancelling",
    }),
    "waiting_approval": frozenset({
        "ready", "running", "paused", "succeeded", "failed", "unknown", "cancelling", "cancelled",
    }),
    "paused": frozenset({"ready", "running", "succeeded", "failed", "unknown", "cancelling", "cancelled"}),
    "cancelling": frozenset({"cancelled", "unknown", "succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
    "cancelled": frozenset(),
    "skipped": frozenset(),
}
_RECONCILED_TERMINALS = frozenset({"succeeded", "failed", "cancelled"})


def transition_step(current: str, target: str, *, reconciled: bool = False) -> str:
    if type(current) is not str or current not in STEP_STATES:
        raise ValueError("invalid_current_state")
    if type(target) is not str or target not in STEP_STATES:
        raise ValueError("invalid_target_state")
    if type(reconciled) is not bool:
        raise ValueError("invalid_reconciliation_flag")
    if reconciled:
        if current != "unknown" or target not in _RECONCILED_TERMINALS:
            raise ValueError("invalid_reconciliation_transition")
        return target
    if current == target:
        return current
    if target not in _ALLOWED[current]:
        raise ValueError("invalid_step_transition")
    return target
~~~

已绑定任务在 waiting_node/waiting_approval/paused 期间也可凭权威回执记录 succeeded；状态原语不替代 binding 检查。取消请求与真实完成竞态时，succeeded/failed 仍能记录真实完成事实；不能为了配合按钮把它改成 cancelled。只有未派发或实际停止的执行能走 cancelled 路径。F2 必须在真实 binding 上验证这些前置条件。

- [x] **Step 4：重跑同一命令。** 预期 `Ran 6 tests`、`OK`；特别核对 unknown 普通迁移仍失败。
- [ ] **Step 5：审查任务差异并提交。** 获准实施时使用：

~~~bash
git add hub/domain/workflow.py tests/test_workflow_state.py
git commit -m "feat: define guarded workflow step transitions"
~~~

## Task 3：当前版本证据与三维交付

**Files:** Create `hub/domain/workflow_evidence.py`；Test `tests/test_workflow_evidence.py`。

**Interfaces:**

- Consumes：Task 2 状态集合；当前 subject_digest；固定 validator_ref 集合；应用服务选择的当前 evidence。历史记录仍保存，但不是全部重新加入当前 evidence 集合。
- Produces：`ValidationEvidence` frozen dataclass；`validation_state(subject_digest: str, required_validators: frozenset[str], evidence: tuple[ValidationEvidence, ...]) -> str`；`can_release_successor(execution: str, validation: str, *, validation_required: bool) -> bool`；`delivery_state(execution: str, validation: str, acceptance: str, *, validation_required: bool, acceptance_required: bool) -> str`。

聚合优先级：当前必需 evidence 有失败 → failed；缺任意必需 validator → pending；完整但有 inconclusive → inconclusive；全部 passed → passed；无必需 validator → not_requested。新一代如何选中当前 evidence 由 F2 事务控制，不在此原语内猜测“最新”。

- [x] **Step 1：写入完整失败测试。**

`tests/test_workflow_evidence.py`：

~~~python
import unittest

from hub.domain.workflow_evidence import (
    ValidationEvidence, can_release_successor, delivery_state, validation_state,
)

CURRENT = "a" * 64
OLD = "b" * 64


class WorkflowEvidenceTests(unittest.TestCase):
    def test_old_subject_and_old_validator_revision_cannot_pass(self):
        evidence = (
            ValidationEvidence("old-source", "tests@1", OLD, "passed"),
            ValidationEvidence("old-recipe", "tests@0", CURRENT, "passed"),
        )
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), evidence), "pending")

    def test_all_current_required_validators_must_pass(self):
        evidence = (
            ValidationEvidence("review", "review@1", CURRENT, "passed"),
            ValidationEvidence("tests", "tests@1", CURRENT, "passed"),
        )
        required = frozenset({"review@1", "tests@1"})
        self.assertEqual(validation_state(CURRENT, required, evidence), "passed")
        self.assertEqual(validation_state(CURRENT, required, evidence[:1]), "pending")

    def test_failure_and_inconclusive_have_explicit_results(self):
        failed = (ValidationEvidence("bad", "tests@1", CURRENT, "failed"),)
        unsure = (ValidationEvidence("unsure", "tests@1", CURRENT, "inconclusive"),)
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), failed), "failed")
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), unsure), "inconclusive")
        self.assertFalse(can_release_successor("succeeded", "inconclusive", validation_required=True))

    def test_same_evidence_key_cannot_change_content(self):
        good = ValidationEvidence("one", "tests@1", CURRENT, "passed")
        bad = ValidationEvidence("one", "tests@1", CURRENT, "failed")
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), (good, good)), "passed")
        with self.assertRaises(ValueError):
            validation_state(CURRENT, frozenset({"tests@1"}), (good, bad))

    def test_not_requested_is_not_a_pass(self):
        self.assertEqual(validation_state(CURRENT, frozenset(), ()), "not_requested")
        self.assertTrue(can_release_successor("succeeded", "not_requested", validation_required=False))
        self.assertFalse(can_release_successor("succeeded", "not_requested", validation_required=True))
        self.assertFalse(can_release_successor("running", "passed", validation_required=True))

    def test_execution_completion_still_requires_acceptance(self):
        self.assertEqual(delivery_state(
            "succeeded", "passed", "pending", validation_required=True, acceptance_required=True,
        ), "awaiting_acceptance")
        self.assertEqual(delivery_state(
            "succeeded", "passed", "accepted", validation_required=True, acceptance_required=True,
        ), "delivered")

    def test_acceptance_cannot_override_failed_or_missing_validation(self):
        for validation, expected in (("failed", "validation_failed"), ("pending", "awaiting_validation")):
            self.assertEqual(delivery_state(
                "succeeded", validation, "accepted", validation_required=True, acceptance_required=True,
            ), expected)

    def test_execution_and_rejection_priorities(self):
        for execution, expected in (
            ("unknown", "needs_reconciliation"), ("failed", "execution_failed"),
            ("cancelled", "cancelled"), ("waiting_external", "in_progress"),
        ):
            self.assertEqual(delivery_state(
                execution, "passed", "accepted", validation_required=True, acceptance_required=True,
            ), expected)
        self.assertEqual(delivery_state(
            "succeeded", "passed", "rejected", validation_required=True, acceptance_required=True,
        ), "rejected")

    def test_invalid_evidence_and_boolean_flags_are_rejected(self):
        with self.assertRaises(ValueError):
            ValidationEvidence("bad", "tests@1", "not-a-hash", "passed")
        with self.assertRaises(ValueError):
            ValidationEvidence("bad", "tests@1", CURRENT, "success")
        with self.assertRaises(ValueError):
            delivery_state("succeeded", "passed", "accepted", validation_required=1, acceptance_required=True)
        with self.assertRaises(ValueError):
            validation_state(CURRENT, frozenset({"tests@1"}), tuple(
                ValidationEvidence(str(i), "tests@1", CURRENT, "passed") for i in range(33)
            ))


if __name__ == "__main__":
    unittest.main()
~~~

- [x] **Step 2：运行失败验证。** `PYTHONPATH=. python3 -m unittest discover -s tests -p test_workflow_evidence.py -v`。预期缺少 `hub.domain.workflow_evidence`。
- [x] **Step 3：创建以下完整实现。**

`hub/domain/workflow_evidence.py`：

~~~python
"""Current-version evidence and deterministic delivery projection."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .workflow import STEP_STATES, WORKFLOW_EXECUTION_STATES

VALIDATION_STATES = frozenset({"not_requested", "pending", "passed", "failed", "inconclusive"})
ACCEPTANCE_STATES = frozenset({"not_requested", "pending", "accepted", "rejected"})
_RESULTS = frozenset({"passed", "failed", "inconclusive"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _text(value: str) -> None:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid_evidence_reference")
    try:
        if len(value.encode("utf-8")) > 128:
            raise ValueError("invalid_evidence_reference")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid_evidence_reference") from exc


def _digest(value: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid_subject_digest")


def _choice(value: str, allowed: frozenset[str]) -> None:
    if type(value) is not str or value not in allowed:
        raise ValueError("invalid_state")


def _flag(value: bool) -> None:
    if type(value) is not bool:
        raise ValueError("invalid_boolean")


@dataclass(frozen=True)
class ValidationEvidence:
    evidence_key: str
    validator_ref: str
    subject_digest: str
    result: str

    def __post_init__(self) -> None:
        _text(self.evidence_key)
        _text(self.validator_ref)
        _digest(self.subject_digest)
        _choice(self.result, _RESULTS)


def validation_state(
    subject_digest: str,
    required_validators: frozenset[str],
    evidence: tuple[ValidationEvidence, ...],
) -> str:
    _digest(subject_digest)
    if type(required_validators) is not frozenset or len(required_validators) > 16:
        raise ValueError("invalid_validator_set")
    for validator in required_validators:
        _text(validator)
    if type(evidence) is not tuple or len(evidence) > 32:
        raise ValueError("invalid_evidence_set")
    seen = {}
    results = {validator: set() for validator in required_validators}
    for item in evidence:
        if type(item) is not ValidationEvidence:
            raise ValueError("invalid_evidence")
        if item.evidence_key in seen and seen[item.evidence_key] != item:
            raise ValueError("evidence_conflict")
        seen[item.evidence_key] = item
        if item.subject_digest == subject_digest and item.validator_ref in results:
            results[item.validator_ref].add(item.result)
    if not required_validators:
        return "not_requested"
    if any("failed" in values for values in results.values()):
        return "failed"
    if any(not values for values in results.values()):
        return "pending"
    if any("inconclusive" in values for values in results.values()):
        return "inconclusive"
    return "passed"


def can_release_successor(execution: str, validation: str, *, validation_required: bool) -> bool:
    _choice(execution, STEP_STATES)
    _choice(validation, VALIDATION_STATES)
    _flag(validation_required)
    return execution == "succeeded" and (not validation_required or validation == "passed")


def delivery_state(
    execution: str, validation: str, acceptance: str,
    *, validation_required: bool, acceptance_required: bool,
) -> str:
    _choice(execution, WORKFLOW_EXECUTION_STATES)
    _choice(validation, VALIDATION_STATES)
    _choice(acceptance, ACCEPTANCE_STATES)
    _flag(validation_required)
    _flag(acceptance_required)
    if execution == "unknown":
        return "needs_reconciliation"
    if execution == "failed":
        return "execution_failed"
    if execution == "cancelled":
        return "cancelled"
    if execution != "succeeded":
        return "in_progress"
    if validation_required:
        if validation == "failed":
            return "validation_failed"
        if validation != "passed":
            return "awaiting_validation"
    if acceptance == "rejected":
        return "rejected"
    if acceptance_required and acceptance != "accepted":
        return "awaiting_acceptance"
    return "delivered"
~~~

完整生产 evidence 还要有 owner/workspace、producer、source trust、validator 配方与时间等审计字段，由 F1.3/F2 类型和持久记录承载。这个最小值对象只包含聚合必需数据；不能直接作为未经裁剪的 HTTP DTO 或权限证明。

- [x] **Step 4：重跑证据测试与全组。**

~~~bash
PYTHONPATH=. python3 -m unittest discover -s tests -p test_workflow_evidence.py -v
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_workflow_*.py' -v
PYTHONPATH=. python3 -m pytest tests/test_workflow_identity.py tests/test_workflow_state.py tests/test_workflow_evidence.py -q
git diff --check
~~~

首批无其他 workflow 用例时，unittest 全组预期 21 个测试通过；pytest 同一 21 个用例通过。数字只描述此计划样例，不是当前项目已执行结果。

- [ ] **Step 5：审查范围后提交。** 获准实施时使用：

~~~bash
git add hub/domain/workflow_evidence.py tests/test_workflow_evidence.py
git commit -m "feat: derive workflow delivery from current evidence"
~~~

## 最终检查点与后继工作

- [x] 三任务均保存先失败、后通过的同一行为测试证据；没有代码字符串存在性测试冒充功能测试。
- [x] 核对 token 向量、状态名称、参数名与上位契约一致；没有 epoch 导致重建任务、无证据当通过或 cancelled 伪造停止。
- [x] F1 仅新增纯领域模块和测试；后续 F2.1 增加存储/恢复保护。配置默认值、原执行语义与节点生命周期未改动。
- [x] 更新[路线图](2026-10-20-workflow-platform-roadmap.md) F1.1–F1.3 与当前实现记录；完整字段编码已在执行契约 §3.1 固定，并由独立解析/冻结测试逐项验证。

完成本计划意味着领域规则可调用，不代表已有可运行 Workflow。F1.3 与 F2.1 的后续状态见实施进度；F2.2 的执行查回、F2.3 协调器与 F3 真实交付仍分别验收。三个提交步骤保持未勾选：本轮没有创建 Git 提交。
