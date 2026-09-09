"""hub/notifier.py — 重要事件通知器

严格的通知策略（用户要求：不要乱通知）：
  - 只有"值得关注"的变化才发通知
  - 包括：agent 变下线 / 恢复上线 / Hermes 状态变化 / 高负载 / 磁盘接近满
  - 常规 scan 无变化 → 零通知

发送通道（可选）：
  - stdout（调用方——如 cron ———捕获后决定是否推送）
  - (预留) 微信/TG 直接推送，读取 credentials/ 或 ~/.hermes 渠道

设计：emit 事件时由 here 判断是否值得推送，只在值得时 output 一行。
"""

import json
import os
import time
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent

# 阈值（与 hosts.yaml 对齐）
DEFAULTS = {
    "high_load": 4.0,
    "disk_high_pct": 90,
    "agent_down_min": 10,
}


def _thresholds():
    try:
        import yaml
        cfg = yaml.safe_load((FLEET_HOME / "hosts.yaml").read_text())
        return cfg.get("thresholds", DEFAULTS)
    except Exception:
        return DEFAULTS


def _time_ago(ts):
    return int(time.time() - ts)


def evaluate_snapshot(machine, snapshot):
    """根据最新快照判断是否有值得通知的告警。

    Returns:
        list[str]: 告警消息（空 = 无需通知）
    """
    alerts = []
    th = _thresholds()

    system = snapshot.get("system", {})
    # 负载
    try:
        load = float(system.get("load", 0) or 0)
        if load > th.get("high_load", 4.0):
            alerts.append(f"{machine} 负载偏高: {load} (阈值 {th.get('high_load')})")
    except (ValueError, TypeError):
        pass
    # 磁盘
    try:
        disk = float(str(system.get("disk_used_pct", 0) or 0).rstrip("%"))
        if disk > th.get("disk_high_pct", 90):
            alerts.append(f"{machine} 磁盘使用率 {disk}% > {th.get('disk_high_pct')}%")
    except (ValueError, TypeError):
        pass

    # Hermes gateway 非 running（部署了但挂了）
    agents = snapshot.get("agents", {})
    hermes = agents.get("hermes")
    if isinstance(hermes, dict) and hermes.get("installed"):
        gw = hermes.get("gateway_state", "unknown")
        if gw != "running":
            alerts.append(f"{machine} Hermes gateway 状态异常: {gw}")

    # 采集错误（连接器报错）
    for atype, astate in agents.items():
        if isinstance(astate, dict) and astate.get("error") and "empty" not in str(astate.get("error")):
            alerts.append(f"{machine} {atype} 采集错误: {astate['error']}")

    return alerts


def make_notify_cb():
    """构造事件订阅者：只对值得关注的事件输出（供 cron 捕获）。"""

    def on_event(ev):
        # 只处理 state_changed 事件，且必须有实际变更
        if ev.get("event") != "state_changed":
            return
        machine = ev.get("machine")
        snapshot = ev.get("extra", {}).get("snapshot") or {}
        alerts = evaluate_snapshot(machine, snapshot)
        if alerts:
            for msg in alerts:
                print(f"[AGENT-FLEET][ALERT] {msg}")
    return on_event


def setup_alert_notify(publisher=None):
    """注册告警通知订阅者（在 scan 前调用）。"""
    if publisher is None:
        from hub import events as evmod
        evmod.subscribe(make_notify_cb())
    else:
        publisher.subscribe(make_notify_cb())


# ---------- 直接推送通道（可选，预留） ----------
def send_telegram(text):
    """Optional Hermes telegram channel.

    Deferred-with-condition (HANDOFF §六): stdout→cron already covers
    non-noisy alerts. Do not wire send_message until a separate ops plan
    keeps bot tokens out of fleet config/logs.
    """
    print(f"[TG] {text}")


def send_weixin(text):
    """Optional Hermes weixin channel. Same deferral as send_telegram."""
    print(f"[WX] {text}")