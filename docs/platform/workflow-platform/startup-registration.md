# Workflow 启动登记

返回 [Workflow 入口](README.md)、[实施计划](../../superpowers/plans/2026-11-11-workflow-startup-registration.md)及[开发完成账](../development-completion.md)。本页描述候选代码配置，不表示当前线上已启用。

## 现有对象与启动顺序

Hub 的普通 `hub.web.make_app()` 入口可读取 operator 管理的 JSON 文件，装配原有 `coding-delivery@1` 以及 Native 文本、代码和媒体输入执行器。不会在请求中接受可执行命令，也不会仅因加载配置启动后台线程。

先在原平台库准备启用的 owner/workspace 及同 owner/workspace 且未归档的 conversation，使用下节的初始化命令。coding-delivery 要求三个独立会话；Native 按模板中 1–8 个 step 的 binding 准备会话，共用的 conversation 只创建一次。workspace 使用 Hub 实际可访问的绝对目录。开发 machine/project 必须已被原 Tasks 项目白名单授权，Runner 使用原注册与轮询方式。评审命令及 launcher 必须是实际可执行的绝对 argv；它们由运维提供，需实现原 `CommandReviewAdapter` 合同：从环境变量 `FLEET_REVIEW_REQUEST` 指向的文件读取 JSON 请求，向 stdout 返回 JSON 报告。不要将测试夹具当作生产评审器。

配置路径解析顺序：显式 `make_app(platform_workflow_registration_file=...)` → `AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE` 环境变量 → `FLEET_HOME/fleet-gates.conf` 同名字段。显式空串或存在但为空的环境变量关闭文件登记，不回退到文件。配置必须是绝对路径、UTF-8 无 BOM JSON 普通文件，最大 256 KiB，`schema_version` 必须为整数 1，`coding_delivery` 与 `native` 至少有一项且总数不超过 32。

```ini
AGENT_FLEET_PLATFORM_ENABLED=1
AGENT_FLEET_PLATFORM_WORKFLOWS_ENABLED=1
AGENT_FLEET_PLATFORM_WORKER_ENABLED=1
AGENT_FLEET_PLATFORM_WORKER_SCHEDULER_ENABLED=1
AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE=/var/lib/agent-fleet/coding-delivery.json
AGENT_FLEET_PLATFORM_WORKFLOW_DISPATCH_ENABLED=0
```

将文件放在原部署保留的持久目录中。先关闭 dispatch 启动，登录检查 `GET /api/platform/v1/workflow-templates`，应出现 `coding-delivery`。在所有执行器可用后再打开 dispatch。生产多 owner 使用原 worker scheduler；单个匹配的 `dev_operator` 只用于开发。必须同时启用 Tasks；关闭 Workflow 总开关不会加载此文件。开启 dispatch 要求 worker 和 scheduler（或匹配的开发身份）、可用项目及本地测试 launcher。

### Native 文本、代码和图片/音频输入

Native 入口与 `coding_delivery` 可以放在同一个登记文件中。Native 每个 binding 必须固定一个仍处于启用状态的 `model_profile_id`，并同时保存完整的 `provider_snapshot`；snapshot 只包含 provider、model、provider_config、capabilities 和可选的 `secret_ref` 引用，不保存 secret 值。Hub 启动时只用当前模型目录确认该 profile 仍获准，实际请求使用 manifest 中固定的 snapshot，因此模型 endpoint、模型名、能力或 secret 引用在重启后改变时，已排队 Run 仍按原配置恢复。

```json
{
  "schema_version": 1,
  "native": [{
    "owner_id": "operator-a",
    "workspace_id": "workspace-a",
    "definition": {"schema_version": 1, "template_id": "native-input", "revision": 1,
      "acceptance_required": true,
      "steps": [{"step_key": "develop", "kind": "native_run", "requires": [],
        "contract": {"schema_version": 1, "output_specs": [], "input_refs": [],
          "context_refs": [], "capability_requirements": [],
          "policy_ref": {"id": "safe", "revision": 1}, "validator_refs": [],
          "retry_class": "manual_only",
          "budget": {"max_tokens": 100, "max_model_requests": 2,
            "max_active_seconds": 60, "max_wait_seconds": 120}}}]},
    "bindings": [{"step_key": "develop", "target_key": "native-a",
      "conversation_id": "native-chat", "model_profile_id": "local",
      "provider_snapshot": {"provider": "openai_compatible", "model": "configured-model",
        "provider_config": {"endpoint": "https://model.example/v1"},
        "capabilities": {"image_input": true, "audio_input": true},
        "secret_ref": "env://MODEL_KEY"},
      "input_mode": "media"}]
  }]
}
```

