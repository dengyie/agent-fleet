# CI Auto Deploy Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** main 的完整测试和前后端打包通过后，自动部署到现有 HK 实例，并保留失败证据与代码回滚能力。

**Architecture:** GitHub hosted runner 通过专用受限 SSH key 上传同一 CI 的发布包。宿主预装的 root-owned receiver 校验命令、大小和 SHA-256，通过 systemd 启动独立部署事务；配置与凭据只保存在环境 secrets 和宿主私有配置。事务更新原 LIVE 目录中的发布管理路径，使用现有 canonical launcher，保留 gates、hosts、账号凭据和全部运行数据。

**Tech Stack:** GitHub Actions、OpenSSH、systemd、Python 标准库、Docker、Nginx、SQLite。

## Constraints

- 用户本次明确授权自动部署，替代旧手工-only 约定；不复制旧 SSH deploy.yml。
- 工作树为 `codex/ci-auto-deploy`；不修改主工作树的并行浏览器开发。
- PR 无部署凭据；只有 main 的 push 或 main 手动运行在测试/打包成功后进入 production job。
- 部署串行且不因新提交取消；过期 main SHA 不部署。
- 不改 LIVE inode，不覆盖 fleet-gates.conf、hosts.yaml、credentials、state、var、.env。
- 数据库停写后用 SQLite backup API 保存；自动回滚只恢复代码和 Nginx，永不恢复旧数据库。
- 不通过真实模型或邮件调用执行自动探针；独立外部验收状态仍准确记录。

## Tasks

- [x] 新增 `deploy/auto_release.py`：发布包校验、限定路径复制、数据库备份、部署事务与故障回滚；临时目录测试校验失败、部分复制失败、探活失败、回滚失败、数据保留和幂等。
- [x] 新增 `deploy/auto_deploy_host.py` 和容器 helper：真实宿主命令边界、活动任务检查、维护屏障、精确进程停止、原 launcher 重启、账号/版本探针。
- [x] 新增受限 SSH receiver，校验上传并通过 systemd 执行；测试非法命令/损坏包不能进入部署。
- [x] 更新 CI：同 run artifact、main-only、production environment、已知主机校验、专用 key、禁止取消部署、过期版本跳过、上传部署结果。
- [x] 跑定向及完整门禁，审查 diff；安装专用 receiver 和配置/key，使用当前 main 的 CI 产物执行仅校验探针，不替换 LIVE。
- [ ] 创建新 PR 并验证 Linux CI；配置 environment secrets；补齐仓库与 canonical 运维文档、router 和 MOC。

## Validation

定向：`PYTHONPATH=. .venv/bin/python -m pytest tests/test_auto_deploy.py tests/test_release_layout.py -q`。
完整：沿用 `docs/testing/README.md` 的浏览器和 journey 门禁命令。
生产配置验证只使用 receiver 的 `check` 模式，必须保证 LIVE inode、版本、进程与运行配置不变。新 workflow 合入 main 后开始自动部署；本任务不自行合并 PR。

## Evidence (2026-10-04)

- 部署事务和发布布局专项：68 passed、2 subtests。新增失败证据包括维护前任务竞态、提交状态写入失败、回滚记录写失败、上传中断、缺少结果和失败结果不能假绿；每项已先红后绿。
- 第一轮完整门禁：2313 passed、2 个 macOS 平台 skipped、154 subtests；27/27 journeys。后续新增 6 个回归实例后重跑完整门禁，最终交付以同一 PR 的最新 Linux CI 为准。
- 宿主四个安装文件逐一 SHA-256 比对一致；真实专用 SSH key 的 check 上传→systemd→候选导入→账号/readiness/退出通过。前后 LIVE 版本/inode/进程和运行配置摘要一致；未执行实际切换。
- production 环境已配齐 5 个 SSH secrets，branch policy 仅允许 main；专用 key 的普通 shell 命令实测被拒绝。临时明文 bootstrap 凭据已从本机和宿主清除。
- canonical 运维手册、文档路由和项目 MOC 已同步；文档扫描 RESULT=OK。自动发布在此 workflow 合入 main 后激活，不将 PR 通过当作已部署。

- 全量重跑暴露既有脱敏测试偶发误报：随机 adoption ID 含 `4242` 被当作 PID 泄露。已用固定 ID 先复现失败，再改为公开响应字段合同和实际值断言，路径泄露检查保留；部署/发布/接管 HTTP 合并专项 91 passed、2 subtests。最新完整门禁与 Linux CI 结果记录于 PR。
