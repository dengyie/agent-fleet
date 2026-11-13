# 发布证据 PNG 完整性实施计划

## 2026-11-10 当前架构集成验证

原源树实现遗漏于`codex/functional-root-fixes`的初次集成；本轮在目标树重新得到8类损坏PNG被接受的8 failed，
再原样带入纯流式校验器并接回当前release entry。原Git/矩阵/JUnit/外部检查不放宽。
内存探针的父进程3MiB无关分配先1 failed/1 passed，随后在独立子进程校验同一16MiB像素文件，
保持2MiB上限。五模块213 passed/2 subtests；完整6887 passed/5平台skip/204 subtests、
43/44 journeys、1975 selectors、46张实际PNG全部完整核验，1121源码摘要不变。
完整路径`/tmp/agent-fleet-functional-root-ci-png-20261110/`，本机环境与Linux缺口见
[当前开发总账](../../platform/development-completion.md)。下文历史结果保留其原源码归属。

> **For agentic workers:** 使用 `executing-plans` 在本会话逐项实施与自检；沿用现有独立工作树，按 checkbox 记录证据。

**Goal:** 现有发布核验器拒绝截断或损坏的浏览器 PNG，同时保持有效截图与原 CI 门禁兼容。

**Architecture:** 在 `tools/testing/png_evidence.py` 保留一个流式 PNG 校验函数，原 `tools/testing/release_evidence.py` 负责文件所有权和统一 `EvidenceError`。不增加第二个发布核验入口、运行时服务、执行队列或外部依赖。

**Tech Stack:** 现有 Python 3.10+、stdlib `struct`/`zlib`/`typing.BinaryIO`、pytest；原 CI 为 Linux/Python3.10/Node22/Playwright1.63.0。

## 基准与约束

