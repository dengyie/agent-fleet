# Linux Workflow 沙箱验收实施计划

## 2026-11-10 当前架构集成续审

目标树为 `codex/functional-root-fixes` / `301e259`，以下源树历史结果不作为本次证明。
当前 macOS 全仓 6806 passed、5 Linux 平台 skip、204 passing subtests，43/44 journeys；
Linux 仍待验收。知识库 canonical《腾讯云 TCCLI 管理》确认原 `gz-cvm` 于 11/10 到期注销，
不继续连接旧端点，也不把其它生产机自行当作替代验收机。

复用 Task 3 原有 CI 配置：保留当前 main 的依赖锁、lint、审计、Windows 和部署契约，
仅补 bubblewrap/libseccomp2 与原 45 分钟测试预算。先运行配置回归得到失败，再修 workflow。
当前 CI 完整 Ruff 已发现 5 个 F401；保留确实被消费的兼容导出，删除其余未使用导入，
以相同 Ruff 命令和对应功能集合验证。无外部服务、运行依赖或生产配置改动。

- [x] 新 Linux CI 配置回归先红后绿，登记 RELEASE-01。
- [x] CI 完整 fatal/undefined/F401 Ruff 与受影响模块通过；334 passed/2 subtests。
- [x] 更新当前总账与本机验收证据，明确 Linux 实机缺口。后续完整6887 passed/5平台skip/204 subtests，43/44 journeys，仍不作为Linux零skip证据。

> **For agentic workers:** 使用 `executing-plans` 在现有独立工作树逐项实施；每个文件完整落盘后运行真实平台探针。

**Goal:** 让现有 Workflow 实隔离测试在 Linux 使用等价的内核隔离，消除仅 macOS 实现造成的四项平台 skip；原发布核验器保持零 skip 要求。

**Architecture:** 复用原 `SandboxBackend` 和 `CommandReviewAdapter` 的管理员 launcher 边界。测试 fixture 用 bubblewrap 构建 user/mount/PID/network namespace，只读绑定 Python 运行时、源目录和评审控制文件，仅工作区或 scratch 可写；libseccomp 拒绝网络系统调用。macOS 保留 sandbox-exec。没有新服务、队列、领域接口或生产配置。

**Tech Stack:** Python 3.10+ stdlib `ctypes` / `os` / `subprocess`，现有 Linux bubblewrap / libseccomp 系统工具、pytest；不改变 Python 运行依赖。

## 基准与约束

