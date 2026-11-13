#!/usr/bin/env python3
"""tools/fleet-client.py — 节点客户端生命周期 CLI（安装/更新/回滚/诊断）。

用法:
  python3 tools/fleet-client.py status [--json]
  python3 tools/fleet-client.py check
  python3 tools/fleet-client.py update [--dry-run] [--force] [--grace 90]
  python3 tools/fleet-client.py recover
  python3 tools/fleet-client.py rollback
  python3 tools/fleet-client.py locks [--reclaim-stale]
  python3 tools/fleet-client.py migrate-layout [--reload]

更新只信 hub 清单的 Ed25519 签名（runner.yaml 的 supervisor public_key）；旧进程
通过 drain 文件在两轮之间自愿退出，updater 从不 kill 进程。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap_direct_imports():
    if __package__ not in (None, ""):
        return
    root = Path(__file__).resolve().parent.parent
    sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != root / "tools"]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_direct_imports()

from fleet_core.locking import inspect_lock, reclaim_stale
from tools import runner_config
from tools.client_update import runtime
from tools.client_update import services as svc
from tools.client_update.installer import (
    HubFetcher,
    Installer,
    Layout,
    UpdateError,
    trusted_keys,
)

EXIT_ERROR = 1
EXIT_CONFIG = 2


def _emit(args, value, text: str) -> None:
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)


def _installer(args, *, need_hub: bool) -> Installer:
    cfg = None
    if need_hub:
        cfg = runner_config.load_config(args.config)
    return Installer(
        layout=Layout(runtime.client_home()),
        public_keys=trusted_keys(cfg) if cfg else [],
        fetcher=HubFetcher(cfg) if cfg else None,
        services=svc.discover(),
        grace_s=getattr(args, "grace", 90.0),
        log=lambda msg: print(f"[fleet-client] {msg}", file=sys.stderr),
    )


def cmd_status(args) -> int:
    st = _installer(args, need_hub=False).status()
    lines = [f"客户端目录: {st['home']}",
             f"当前版本:   {st['current'] or '（未安装，服务仍跑旧布局）'}"
             + (f"  seq={st['sequence']}" if st["sequence"] is not None else ""),
             f"上一版本:   {st['previous'] or '-'}",
             f"已接受序号: {st['observation'].get('sequence', 0)}"
             + (f"  失败序号={st['observation']['failedSequence']}"
                if st["observation"].get("failedSequence") else ""),
             "服务:"]
    for item in st["services"]:
        mark = "已托管" if item["managed"] else "旧布局"
        lines.append(f"  {item['role']:<15} {item['label']:<40} {mark}")
    if not st["services"]:
        lines.append("  （未发现 agent-fleet 服务）")
    lines.append("运行回执:")
    for role, receipt in st["runtime"].items():
        alive = {True: "存活", False: "已退出", None: "未知"}[receipt.get("alive")]
        lines.append(f"  {role:<15} v={receipt.get('version')} pid={receipt.get('pid')} {alive}")
    if st["pendingTransactions"]:
        lines.append(f"未完成事务: {len(st['pendingTransactions'])}（运行 recover）")
    _emit(args, st, "\n".join(lines))
    return 0


def cmd_check(args) -> int:
    result = _installer(args, need_hub=True).check()
    m = result["manifest"]
    text = {"install": "有新版本", "up_to_date": "已是最新",
            "skip_failed": "新版本曾安装失败（update --force 重试）"}[result["action"]]
    _emit(args, result, f"{text}: {m['version']} seq={m['sequence']} "
                        f"size={m['artifact']['size']}")
    return 0


def cmd_update(args) -> int:
    result = _installer(args, need_hub=True).update(force=args.force, dry_run=args.dry_run)
    if result["action"] == "installed":
        text = (f"已安装 {result['version']} seq={result['sequence']}  "
                f"已接管={result['verified'] or '-'} 仍在收尾={result['draining'] or '-'}")
        if result["unmanaged"]:
            text += f"\n旧布局服务未切换: {result['unmanaged']}（运行 migrate-layout）"
    else:
        text = f"{result['action']}: {result['version']} seq={result['sequence']}"
    if result.get("recovered"):
        text += f"\n已恢复中断事务: {result['recovered']}"
    _emit(args, result, text)
    return 0


def cmd_recover(args) -> int:
    done = _installer(args, need_hub=False).recover()
    _emit(args, done, "\n".join(f"{d['txid']}: {d['from']} → {d['to']}" for d in done)
          or "没有未完成事务")
    return 0


def cmd_rollback(args) -> int:
    result = _installer(args, need_hub=False).rollback()
    _emit(args, result, f"已回滚到 {result['current']}（原 {result['previous']}）")
    return 0


def _lock_paths(args) -> list[Path]:
    paths = [runtime.client_home() / "state" / "updater.lock"]
    try:
        cache = runner_config.load_config(args.config).cache_dir
    except Exception:
        cache = runner_config.DEFAULT_CACHE_DIR
    if cache.is_dir():
        paths.extend(sorted(p for p in cache.rglob("*.lock") if p.is_file()))
    return list(dict.fromkeys(paths))


def cmd_locks(args) -> int:
    states = []
    for path in _lock_paths(args):
        state = inspect_lock(path)
        if args.reclaim_stale and state.state == "stale" and reclaim_stale(path):
            state = inspect_lock(path)
        states.append(state.to_dict())
    label = {"absent": "不存在", "free": "空闲", "held": "被持有", "stale": "残留(持有者已死)"}
    lines = []
    for st in states:
        owner = st["owner"] or {}
        who = f" {owner.get('purpose')} pid={owner.get('pid')}" if owner else ""
        lines.append(f"{label[st['state']]:<12} {st['path']}{who}")
    _emit(args, states, "\n".join(lines) or "没有锁文件")
    return 0


def cmd_migrate(args) -> int:
    layout = Layout(runtime.client_home())
    if layout.link_version(layout.current) is None:
        print("[fleet-client] 尚未安装托管版本，先运行 update", file=sys.stderr)
        return EXIT_ERROR
    results = []
    for service in svc.discover():
        backup = svc.migrate(service, layout.current)
        reloaded = svc.reload(service) if (backup and args.reload) else None
        results.append({**service.to_dict(), "backup": str(backup) if backup else None,
                        "reloaded": reloaded})
    lines = []
    for r in results:
        if r["backup"] is None:
            lines.append(f"{r['role']:<15} 已是托管布局")
        else:
            state = {True: "已重载", False: "重载失败", None: "待重载（--reload 或重新登录生效）"}
            lines.append(f"{r['role']:<15} 已改写，备份 {r['backup']}  {state[r['reloaded']]}")
    _emit(args, results, "\n".join(lines) or "未发现 agent-fleet 服务")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="agent-fleet 节点客户端生命周期")
    parser.add_argument("--config", default=str(runner_config.DEFAULT_CONFIG_PATH))
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("check").set_defaults(func=cmd_check)
    up = sub.add_parser("update")
    up.add_argument("--dry-run", action="store_true")
    up.add_argument("--force", action="store_true", help="重试曾失败的同一序号")
    up.add_argument("--grace", type=float, default=90.0, help="等待新版本接管的秒数")
    up.set_defaults(func=cmd_update)
    sub.add_parser("recover").set_defaults(func=cmd_recover)
    sub.add_parser("rollback").set_defaults(func=cmd_rollback)
    lk = sub.add_parser("locks")
    lk.add_argument("--reclaim-stale", action="store_true")
    lk.set_defaults(func=cmd_locks)
    mg = sub.add_parser("migrate-layout")
    mg.add_argument("--reload", action="store_true", help="改写后立即重载服务（会重启）")
    mg.set_defaults(func=cmd_migrate)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except runner_config.ConfigError as exc:
        print(f"[fleet-client] 配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except UpdateError as exc:
        print(f"[fleet-client] {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
