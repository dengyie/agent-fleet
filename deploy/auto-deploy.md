# main 自动部署

用户于 2026-10-04 授权将 main 的发布从手工切换改为自动部署。当前链路是 `test → package → deploy`：完整 pytest/浏览器/journey 门禁通过后，下载**同一次 workflow** 的 `agent-fleet-release` 与 `agent-fleet-frontend`，校验并上传到宿主专用入口。

## 触发与权限

- 仅 main 的 push 或 main 上手动运行 CI 会部署。PR 的 test/package 不读取部署 secrets，PR 不进入 production job。
- GitHub `production` environment 的 deployment branch policy 必须只允许 main。部署 job 只授予 contents/actions read 和 deployments write。
- main workflow 和 production job 均不取消正在执行的部署；宿主另有非阻塞文件锁。部署前查询 main SHA，过期版本跳过；宿主拒绝比上次成功 run 更早的 run。
- 使用专用 Ed25519 key，服务器 authorized_keys 对该 key 设置 `restrict,command=...`。它只能调用预装 receiver，不提供任意 shell、PTY、端口转发或 SCP。
- SSH 主机公钥通过已有可信连接获取并钉在 `SSH_KNOWN_HOSTS`；不使用 `StrictHostKeyChecking=no` 或临时信任 ssh-keyscan。

`production` 环境需设置 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_KNOWN_HOSTS`。应用账号、模型、SMTP、ingest 凭据留在服务器，不放入 GitHub。应用账号探针只检查登录、readiness 和旧 Cookie 撤销，不发送模型任务或邮件。

## 宿主一次性配置

宿主需要 Python 3.10+、systemd、Docker 和 Nginx；容器继续使用已验证的 canonical launcher。以 `auto-deploy.example.json` 为字段模板，在宿主创建 root:root、0600 的 `/etc/agent-fleet-deploy.json`；填写**当前环境已核实**的路径和容器用户，不能直接执行示例路径。

把以下四个已审查文件安装到 root:root、0755 目录 `/usr/local/lib/agent-fleet-deploy/`，文件权限 0644：

```text
auto_release.py
auto_deploy_host.py
auto_deploy_container.py
auto_deploy_ssh.py
```

创建 root-owned、0755 的 `/usr/local/sbin/agent-fleet-deploy`：

```sh
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/agent-fleet-deploy/auto_deploy_ssh.py "$@"
```

在现有部署账号的 authorized_keys **追加**专用公钥行，保留其他 key：

```text
restrict,command="sudo -n /usr/local/sbin/agent-fleet-deploy \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC_KEY agent-fleet-ci
```

部署账号需有执行上述固定命令的 sudo 权限；不要为了部署给新 key 开放无约束的 SSH shell。配置的 state 目录必须 root-owned 0700，账号 JSON 必须 root-owned 0600。应用代码不能写入 receiver 和配置所在路径。更新 receiver 是独立的运维操作，不从收到的发布包执行 root 安装脚本。

## 发布事务

1. Receiver 校验命令、完整 SHA、run ID、压缩包大小（最多 128 MiB）和 SHA-256；传输限时 120 秒。tar 拒绝路径穿越、软/硬链接、设备、重复项、非法顶层文件、超过 30000 项或展开超过 512 MiB。剩余磁盘不足 1 GiB 时拒绝新上传。
2. 使用 systemd 启动独立事务；SSH 断开不杀部署。事务保存 `pending.json`，防止进程异常中断后下一次发布覆盖现场。systemd 运行上限 600 秒，TERM 留 120 秒完成回滚。
3. 检查原实例的唯一 Hub/guardian、活动 Run/任务、账号会话、readiness；在容器临时目录导入候选代码。版本必须与后台 RELEASE_ORIGIN、前端 manifest 和同一个 run 完全一致。
4. 保存旧代码和 Nginx，建立现有 launcher 的维护屏障；Nginx 临时返回 503，阻止公网新请求。再查活动任务；如果已有新任务，恢复访问并退出，不停止任务。
5. 只停止 UID、cwd、脚本和进程启动时间均匹配的 guardian/Hub；停写后对配置中的每个 SQLite 数据库使用 backup API，并检查 integrity/FK。
6. 在原 LIVE 目录内更新有限的发布管理路径。**不覆盖** `fleet-gates.conf`、`hosts.yaml`、`.env`、`credentials/`、`state/`、`var/`，不改变 LIVE inode。前端部署到不可变的完整 SHA 目录。
7. 使用原 launcher 恢复 Hub/guardian；检查版本、healthz、匿名401、账号登录/readiness、退出后旧 Cookie 401；切换 Nginx root 并检查公网 manifest SHA。Nginx reload 返回后新 worker 可能尚未接管，公网只读探针在统一 30 秒预算内等待维护503、旧版本或暂时网络失败消失；每次请求最多使用剩余预算，截止后返回成功也不得通过。
8. 保存成功记录，清除 pending。成功记录提交中途失败时，回滚同时恢复旧成功指针；回滚记录写入失败仍保持维护屏障。重复部署同一成功 SHA 只核验，不再重启。失败记录保留原因类别，不输出凭据、Cookie 或原始应用响应。

自动探针不能证明外部模型、邮箱送达或真实远程执行能力；按 [发布验收](../docs/testing/release-acceptance.md)分别留证。维护期间访问会短暂返回 503；本实现不承诺零停机发布。

## 验证与上线

先下载当前 main 的已通过 CI 产物，用包装命令生成 bundle：

```bash
python3 deploy/auto_deploy_ssh.py bundle \
  --backend /private/backend/agent-fleet-release-FULL_SHA.tgz \
  --frontend /private/frontend --output /private/bundle.tgz \
  --sha FULL_SHA --run CI_RUN_ID
