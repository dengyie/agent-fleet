# 线上验收闭环修复计划

**基准**：`docs/testing/release-acceptance.md`、`docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md`、`docs/platform/muse-workspace.md`。复用 Python 3.10+、Flask、SQLite 和现有纯函数/仓储边界，不新增依赖或服务抽象。

## 已复现问题与修改边界

1. Windows 真实 Claude 用 `--session-id` 启动时，Probe 忽略会话 ID 并按最近修改时间选中了另一个进程的 transcript。Claude 原生路径必须基于明确的 `--session-id` / `--resume` / `-r` 完整 ID，匹配各 project 直属的完整文件名；未找到、重复或无法完成有界检查时返回无路径，不猜测其他会话。进程控制身份校验不变。最多检查 512 个 project 条目，不遍历历史 transcript 或 subagent；时间 O(B)、额外空间 O(1)，B 为固定预算。
2. 生产 Muse 状态接口重复返回 50 份完整审批结果（2,262,234 字节），前端仅显示操作类型、状态和时间。仓储 audit 查询只投影 request_id/action/state/created，不读取或解析 result。详细 receipt 和墓碑保留原数据，重复请求仍返回第一次的完整结果；列表最多 50 条，空间 O(50)。

## 文件与验证

- [ ] `tests/test_discovery.py`：会话 ID 各种 CLI 写法、缺失/重复/近似匹配、无 ID、超预算、Windows 长命令行。
- [ ] `tools/probe/discovery.py`：Claude 精确、有界且歧义时关闭的定位。
- [ ] `tests/test_muse_workspace.py`：50 份大快照经真实 HTTP status 后只返回摘要，详细回执重放仍一致，跨账号隔离。
- [ ] `hub/infrastructure/muse_repository.py`：摘要查询与完整回执分离，删除列表 result 复制。
- [ ] 文档同步上述合同；focused tests、浏览器 Muse 回归、完整 Linux/Windows CI、准确版本部署。
- [ ] home-win 升级准确发布，真实工具纳管/暂停/恢复/终止/脱管，核验 OS 退出与 Hub closed/stopped 一致。
- [ ] 外部模型逐个只读验收；上游失败保留原 unknown，不重放、不改渠道。不支持或缺少授权的外部项明确记为未验收。

## 本次真实证据

仓库外私有证据：`~/.codex/agent-fleet-deploy-evidence/20261103/completion/`。Windows native-identity diagnostic 记录实际 PID 与错误/预期原生会话 ID；model-live-first 记录三个成功模型和 Opus 原始 unknown Run；Opus 网关日志证明时段限制、余额不足及上游不可用，不是本地租约错误。Windows 首次验收在纳管前拒绝错误 transcript，并只清理自身创建的测试进程树。所有凭据不进入仓库。
