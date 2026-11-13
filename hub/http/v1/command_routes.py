"""hub/http/v1/command_routes.py — runner 命令路径 /api/v1 兼容适配器

Task 18 决定：**不注册** runner v1 命令路径。

理由：runner poll/heartbeat/result 的完整兼容契约（lease TTL、attempt_id 幂等、
nonce、bounded 日志转发、机器身份必须来自认证后的 ``g.runner_machine``）目前只
在旧 ``/api/commands/*`` 上被完整覆盖与验证。在未对 v1 命令路径写出完整等价的
契约测试之前，不提供第二个并行 runner 表面；旧 ``/api/commands/*`` 仍是
已部署 runner 的权威接口。

（无 view / blueprint 定义；若未来需要 v1 命令表面，应复用本包其它模块的
“同 callable 重挂 URL”手法，并补充 poll/heartbeat/result 全契约对等测试。）
"""