- [执行契约 §6.2](../specs/2026-10-20-workflow-execution-contract.md#62-f3-固定测试的执行接线约束)、[产物契约 §8](../specs/2026-10-20-artifact-capability-integration-contract.md#8-代码交付包与验证)和[验收矩阵](../../testing/workflow-platform-acceptance.md)要求真实只读源码、隔离测试、环境凭据白名单、原租约/取消及固定 subject；目录本身不算沙箱。
- Linux 全仓冻结 revision `1a4e4a29ff92760955d10ed74d52b3c70ec774aa` 已运行 5720 passed、0 failure/error、4 macOS 专项 skip、197 passing subtests，42/42 journeys；原 verifier 因 `junit_incomplete` 拒绝。不能把该结果改写为零 skip。
- 开发在 `codex/workflow-platform-docs`；主工作树、所有已有改动和历史证据保留。不提交、推送、合并、部署或修改生产 gate。
- 文件 UTF-8 无 BOM。已批准 Chromium156 恢复完成且 301 个归档文件匹配，153 实体测试目录独立；这不构成其他大包下载授权。Linux bubblewrap 包 65,633 bytes，仅解包到本次临时目录，不安装全局工具。
- launcher 只接受可信测试配置，不继承额外凭据。缺少 bubblewrap、libseccomp 或 namespace 支持时测试明确失败，不 skip，不回退 host process。
- 使用 namespace 内独立 `/proc`，drop ALL capabilities，`--die-with-parent`；根文件系统与所有运行时/源码/控制挂载只读。只有登记 workdir 或 scratch 是可写 bind。文件未挂载产生 ENOENT 也是拒绝：外层先证明该文件真实存在；无隔离对照必须揭示已开放的宿主读取、写入及网络。
- 网络 syscall 固定返回 EPERM；仅无路由产生 ENETUNREACH 不能作为 seccomp 证据。原 backend 仍承担资源限额、deadline、精确环境白名单及进程组回收。
- launcher 参数规模固定，路径集合索引/去重 O(n)、空间 O(n)；不复制运行时或源树，使用只读挂载。仅真实配置对应的 Linux/macOS 证据，不推广为 Windows 或任意生产沙箱验收。

## Task 1：原失败证据与测试平台入口

**Files:** `tests/test_workflow_sandbox.py`、`tests/test_workflow_review_execution.py`、`tests/test_workflow_e2e.py`。

- [x] 检查完整 JUnit 与原 verifier：四项 skip 全为 macOS sandbox-exec；873 文件前后相同，Node22/Chromium153 真实配置已核验，排除源码漂移和浏览器缺失。
- [x] 将原四个参数实例改为 Linux/macOS 共用的实际隔离边界测试；Linux 初次必须因 helper 不存在或 namespace 未实现失败，保存 JUnit。
- [x] 保留 macOS profile 与所有成功/失败/证据/工作区不变断言；Linux 文件拒绝只接受 EPERM/EACCES/EROFS/ENOENT，网络仅 EPERM/EACCES。

## Task 2：完整 Linux 测试 launcher

**Create:** `tests/fixtures/workflow/linux_sandbox.py`、`tests/workflow_sandbox_runtime.py`。

**Interfaces:**

```python
def linux_test_backend(workdir: Path) -> SandboxBackend:
    return SandboxBackend(workdir, launcher=linux_launcher(workdir=workdir))

def linux_review_launcher(script: Path) -> tuple[str, ...]:
    return linux_launcher(script=script)
```

`linux_launcher` 固定为登记 Python + 完整 fixture 脚本 + `--bwrap` / `--runtime` / `--venv` / 模式参数 + `--`。review 使用原 adapter 的 `{source}` / `{control}` / `{scratch}` 替换。child fixture 经 ctypes 生成 libseccomp BPF 到唯一匿名 fd，export 成功且大小有界后将 fd 设置为 inheritable，再 `os.execv` bubblewrap。filter context 与失败 fd 均显式释放，不吞异常。

- [x] 完整落盘 launcher 与精确 argparse 模式校验；白名单只读绑定系统动态库、Python base/venv、单 reviewer 脚本、source/control，test workdir 或 review scratch 单独可写。
- [x] 根 filesystem `--remount-ro /`，所有 capabilities 丢弃，新的 user/PID/network namespace 与私有 `/proc`，凭据仍由现有 broker 环境白名单剔除。
- [x] libseccomp 固定拒绝 socket/connect/bind/listen/accept 与 send/recv 系统调用，未知 syscall/filter 错误明确失败；不把隔离失败映射为成功或 unsupported skip。
- [x] 在 Linux 实际执行原固定 pytest、只读评审与真实 Cookie HTTP 全链；在 macOS 执行原 profile，保存完整 JUnit 与错误链。

## Task 3：CI、矩阵、完整门禁与文档

**Files:** `.github/workflows/ci.yml`、`docs/testing/journeys.json`、`docs/testing/README.md`、`docs/testing/workflow-platform-acceptance.md`、开发入口/实施进度、本计划。

- [x] CI 安装明确的 bubblewrap 系统测试工具；没有新增运行依赖或自动启用沙箱能力。
- [x] 将三个共用 selector / 全部参数实例登记 WF-01；原 verifier 和 JUnit/skip 合同不变。
- [x] 先执行三模块关联集合，再冻结新的 Git revision 和所有源文件摘要；同 Node22/Playwright1.63/Chromium153、禁用 DAC 能力的 Linux 全仓运行原命令，不使用 `-k` / `--deselect` / `--lf`。
- [x] 原 `tools.testing.release_evidence` 实际通过；核验全部 PNG、矩阵/源码摘要、JUnit 和七项外部 not_run。Linux Python3.13 与 CI Python3.10 区别保留；本地双版本兼容另行证明，不拼接全仓结果。
- [x] 更新文档并保存全量本地证据；已批准 Chromium156 恢复记录逐文件审计通过。Windows、真实两机、真实模型/CLI、完整生产隔离与 MCP/Muse 外部验收继续开放，整体目标保持 active。

## 2026-10-30 关联验证与完整门禁准备

- 红证据：`sandbox-red-20261029/evidence` 为 3 failed / 1 passed，Linux helper 缺失时失败关闭，无隔离对照真实失败证据正确发布。初版缺少 bwrap PATH 的运行、venv 可执行文件别名和相对 JUnit 参数不匹配的失败均保留，未调整生产实现或放宽合同。最终从原配方读取精确相对 JUnit 参数；uv 的 venv 别名只读绑定同一 Python base，不暴露运行时父目录。
- Linux 三模块实际 **50 passed / 0 skip**，`sandbox-green-20261029/evidence-related`；宿主文件先真实创建，无隔离 read/write/network 三例均必须失败，实际沙箱三例均必须通过。沙箱直接断言不同 user/mount/PID/network namespace、CapEff/CapPrm/CapBnd 为零、NoNewPrivs=1、Seccomp=2，源码写入拒绝。评审原 source/control 只读、scratch 可写，额外合成凭据不继承。
- macOS Python **3.10.21 / 3.13.15 各 50 passed / 0 skip**；JUnit/命令/源码摘要保存于本地证据根的 `sandbox-macos-20261030`。两次运行的五个相关文件摘要一致。首次依赖离线解析不足后只安装项目既有 requirements-test 约束的常规小包，没有新增产品依赖。
- CI 增加明确 bubblewrap/libseccomp2 测试工具；根据完整运行 1562.90 秒的已观察耗时将 test 超时由 15 分钟调整为 45 分钟，避免固定 15 分钟截断。原 release verifier 未修改。WF-01 新增 3 selectors / 4 cases。
- 运行时只读 bind，源码只读，测试 `.sandbox-home` / `.sandbox-tmp` / `.pytest_cache` 和单一 JUnit 文件允许写，review 仅 scratch 允许写。无运行时复制，固定大小路径索引，未增加服务或业务分层。
- 全仓新快照和原发布校验当时仍待运行；该状态由下节的真实冻结运行结果更新，不能用关联集合本身替代完整门禁。

## 2026-10-30 Linux 全仓门禁结果

- 冻结测试 revision：`8b93185d81dab3eb2442ef8fd0b863dc8eda2552`，开发工作树 HEAD `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`。测试快照包含 876 个源文件（874 个 UTF-8 无 BOM 文本、2 个既有二进制资源）；matrix SHA-256 为 `f2bc851f3597b77e2a310cc2f642c9bc8df614de5a3ec6a44fbaad98709a5b8b`，1484 个不同 required selector。源 bundle 为 3,134,974 bytes，SHA-256 `5fa8e98070d4e9df38b2bd39968cb0a2b0ffd2e658a7b59e64acf3c158b1de18`。
- 使用 Python 3.13.16、Node 22.21.1、Playwright 1.63.0、Chromium 153.0.8010.12、`LC_ALL=C`；以 `setpriv --bounding-set=-dac_override,-dac_read_search --no-new-privs` 启动原 `python -m pytest tests -q --require-journeys ...` 命令，未缩小测试范围。运行耗时 1547.99 秒，pytest 退出 0：**5724 passed、197 passing subtests、0 failed、0 error、0 skipped**，42/42 journeys passed。运行前后 876 个源码摘要匹配，冻结 checkout clean。
- 原 `tools.testing.release_evidence` 在冻结 checkout 上退出 0：`Verified 42 journeys, 1723 selectors, and 5724 JUnit cases; external checks remain not_run`。44 张 PNG 均由仓库 PNG verifier 逐张重验；七项外部检查逐项保持 `not_run`。Linux Python 3.13.16 的全仓证据不冒称 CI Python 3.10 全仓结果；macOS Python 3.10.21/3.13.15 的 50-case 双版本运行是独立兼容证据。
- gz-cvm 远端与本地持久副本的 51 个证据文件共 7,937,234 bytes 逐字节 SHA-256 对比一致。本地原 verifier 再执行一次也退出 0。完整证据与来源审计位于 `/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-full-20261030-nzb5qq22/`，包括 `evidence/controlled-full/`、`provenance.json`、`frozen-source.bundle` 与 `local-evidence-audit.json`。Chromium 156 恢复逐一核验 301 个归档文件，记录位于同证据根 `gz-cvm/chromium156-restoration-audit.json`。
- 本结果关闭的是 Linux 测试用实际隔离及其零 skip CI 门禁缺口。Windows 文件句柄/reparse、两台真实节点交接、真实 CLI/模型、生产 sandbox 配置、真实 MCP/Muse 试点仍未运行；整个产品目标继续开放。