```

按 workflow 的 SSH 调用方法把首个命令词改为 `check`。`check` 会校验包、候选导入和真实账号探针，**不维护停机、不复制 LIVE、不重启、不切换 Nginx**。账号登录/退出仍会产生正常会话审计数据。对照前后的 RELEASE_ORIGIN、inode、Hub/guardian PID、gates/hosts 摘要，确认不变。

代码检查：

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_auto_deploy.py tests/test_release_layout.py -q
```

完整检查仍使用 [测试入口](../docs/testing/README.md)中的门禁命令。新增用例执行真实临时文件/SQLite 事务，注入宿主边界故障，验证部分复制失败、探活失败、活动任务竞态、状态提交失败、回滚失败、数据保留、包校验、SSH 协议、缺失结果不假绿和 CI 权限。首次真实切换由本 workflow 合入 main 后触发；PR 测试通过不等于已上线。

## 失败与恢复

- `rolled_back`：旧代码和 Nginx 已恢复，旧应用的账号/健康探针通过；CI 仍失败，不能记作新版本发布成功。
- `rollback_failed` / `unfinished_deployment_requires_recovery`：保留 `pending.json`、维护屏障及备份目录，阻断下一次自动部署。若新版本已有活动任务，停止会被拒绝，需等待其终态后按环境运维手册恢复。
- `unconfirmed`：SSH/systemd 未返回完整结果；先查询 `systemctl status agent-fleet-deploy-*` 和 state 下对应 job/result/pending，**不要自动重发**。
- 备份目录包含旧代码、Nginx 和停写时数据库快照。自动回滚**永不恢复数据库**；恢复旧 DB 需要独立确认数据影响。出现不兼容迁移时保持维护，按环境手册人工修复。
- GitHub 保存 `agent-fleet-deployment` 结果 14 天；宿主保留结果与备份，上传/解包临时文件在 job 完成后清理。代码、数据库和前端历史版本不自动删除，定期按保留策略清理；磁盘不足时拒绝发布以保护当前服务。
- 禁用自动部署可移除 environment 的 SSH key secret，并删除宿主对应 authorized_keys 行；不要撤销其他运维 key。已有 systemd 事务应先查状态，不能通过删除 key 来推断已停止。