`input_mode` 支持 `text`、`code`、`text_code` 和 `media`。Native 当前要求本地执行节点、已启用 workspace/conversation/model，且一个 step 只能有一个 binding；视频、CLI/review/test、远程 context media 仍不在此入口。修改 provider 配置或能力时应生成新的 manifest/revision，并先用关闭 dispatch 的 Hub 启动检查模板和 target，再开启 dispatch。

## 工作区和会话初始化

候选代码提供本地运维入口 `python3 -m hub.workflow_setup`，读取同一份登记文件，精确选择一项 owner/workspace，准备该项的工作区与会话。coding-delivery 创建 develop/review/test 三个独立会话；2026-11-13 的开发增量也支持 Native binding 初始化，该增量尚未包含在公开 PR42 中。先保存登记文件并准备实际存在的工作区目录，再运行预览。以下路径为部署结构示例，必须使用当前 Hub 的真实数据库及对应 Python，不能误把另一空库当作生产库；未显式配置时原平台库默认在 release root 的 `var/platform/platform.db`。

```bash
python3 -m hub.workflow_setup \
  --database /srv/agent-fleet/var/platform/platform.db \
  --registration /var/lib/agent-fleet/coding-delivery.json \
  --owner operator-a --workspace workspace-a \
  --root /srv/workspaces/project-a
```

默认只预览：数据库不存在时也不会创建目录或数据库；已有数据库使用只读连接，不初始化 schema。成功输出 `mode=preview` 及 `create/existing` 数量。确认绑定后，用同一命令追加 `--apply`，通过原 `PlatformRepository.init()` 初始化/迁移 schema，再在一个写事务中创建缺失的工作区和会话；不会启动任何 Workflow、Runner、模型或 Node 命令。退出码 0 为完成，2 为配置、冲突或存储错误。

Native 初始化使用同一命令，`--template` 精确匹配 `definition.template_id`：

```bash
python3 -m hub.workflow_setup \
  --database /srv/agent-fleet/var/platform/platform.db \
  --registration /var/lib/agent-fleet/native-input.json \
  --owner operator-a --workspace workspace-a \
  --root /srv/workspaces/project-a --template native-input
```

无 selector 时，同一 owner/workspace 存在 coding-delivery 就保持原 coding 选择；否则要求恰好一个 Native 条目。多 Native 模板需分别指定 `--template`；指定 `coding-delivery` 时也检查实际匹配项，与同名 Native 条目冲突则拒绝。缺失、歧义或不合法的 Native 定义/binding 在创建数据库前退出 2。计数按不同 conversation ID 计算，共用会话的 title 取首次出现的 step。模型目录和 provider secret 仍须另行配置；apply 成功只证明对象已准备，随后应以关闭 dispatch 的普通 Hub 加载同一 manifest，检查模板后再进行真实任务验收。实现与回归见 [Native 初始化计划](../../superpowers/plans/2026-11-13-native-workflow-setup.md)。

重复 apply 的新增数量为零；已有工作区 name/default_node、会话 title/overrides/revision 和历史保持原样。已有工作区必须启用、使用 directory backend、root 与指定目录解析后相同；已有会话须同 owner/workspace 且未归档。跨 owner 的同名会话、错误 workspace、已归档/停用对象和 root 漂移会明确拒绝，不自动重绑或复活。所有既有对象先核对再插入；任意会话插入失败会回滚该项的全部内容写入。原 schema 初始化单独遵循原平台迁移合同，不等同于内容事务。

原平台初始化在基础建表脚本结束后取得迁移写锁，再检查和新增列；多个进程共用同一库时，后来的进程会在取得锁后重新检查已提交的列。新增列与后续迁移同事务，后续迁移失败时新增列也回滚；基础 `CREATE TABLE IF NOT EXISTS` 不在这个事务中。owner 同时遵守平台身份和 Workflow 的 128-byte 引用限制，超限在创建数据库前拒绝。

