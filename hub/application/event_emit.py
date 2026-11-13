"""统一的"提交后发事件"助手。

事件发布永远是写用例提交之后的副作用：存储已经落盘，发布失败（订阅者
异常、事件库故障、未接线的 publisher）绝不能把已成功的写操作变成 5xx，
也不能被静默吞掉。``safe_emit`` 记录日志后返回 ``None``，从不抛出。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def safe_emit(publisher, event_type: str, **kwargs):
    """调用 ``publisher.emit(event_type, **kwargs)``；失败只记日志，从不抛出。

    返回 emit 的结果（事件 dict），失败或 publisher 缺失时返回 ``None``。
    日志只含事件类型与异常类名，不含快照体/凭据等负载。
    """
    if publisher is None:
        return None
    try:
        return publisher.emit(event_type, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 提交后副作用，绝不回滚调用方
        logger.warning("event emit failed after commit: event=%s error=%s",
                       event_type, type(exc).__name__)
        return None
