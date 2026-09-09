# SDD ledger — plan: docs/superpowers/plans/2026-08-28-agent-profile-registry.md

Identity: 2026-08-28-agent-profile-registry
Base branch: main (feat/agent-profile-registry @ deb27d2)
Worktree: .claude/worktrees/agent-profile-registry

## Rulings (pre-execution)

- **Ruling: 范围 = P0 + P1。P2(hub 下发 profile)暂缓。** — 依据:深度 review 建议里 P2 触碰 fail-closed 安全模型(控制面绝不下发密钥),用户「可以,分为详细开发任务」是对 P0/P1 方向的批准;P2 需单独安全设计。成本:若用户本意含 P2,需再排一轮。
- **Ruling: Task 2 的测试是回归护栏,非严格 fail-first。** — `HUB_TYPES == EXECUTABLE_AGENT_TYPES` 按值比较,派生前也成立(字面量值相等)。实现者应确认改前测试即绿,改后仍绿;若要验证「派生」,用身份断言 `hub.domain.task.DEFAULT_AGENT_TYPES is EXECUTABLE_AGENT_TYPES`。成本:无——行为保证是「值不变」。
- **Ruling: probe DEFAULT_AGENT_TYPES 顺序由 hermes 开头变为 codex 开头,语义无差。** — 若某测试按顺序断言 probe 输出,改为集合断言并记录。成本:极低(输出是 dict)。
- **Ruling: hosts.yaml 的 agents[] 清单不改、不做校验。** — 它是展示性节点清单,v4 ingest 模型下由 probe 动态发现 agent。成本:若用户期望 hosts.yaml 也纳入 registry,后续可加。

## Pre-flight scan

| 共享文件/接口 | 生产者任务 | 消费者任务 | 检查结果 |
|---|---|---|---|
| agent_profiles.EXECUTABLE_AGENT_TYPES / OBSERVABLE_AGENT_TYPES | T1 | T2 | 名称一致 ✓ |
| agent_profiles.PROFILES / is_executable / default_command | T1 | T3 | 名称一致 ✓ |
| tests/test_agent_profiles.py | T1(创建) | T2(追加) | 顺序依赖,串行 ✓ |
| agent_family 字段名 | T4(repo/service/DTO) | — | 三处一致,沿用 public_session_dto 既有键 ✓ |
| T2(probe_collectors) vs T4(bridge/session) | — | — | 无共享文件,独立 ✓ |

- T1↔T3: T3 回退循环只 fill 完全缺省的可执行家族,不覆盖显式 null;与 spec「部分指定保持原样」一致 ✓
- T4 迁移幂等: `_migrate_v2` 每次 init 都跑,新库 ALTER 抛 OperationalError 被捕获;老库 ALTER 补列 ✓
- T4 sanitize: `agent_family` 经 `_clean_row` 截断 64 + `public_session_dto._public_id` 拒绝 path/secret 形状;家族名均安全 ✓

## Tasks

- [x] Task 1: agent_profiles.py 注册表模块 — d56c0e9 + fix aa13aad(trailing newlines);评审 APPROVED(1 MINOR 已修)
- [x] Task 2: 收敛三处 DEFAULT_AGENT_TYPES — 22bb57d;评审 APPROVED(1 MINOR 已沉)
- [x] Task 3: runner_config 回退到注册表默认 — 64e9a85;评审 APPROVED(2 MINOR 已沉)
- [x] Task 4: session→agent_family 绑定 — 485e78d;评审 APPROVED(3 MINOR 已沉)
- [x] 终审 + 集成验证(全量 pytest + py_compile)——1079 passed,2 skipped,143 subtests,py_compile OK

## Final review (终审, a14c92296b4d12495)

Verdict: **Ready to merge — Yes**。0 BLOCKING / 0 MAJOR / 3 MINOR。全量 1076 passed + 2 skipped;py_compile OK。
- MINOR-1(安全/一致性):`session_repository._normalize_spec` 的 agent_family sanitize 只拒 `/` `\`,未按 spec「沿用 _public_id 语义」拒完整 secret-marker 集(token/key/secret/private/password)。公开面仍被 `public_session_dto._public_id` 兜住 → 纵深不足,非泄露。**→ 纳入 fix 轮(属 spec 合规缺口)**。
- MINOR-2(测试缺口):service 层 `_session_spec_from_event` 的 agent_family 提取无直接断言。**→ 纳入 fix 轮,补一条 service 层断言**。
- MINOR-3(语义,spec 已授权):回退是「增量注入」,省略家族会被补齐;只跑 hermes 需显式 null 其他。非缺陷。**→ 在样例注释补一句提示**。

## Fix round 1(终审 3 MINOR 收敛,e254b3c)

- Fix 1:`_FAMILY_REJECT_MARKERS` 扩展为完整 secret-marker 集 + `test_agent_family_rejects_secret_markers` ✓
- Fix 2:`tests/test_session_ingest_api.py` 新增 `SessionServiceSpecExtractionTests`(正向提取 + 缺失不含 key)✓
- Fix 3:`deploy/agent-runner.yaml.example` 追加 hermes 专用提示行 ✓
- 集成验证:全量 1079 passed + 2 skipped + 143 subtests;py_compile OK。终审 3 MINOR 全部关闭,无残留。

## Findings ledger

- T1: MINOR trailing newlines → 已修(fix aa13aad)。
- T2: MINOR PEP8 import 字母序(probe_collectors.py)→ **park**。
- T3: MINOR 样例注释全角标点(vs brief 半角)→ **park**(cosmetic)。MINOR(informational)`from agent_profiles` 依赖 sys.path 根,与 Task 2/3 同一写法,**已知约束,不修**(与既有 `from report_schema import` 同模式)。
- T2 实现者备注(准确):「三处」实为两处字面量 + 一处引用;`tests/test_task_domain.py` 不存在,实际在 `tests/test_services.py`。