每次只处理一项明确的 owner/workspace，多项 manifest 分别执行；每项为独立事务。该命令只准备本机 directory 工作区与会话，不创建账号、项目授权、Node 身份/凭据、模型配置或 launcher，也不修复已运行流程。owner 必须对应实际使用的 operator 身份；不能将成功初始化当作业务执行通过。实现与故障/竞争证据见[初始化计划](../../superpowers/plans/2026-11-11-workflow-bootstrap.md)。

## 本地固定测试

下面是结构示例，路径、owner、conversation 和程序必须换成实际值。评审身份必须与开发身份不同；同 adapter/revision 不可登记不同实现。

```json
{
  "schema_version": 1,
  "coding_delivery": [{
    "owner_id": "operator-a",
    "workspace_id": "workspace-a",
    "develop": {
      "machine": "mac-local",
      "agent_type": "codex",
      "project": "agent-fleet",
      "repository_id": "repo-a",
      "conversation_id": "develop-chat"
    },
    "review": {
      "adapter_id": "registered-reviewer",
      "revision": 1,
      "executor_id": "independent-reviewer",
      "kind": "model",
      "model_id": "configured-model",
      "argv": ["/opt/agent-fleet/bin/review-source"],
      "launcher": ["/opt/agent-fleet/bin/review-launcher"],
      "timeout_seconds": 300,
      "conversation_id": "review-chat"
    },
    "test": {"conversation_id": "test-chat"}
  }]
}
```

本地测试通过环境变量 `AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER` 指定 JSON argv，例如 `["/opt/agent-fleet/bin/test-launcher"]`；该字段目前不从 gates 文件读取。launcher 接收原 backend 的 cwd、命令和清洗后环境，需提供现有固定测试使用的 Python/pytest。不得以普通宿主执行包装冒充隔离 launcher。评审器在冻结源码目录运行，从 `FLEET_REVIEW_REQUEST` 指向的 JSON 文件读取 `schema_version/source_revision/subject_digest/executor`，`FLEET_REVIEW_SOURCE` 指向冻结源码目录；stdout 返回同一身份与 subject、`verdict` 和 `findings`；源码被改写、报告错误或命令失败会使原验证失败。

### 仓库内置 Linux 固定测试启动器

Linux 可直接使用候选源码内的 `fleet_core/workflow/linux_launcher.py`，无需引用 `tests/` 或自行编写启动脚本。运行 Python 必须与 Hub 执行固定 recipe 的解释器一致，且该运行环境已经安装 pytest；系统需已有可执行 bubblewrap 和 libseccomp，内核允许创建 user/mount/PID/network namespace。程序不会安装这些依赖，也不会在缺依赖时改为普通宿主执行。

```bash
export AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER='["/opt/agent-fleet/.venv/bin/python","-I","/opt/agent-fleet/fleet_core/workflow/linux_launcher.py","--fixed-test","--bwrap","/usr/bin/bwrap","--"]'
/opt/agent-fleet/.venv/bin/python -I /opt/agent-fleet/fleet_core/workflow/linux_launcher.py --fixed-test --help
```

以上路径须替换为实际部署位置；最后一个 `--` 必须保留，原 backend 会在它后面追加固定的 Python/pytest 命令。`--fixed-test` 根据本次执行的实际 cwd、当前 Python base/venv 和固定 JUnit 文件名生成原显式参数，因此同一配置可以连续服务多个临时源码目录。未显式提供 `--bwrap` 时查找当前 PATH；常驻进程建议使用绝对路径。

原行为保持：源码只读，仅 HOME/TMPDIR、pytest cache 和 `.fleet-workflow-results.xml` 可写，禁用网络及宿主凭据继承；只接受现有固定 recipe，不能把此入口用作任意 shell。此启动器只负责固定测试，不提供真实模型评审器，也不启用 dispatch。旧测试夹具入口保留兼容，实际实现已放入共享模块。验证范围见[启动器实施计划](../../superpowers/plans/2026-11-11-linux-workflow-launcher.md)。

## 仓库内置模型评审器（Linux）

