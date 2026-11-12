# Summary reader preservation Implementation Plan

> **For agentic workers:** Execute the single root-cause fix inline with evidence-driven-bugfix and verification-before-completion, then verify its publication separately.

**Goal:** 首次发布摘要时保留已存在的更高 Workflow reader，避免媒体 Run 历史和备份因 reader 倒退而失效。

**Architecture:** 沿用 `WorkflowSummaryRepository.publish` 的原写事务与摘要表初始化；把常量覆盖改为 SQLite `max(minimum_reader_version,11)`。不新增 schema、数据库或自动摘要生成路径。

**Tech Stack:** Python 3.10+、SQLite、pytest；原 Native Run 派发/查回及明文/加密备份。

返回[开发完成账](../../platform/development-completion.md)及[Workflow 入口](../../platform/workflow-platform/README.md)。

## Global Constraints

- 主工作树保持原状，修改位于 `codex/functional-root-fixes`。
- UTF-8 无 BOM；既有 reader 13/14/15 只能保留或提升。
- 第一次摘要发布失败时，建表与 reader 更新按原事务一并回滚。
- 功能修复不代表已部署，也不代表真实模型和生产执行节点验收完成。

## Task 1: Preserve the reader in the original summary transaction

**Files:** `hub/infrastructure/workflow_summary_repository.py`、`tests/test_workflow_summary.py`、`docs/testing/journeys.json`。

**Interfaces:** 使用既有 `publish`、`RunStepExecutor.lookup`、`WorkflowRepository.init` 与 `PlatformBackupService.create_backup`，不改变签名。

- [x] 增加真实图片、音频和视频 Run 后首次/第二次摘要发布的参数化回归，随后重新初始化仓库、查回原 binding、读原摘要版本并创建备份。
- [x] 用 SQLite authorizer 拒绝首次摘要写入，证明原事务回滚且 reader15 保持。
- [x] 修复前执行两个新函数：3 failed / 1 passed，reader13/14/15 均被实际降低为11；保存原 JUnit 和日志。
- [x] 仅修改原 SQL：

```python
connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,11) WHERE singleton=1")
```

- [x] 初次关联验证9模块315 passed，零 failure/error/skip，31.58s；原红绿证据根：`/Users/mango/.cache/agent-fleet/verification/summary-reader-20261113/`。
- [x] 将两个函数登记 WF-06，并复核原摘要 API/UI、媒体历史、备份与发布证据合同：12模块380 passed、零failure/error/skip、41.30s。
- [x] 核对 lint、Python3.10语法、UTF-8/no BOM、diff、测试收集与源码范围：7665实例、46journeys/2580selectors，零缺项；Windows正式选择增加两个新函数的4实例。
- [ ] 提交已核验增量。
- [ ] 导出到原公开短历史，完整 tree 相等后建立独立 PR，并读取该 revision 的原 CI 终态及工件。

## Deployment acceptance

最新公共候选尚未合入 main；线上仍为301e259。生产接通至少需要完整发布版本、持久 Workflow manifest、可用模型/执行节点以及真实任务、产物下载和重启恢复。此 reader 修复是上线前功能条件，不代替上述验收。

2026-11-13 05:04 SGT真实容器只读检查：2个enabled Node、0个120秒内新鲜心跳，持久gate文件仅platform/worker/scheduler为true；bwrap不可用，namespace探针退出1并报告Operation not permitted。未写配置、提交任务或重启服务。

最终关联命令：

```bash
PYTHONPATH=.:tests FLEET_PLAYWRIGHT_MODULE=/opt/homebrew/lib/node_modules/@playwright/mcp/node_modules/playwright /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest -q tests/test_workflow_summary.py tests/test_workflow_summary_inputs.py tests/test_frontend_workflow_summary_contracts.py tests/test_workflow_summary_admin_browser.py tests/test_workflow_native_media_inputs.py tests/test_workflow_native_audio_inputs.py tests/test_workflow_native_video_inputs.py tests/test_workflow_native_text_recovery.py tests/test_platform_backup.py tests/test_platform_backup_encrypted.py tests/test_release_evidence.py tests/test_runtime_dependencies.py --junitxml=/Users/mango/.cache/agent-fleet/verification/summary-reader-20261113/final.xml
```

原先未指定`tests`目录的全仓收集把产品兼容转发模块`hub/domain/remote_workflow_test.py`误作测试，产生import-file-mismatch；保留该诊断日志。使用正式范围`pytest tests --collect-only -q`重新收集并检查矩阵，不改产品转发模块或清理用户缓存。