- [测试入口](../../testing/README.md)的完整门禁/真实截图规则与[证据核验](../../testing/test-chains.md#核验-ci-证据)继续生效；Git HEAD/clean tree、矩阵摘要、全部 selector/JUnit、外部 not_run 规则不变。
- 本地 dirty 报告仍只是开发证据；Linux RELEASE-01 和真实外部验收不能由此补填 passed。
- 开发路径 `/Users/mango/.codex/worktrees/workflow-platform-docs/agent-fleet`，分支 `codex/workflow-platform-docs`；保留主工作树和全部已有改动，不提交、推送、合并或部署。
- UTF-8 无 BOM；不删除用户文件、不修改 Obsidian。Linux 续接中经用户明确批准，在临时目录下载 Chromium 153，并复制同版本 Node/Chromium 到另一 Linux 对照机；不由此扩大其他软件的下载授权。
- PNG 按标准合法 IHDR、chunk 类型/顺序、CRC、完整 IDAT/zlib、scanline 长度/filter 与末尾 IEND 核验。支持既有合法色型/位深、分片 IDAT 与 Adam7；不把 Node 上传的 4096 尺寸限制套到 CI 全页截图。
- 输入和解压块各至多 64 KiB；zlib 窗口和至多七个 pass layout 由单文件调用拥有。时间 O(B + D)，空间 O(64 KiB)，B 是文件字节、D 是合法头声明的实际解压字节；不保留完整像素或文件。
- 损坏/读取失败统一为 `browser_evidence_invalid`，保留 `ValueError`/`OSError` 与底层 zlib 原始 cause；缺截图仍为 `browser_evidence_missing`。

## Task 1：失败证据与兼容 fixture

**Files:** `tests/test_release_evidence.py`。

- [x] 使用独立 stdlib PNG builder 替换只有签名的成功 fixture；原 Git/矩阵/JUnit/外部检查夹具保持。
- [x] 经真实 `verify_release_evidence` 和临时 clean Git checkout 覆盖八种损坏字节，断言 `EvidenceError.code == 'browser_evidence_invalid'`。
- [x] 执行红色回归：`/private/tmp/agent-fleet-muse-identity-py313-20261026/bin/python -m pytest tests/test_release_evidence.py::test_rejects_incomplete_or_corrupt_png_evidence -q --tb=short --junitxml=/tmp/agent-fleet-release-png-red-20261027.xml`。实际 **8 failed**，全部 DID NOT RAISE EvidenceError；旧实现只读8字节而接受损坏文件。

## Task 2：流式格式校验与原入口接线

**Create:** `tools/testing/png_evidence.py`。

**Interface:** `verify_png(source: BinaryIO) -> None`；成功返回 None，格式错误 ValueError，读取错误保持原 OSError。调用方拥有打开的文件，不由解析器关闭。

- [x] 完整落盘以下实现；所有缓冲与 parser state 仅属于当前文件。

```python
"""Streaming PNG integrity checks for the existing release-evidence gate."""
from __future__ import annotations

import struct
from typing import BinaryIO
import zlib


CHUNK_BYTES = 64 * 1024
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_CRITICAL = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
          (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _read_exact(source: BinaryIO, size: int) -> bytes:
    buffer = bytearray(size)
    offset = 0
    while offset < size:
        part = source.read(size - offset)
        if not part:
            raise ValueError("incomplete PNG chunk")
        buffer[offset:offset + len(part)] = part
        offset += len(part)
    return bytes(buffer)


def _check_crc(source: BinaryIO, expected: int) -> None:
    if int.from_bytes(_read_exact(source, 4), "big") != expected & 0xffffffff:
        raise ValueError("PNG checksum mismatch")


def _header_rows(header: bytes) -> tuple[int, int, tuple[tuple[int, int], ...]]:
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    if (not 1 <= width <= 0x7fffffff or not 1 <= height <= 0x7fffffff
            or color not in _DEPTHS or depth not in _DEPTHS[color]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise ValueError("invalid PNG header")
    passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
    layouts = [(0, 0)] * len(passes)
    count = 0
    for x_start, y_start, x_step, y_step in passes:
        columns = (width - x_start + x_step - 1) // x_step
        rows = (height - y_start + y_step - 1) // y_step
        if columns <= 0 or rows <= 0:
            continue
        layouts[count] = (1 + (columns * _CHANNELS[color] * depth + 7) // 8, rows)
        count += 1
    return color, depth, tuple(layouts[:count])


class _Pixels:
    """Parser state owned by one image; no decoded rows are retained."""

    def __init__(self, rows: tuple[tuple[int, int], ...]) -> None:
        self.decoder = zlib.decompressobj()
        self.rows = rows
        self.expected = sum(size * count for size, count in rows)
        self.decoded = 0
        self.next_filter = 0
        self.pass_index = 0
        self.pass_end = rows[0][0] * rows[0][1]

    def _check_rows(self, data: bytes) -> None:
        end = self.decoded + len(data)
        if end > self.expected:
            raise ValueError("PNG pixel data exceeds header")
        while self.next_filter < end:
            if data[self.next_filter - self.decoded] > 4:
                raise ValueError("invalid PNG row filter")
            self.next_filter += self.rows[self.pass_index][0]
            if self.next_filter == self.pass_end and self.pass_index + 1 < len(self.rows):
                self.pass_index += 1
                size, count = self.rows[self.pass_index]
                self.pass_end += size * count
        self.decoded = end

    def feed(self, data: bytes) -> None:
        pending = data
        try:
            while pending:
                decoded = self.decoder.decompress(pending, min(CHUNK_BYTES, self.expected - self.decoded + 1))
                if self.decoder.unused_data:
                    raise ValueError("trailing PNG compressed data")
                self._check_rows(decoded)
                tail = self.decoder.unconsumed_tail
                if not decoded and tail == pending:
                    raise ValueError("PNG decoder made no progress")
                pending = tail
        except zlib.error as exc:
            raise ValueError("invalid PNG compressed data") from exc

    def finish(self) -> None:
        if (not self.decoder.eof or self.decoder.unused_data or self.decoder.unconsumed_tail
                or self.decoded != self.expected or self.next_filter != self.expected):
            raise ValueError("incomplete PNG pixel data")


def _chunk_data(source: BinaryIO, length: int, kind: bytes, pixels: _Pixels | None) -> int:
    checksum = zlib.crc32(kind)
    remaining = length
    while remaining:
        data = source.read(min(remaining, CHUNK_BYTES))
        if not data:
            raise ValueError("incomplete PNG chunk")
        checksum = zlib.crc32(data, checksum)
        if pixels is not None:
            pixels.feed(data)
        remaining -= len(data)
    return checksum


def verify_png(source: BinaryIO) -> None:
    """Check structure, CRCs and complete bounded deflate output; leave ownership to the caller."""
    if _read_exact(source, 8) != _SIGNATURE:
        raise ValueError("invalid PNG signature")
    length, kind = struct.unpack(">I4s", _read_exact(source, 8))
    if length != 13 or kind != b"IHDR":
        raise ValueError("missing PNG header")
    header = _read_exact(source, 13)
    _check_crc(source, zlib.crc32(header, zlib.crc32(kind)))
    color, depth, rows = _header_rows(header)
    pixels = _Pixels(rows)
    seen_palette = False
    seen_data = False
    data_closed = False
    while True:
        length, kind = struct.unpack(">I4s", _read_exact(source, 8))
        if (length > 0x7fffffff or kind[2] & 32
                or not all(65 <= letter <= 90 or 97 <= letter <= 122 for letter in kind)):
            raise ValueError("invalid PNG chunk header")
        if kind != b"IDAT" and seen_data:
            data_closed = True
        if kind == b"IHDR":
            raise ValueError("duplicate PNG header")
        if kind == b"PLTE":
            if (seen_palette or seen_data or color in (0, 4) or not 0 < length <= 768
                    or length % 3 or (color == 3 and length // 3 > 1 << depth)):
                raise ValueError("invalid PNG palette")
            seen_palette = True
        if kind == b"IDAT":
            if data_closed or (color == 3 and not seen_palette):
                raise ValueError("invalid PNG image data order")
            seen_data = True
        if kind not in _CRITICAL and not kind[0] & 32:
            raise ValueError("unknown PNG critical chunk")
        if kind == b"IEND" and (length or not seen_data):
            raise ValueError("invalid PNG end chunk")
        checksum = _chunk_data(source, length, kind, pixels if kind == b"IDAT" else None)
        _check_crc(source, checksum)
        if kind != b"IEND":
            continue
        pixels.finish()
        if source.read(1):
            raise ValueError("data follows PNG end chunk")
        return
```

**Modify:** `tools/testing/release_evidence.py`。

- [x] 导入原 PNG 校验函数，流式枚举文件；在原错误边界中打开、验证和关闭 descriptor，并保留 cause。缺失与损坏沿用原错误码。

```python
from tools.testing.png_evidence import verify_png

browser_dir = evidence / "browser"
found_screenshot = False
try:
    for path in browser_dir.rglob("*.png"):
        if not path.is_file():
            continue
        found_screenshot = True
        with path.open("rb") as source:
            verify_png(source)
except (OSError, ValueError) as exc:
    raise EvidenceError("browser_evidence_invalid") from exc
if not found_screenshot:
    raise EvidenceError("browser_evidence_missing")
```

该代码位于既有 `verify_release_evidence` 的 browser boundary；函数其余 Git/矩阵/JUnit/外部检查和返回计数保持。

## Task 3：完整边界回归与验收矩阵

**Files:** `tests/test_release_evidence.py`、`docs/testing/journeys.json`。

- [x] 执行同一红色命令，确认原八例变绿；覆盖有效类型/位深、调色板、分片 IDAT、Adam7、跨64KiB输入/解压边界和 partial read。
- [x] 覆盖非法 header/CRC/palette/chunk 顺序、压缩流截断/额外流、decoded 超长/缺失/filter、原 descriptor 关闭与 OSError/zlib cause。
- [x] 新测试全部经原 release entry 或使用实际文件读取器；在 GATE-01 登记实际 selectors。新增16 selectors/79 cases，并纳入原多文件损坏回归，GATE-01共30 selectors/133 cases。

`tests/test_release_evidence.py` 最终113个实例全部通过。关联集合还包括原 journey、package/deploy fixture；四模块双 Python 各208 passed、2 passing subtests、0 failures/errors/skips。最终记录为 `/tmp/agent-fleet-release-png-related-final-20261027.json`；JUnit 为 `/tmp/agent-fleet-release-png-related-final-python310-20261027.xml` 与 `/tmp/agent-fleet-release-png-related-final-python313-20261027.xml`。873个源码文件摘要在两组运行前后完全一致；PNG源文件与本计划实现代码块一致，函数控制流深度至多3。

## Task 4：新鲜验证与文档同步

**Files:** 本计划、`docs/testing/test-chains.md`、`docs/platform/workflow-platform/implementation-progress.md` 与开发入口。

- [x] Python3.10.21 与 3.13.15 串行运行 `tests/test_release_evidence.py tests/test_journey_gate.py tests/test_release_layout.py tests/test_auto_deploy.py`，检查实际 JUnit 与进程退出码。
- [x] 用现有44张实际浏览器截图验证新 parser 兼容；执行完整严格门禁，保留 Linux skip/not_run 原事实与源码摘要。冻结证据 `/tmp/agent-fleet-release-png-full-20261027-kqg47qjf`：5718 passed、0 failures/errors、5平台skip、195 passing subtests，实际5723 testcase；41/42 journeys，严格退出1、ci_status=failed。GATE-01为30/133，WF-06为413/1052，WF-08为201/451，三组全部通过；873文件摘要前后一致，矩阵摘要为 `759e48d6829e22c2ceac70f574713b0d51f06a644f94eb29737354c7bb313bea`。44张新截图通过实际parser；原release entry以dirty_checkout拒绝开发报告。
- [x] 执行双 Python compileall、UTF-8/no-BOM/AST、实际新增 selector与本地链接、`git diff --check`。2026-10-27预检 `/tmp/agent-fleet-release-png-preflight-20261027.json`：851份文本、112个本地链接和1480个不同required selector定义有效，PNG九个函数参数/返回有注解，最大控制流深度3；Python3.10/3.13 compileall与diff检查退出0。未配置静态类型检查器，不以语法检查冒称类型证明。
- [x] 按上述证据更新本计划、证据核验说明和实施进度；整个项目保持未完成，剩余 Linux/Windows/真实试点和未批准业务语义不推定完成。

## Linux 完整门禁续接（用户已授权）

- SSH依据为Obsidian的 `ssh-credentials` → `ssh-config` / `VPS 总览`；荷兰主手册明确其为轻量独立沙盒。实际 `mesh-nl` 的pidfd/proc可用，但只有0.25 vCPU且缺pytest/Node/浏览器，不在该机器安装完整浏览器工具链。
- 项目canonical索引记录了pxed隔离测试。实际 `mesh-pxed` 有Python3.10.12、Node22.21.1和旧测试venv；本次另建临时目录，复用既有Python依赖，补入既有WebSocket/Playwright源码。现有Chromium与Playwright1.63.0要求不一致，已获用户批准下载官方Chromium153到本次临时目录。
- 原开发分支继续保留未提交工作。以全部Git可见文件的逐项SHA-256冻结源码；单独临时Git测试快照产生独立revision，必须记录其与开发HEAD、源码摘要的关系，不将新代码冒记为原HEAD。
- 在临时clean checkout运行原 `python -m pytest tests --require-journeys` 与 `python -m tools.testing.release_evidence`。不拼接两台机器的报告、不更改ci_status/skip/not_run、不增加第二个核验器。
- [x] 留存Linux完整运行、原核验入口的实际退出、源码前后摘要、JUnit/journeys/截图和依赖配置；依据实际结果更新实施进度。Windows和真实外部验收仍独立开放。

### 2026-10-29 续接状态

- 冻结测试 revision 为 `15d90687fb988b8f4e97c312486a83bc3f750ef4`，独立于开发 HEAD；873 个文件的 SHA-256 保存在 provenance 中。源码包 SHA-256 为 `32d5cfcdde6e723be4bd55f467b3611330329bdfb70584a68c9007d726a72f8a`。
- pxed 原 Linux 进程子集 JUnit 实际为 78 testcase、0 failure/error/skip，命令退出0；它不是完整 RELEASE-01 或全仓通过证据。Chromium153 原夹具仍有失败，生命周期/协议/启动参数诊断出现过成功和失败，尚未定位稳定根因；没有修改依赖库、启动参数、断言期限或 skip 来绕过失败。
- 用户批准将已有 Node22.21.1 和官方 Chromium153.0.8010.12 复制到 gz-cvm 本次临时目录。该机 Python3.13.16，与 CI Python3.10 的差异保留。873 个源文件逐项匹配、clean checkout；同版本原始浏览器夹具为 **1 passed，3.77 秒，退出0**。此前 Node24/Chromium156 的通过仅是版本不同的诊断对照。
- gz-cvm 完整原门禁实际为 **5716 passed、3 failed、4 skipped、195 passing subtests，42/42 journeys**，命令退出1；失败是 root 权限绕过导致的只读/不可读测试，以及非 `LC_ALL=C` 的排序断言。禁用 `CAP_DAC_OVERRIDE/CAP_DAC_READ_SEARCH` 的两个权限测试均通过，`LC_ALL=C` 下排序测试通过。原 release verifier 对该 `ci_status=failed` 证据返回 `journey_incomplete`，因此 RELEASE-01 仍未通过。
- 准备阶段曾误沿旧诊断符号链接触达 gz-cvm 既有 Chromium156 缓存，现场与受影响文件清单记录于 `runtime-symlink-incident.json`；用户已批准恢复官方下载，2026-10-29 恢复 Chromium156.0.8078.4 完成，301个归档文件逐项匹配；归档SHA-256 `43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79`。原受影响目录单独保留；153 测试使用实体私有浏览器目录，审计见本地证据 `gz-cvm/chromium156-restoration-audit.json`。
- 本地持久证据目录为 `/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b`；gz-cvm 临时证据仍在 `/tmp/agent-fleet-linux-control-20261028.6uddg5ud`，不得删除。不同主机、Python版本和局部结果不得拼接为一个完整通过报告。

### Task 5：打包清单跨 locale 的确定性

基准为 `deploy/frontend-release-layout.md` 的稳定排序合同。原打包脚本使用调用者 locale 的 `sort`，英文 UTF-8 locale 把 `THIRD_PARTY.md` 排到小写路径之后，与 Python `sorted` 和 C locale 不同；真实完整门禁与 `LC_ALL=C` 对照已确认此根因。最小修复只在原排序命令设置 `LC_ALL=C`；保持 shell/stdlb 工具和原目录/凭据检查，不新增 facade、依赖或服务。枚举与排序为 O(n log n)、清单空间 O(n)。

- [x] 在 `tests/test_release_layout.py` 加入实际打包回归：临时静态源包含大写许可证和小写资源，在 `C` / `en_US.UTF-8` 环境分别生成独立目录，断言完整清单和 manifest 字节一致。先在 gz-cvm 冻结源码副本验证失败，再修改脚本。
- [x] `deploy/package-frontend-release.sh` 仅将枚举中的 `sort` 改为 `LC_ALL=C sort`，在原测试入口登记并复跑回归与相关发布模块。
- [x] 冻结新的明确 revision 后重新运行完整门禁，保留禁用 DAC 绕过能力的启动范围和全部真实 skip；任何平台 skip 仍由原 verifier 拒绝，不放宽核验合同。

### 2026-10-30 已核实后续结果

跨 locale 回归在真实 `en_US.UTF-8` 对旧脚本失败；`LC_ALL=C sort` 修复后 1 passed / 2 passing subtests，证据 `evidence/locale-final/command.json`。之后冻结 revision `1a4e4a29ff92760955d10ed74d52b3c70ec774aa`，873份源码摘要前后相同，Node22.21.1 / Playwright1.63.0 / Chromium153.0.8010.12 / Python3.13.16 / LC_ALL=C，移除 DAC override/read-search 并 NoNewPrivs=1。原全仓实际 **5720 passed、4 skipped、197 passing subtests、42/42 journeys**，pytest退出0。原release verifier退出1、`junit_incomplete`，原因四个 macOS sandbox 专项 skip，不能称为严格发布通过。44张PNG完整验证，七项外部检查not_run。此前遗漏Node22 PATH的中止运行单独记录在 `retry-20261029/evidence/full/aborted-environment.json`，不作通过证据。

该次历史证据位于本地证据根 `gz-cvm/retry-20261029/controlled-full`，含 full-command、results.xml、journeys、release-verifier、verified-summary 与 44 张图。四个 skip 的 Linux 实际等价测试已按[Linux沙箱计划](2026-10-29-linux-workflow-sandbox.md)实施；三模块 Linux 50 passed、macOS 双 Python 各 50 passed。

后续冻结 revision `8b93185d81dab3eb2442ef8fd0b863dc8eda2552` 以相同 Node 22 / Playwright 1.63 / Chromium 153 / Python 3.13.16、移除 DAC 绕过能力和 `LC_ALL=C` 配置运行原全仓：**5724 passed、197 passing subtests、0 failures/errors/skips、42/42 journeys**，pytest 与原 release verifier 均退出 0。876 个源码摘要前后相同，44 张 PNG 完整核验；七项外部检查保持 `not_run`。完整证据位于 `/Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-full-20261030-nzb5qq22/`，51 个文件已与远端逐字节摘要匹配，本地复跑原 verifier 也退出 0。Windows、真实两机/模型/CLI、生产隔离和真实 MCP/Muse 验收仍开放。