候选代码还提供 `hub/workflow_model_review.py`，可直接作为上面 `review.argv` 的评审命令；`launch` 子命令提供 Linux namespace 启动器。它复用 Hub 已有的 OpenAI 兼容 provider，读取完整冻结文本源码，发起一次无工具、无重试、非流式模型请求，并原样交给既有报告校验器核对。模型负责给出 `passed`、`failed` 或 `inconclusive`，程序不修补身份或改写结论。它不是 Codex CLI，也不需要试点脚本。

先准备持久配置和单独的 API key 文件。下例值为结构示例；endpoint 必须填写完整的 `/chat/completions` 地址，model_id 必须和 manifest 中评审执行身份完全一致。API key 文件只包含 key，可带末尾换行；不要把真实 key 写入配置、登记文件或仓库。

```json
{
  "schema_version": 1,
  "endpoint": "https://model.example/v1/chat/completions",
  "model_id": "configured-model",
  "api_key_file": "/var/lib/agent-fleet/reviewer.key",
  "timeout_seconds": 180,
  "max_source_bytes": 262144,
  "instructions": "根据源码中的需求与测试进行独立正确性评审。关注功能与性能；安全专项后续单独处理。不得执行源码或声称测试已运行。"
}
```

`timeout_seconds` 支持1–240秒，必须小于 manifest 中评审适配器的总超时；`max_source_bytes` 支持1–4194304字节，计算所有源文件的完整字节数。文件预算超限、存在二进制/非UTF-8文件或请求不合法时，在调用模型前明确失败，不截断或悄悄省略文件。长文件分成带路径、摘要及序号的完整片段，避免既有 provider 的单条消息上限截断源码。输入预算不是模型 token 上限，应按所选模型上下文容量配置；模型截断输出、错误JSON/主体/身份或HTTP失败不生成通过报告，不自动重试。

把配置保存为 `/var/lib/agent-fleet/reviewer.json` 后，用实际 Hub Python 和源码位置生成 manifest 的 `argv`、`launcher`。以下只生成公开配置字段，不读取 key 内容，也不会发模型请求或启用派发：

```python
import hashlib
import json
from pathlib import Path

python = "/opt/agent-fleet/.venv/bin/python"
script = "/opt/agent-fleet/hub/workflow_model_review.py"
config = Path("/var/lib/agent-fleet/reviewer.json")
digest = hashlib.sha256(config.read_bytes()).hexdigest()
base = [python, "-I", script]
pinned = ["--config", str(config), "--config-sha256", digest]
print(json.dumps({
    "argv": [*base, "run", *pinned],
    "launcher": [*base, "launch", *pinned, "--bwrap", "/usr/bin/bwrap",
                 "--source", "{source}", "--control", "{control}",
                 "--scratch", "{scratch}", "--"]
}, ensure_ascii=False, indent=2))
```

把输出两字段放入 manifest 的 `review` 对象，保留适配器版本、独立 executor_id、kind=model、相同 model_id、总超时和 conversation_id。末尾 `--` 必须保留。配置字节SHA256同时固定在命令和启动器中；改 endpoint/model/指令/预算必须重新生成两字段，并按既有不可变 adapter 合同增加 revision。API key 文件内容可独立轮换；配置摘要固定文件路径，不固定 key 字节。

启动器需要已有 bubblewrap；其运行Python环境需安装本项目Hub依赖。它复用已有运行时/源码/控制目录挂载，源码只读，仅 scratch 可写，仅额外挂载 Hub、fleet_core、指定配置与独立key，以及系统DNS/CA文件。模型HTTP需要显式网络访问，因此不能复用禁网的固定测试launcher。不会继承Hub/Runner环境凭据或代理环境；endpoint须从该主机直接可达，不自动透传宿主HTTP代理。未提供实际凭据、网络或有效报告时明确失败，不用fixture补结果。

```bash
/opt/agent-fleet/.venv/bin/python -I /opt/agent-fleet/hub/workflow_model_review.py --help
```

实现及验证范围见[正式模型评审入口计划](../../superpowers/plans/2026-11-11-deployable-model-reviewer.md)。受控HTTP对端证明协议与运行接线；2026-11-11 已另外通过原生产命令及 Linux launcher 完成一次真实模型服务评审，报告校验通过。该次原 endpoint 位于 Mac loopback，使用临时 SSH 转发后才从 Linux 可达；转发已关闭。生产必须配置目标主机实际可达的 endpoint，不能直接复制试点的 `127.0.0.1` 地址。真实服务切片不代替实际生产配置下的完整流程验收。

## 远程 Linux 固定测试

在同一 `test` 对象中增加 `remote`：

```json
{
  "conversation_id": "test-chat",
  "remote": {
    "node_id": "linux-test-a",
    "capability": {
      "capability_id": "coding.test",
      "revision": 1,
      "adapter_id": "fixed-tests",
      "input_schema_ref": "in-v1",
      "output_schema_ref": "out-v1",
      "input_schema": {"type": "object"},
      "output_schema": {"type": "object"},
      "input_schema_hash": "a2c799262a3ce3c19ef5cdd983bf3d12b43ab3c426227091b909dcb7054738c0",
      "output_schema_hash": "a2c799262a3ce3c19ef5cdd983bf3d12b43ab3c426227091b909dcb7054738c0",
      "risk": "workspace_read",
      "retry_class": "manual_only",
      "allowed_owner_roles": ["operator"],
      "max_active_seconds": 3600,
      "result_limit_bytes": 65536
    },
    "architecture": "x86_64",
    "software": {"python": "3.11.2"},
    "residency_alias": "private"
  }
}
```

`capability` 必须是原 `CapabilityDefinition.payload()` 的完整值，和 Node 本地登记的内容完全一致。architecture/software 使用节点实际上报的精确值，不是最低版本表达式。需先启用同 owner 的原 Node 身份，保持原 node-scoped credential 和 Command signing key；密钥沿用原环境/凭据文件加载方式，不写入此 manifest。

Hub 同时启用 `AGENT_FLEET_PLATFORM_REMOTE_EXECUTION_ENABLED`、`AGENT_FLEET_PLATFORM_ARTIFACT_TRANSFER_ENABLED` 和 `AGENT_FLEET_PLATFORM_CAPABILITY_ROUTING_ENABLED`。装配沿用原 `source` / `.tar` 接收、`tool.workflow.test`、签名、journal 和产物合同。同一 owner 的多个 workspace 可显式引用同一 Node；启动时合并兼容节点策略的工作区集合，排序与登记顺序无关。节点仍只有一个固定测试执行槽位，跨工作区的 held/isolated 预约统一占用该槽位；不会按工作区复制容量。节点级 residency 等策略必须一致，冲突会在发布登记前拒绝。每个工作区的 develop/review/test 会话、源码接收目标及 architecture/software 选点条件分别保留；不匹配真实节点报告的工作区继续等待，不会扩大允许的软件范围。32项 manifest 上限保持不变。

共享节点的装配、重启与双进程容量证据见[共享测试节点计划](../../superpowers/plans/2026-11-11-shared-test-node-registration.md)。新增工作区仍须分别完成[初始化](#工作区和会话初始化)，并使用该工作区自己的三个会话ID。Node 本地沿用一个进程、同一凭据、同一 `source` alias 和原 journal，无需为每个工作区启动额外 Node。

此配置只负责 Hub 端装配。常驻 Node 按 [Linux 测试 Node 配置](node-startup.md) 在原 CLI 中启用能力上报、接收目录和固定测试 launcher；仅登记 Node 身份不会自动提供远程测试能力。

## 复验与恢复

```bash
python3 -m pytest -q tests/test_workflow_startup.py tests/test_workflow_e2e.py tests/test_workflow_gates.py
```

这些用例覆盖普通启动、文件优先级、禁派发、已有绑定、远程签名装配，以及 Cookie→Runner→源码→独立评审→真实 sandbox pytest→显式测试身份接受的完整受控链。fixture 接受不代表人工接受，也不代替真实模型、常驻 Node 或生产验证。

关闭 dispatch 可停止新派发，保留 Workflow 总开关以核对原流程。登记文件仍要求已有 workspace/conversation 可用；不要在排空前停用 workspace、归档被绑定会话或移除登记。不要在未核对原 `unknown`/未决执行前关闭整个 Workflow 域或移除 executor。重启后的状态与原任务/Command journal 一致性继续遵循原恢复合同。
