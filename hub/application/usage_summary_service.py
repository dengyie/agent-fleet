"""Bounded, owner-scoped usage aggregation; monetary values stay Decimal strings."""
from __future__ import annotations

import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any, Callable, Generator, Iterator, Mapping, Protocol

from hub.application.task_service import ApplicationError
from hub.infrastructure.usage_repository import UsageRepositoryError
from platform_schema import validate_owner_id

DIMENSIONS = ("day", "project", "machine", "agent", "model")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
MAX_BUCKETS = 1000
MAX_SAFE_INTEGER = 9_007_199_254_740_991


class UsageSummaryReader(Protocol):
    def iter_summary_events(self, owner_id: str, *, from_time: float, to_time: float,
                            filters: Mapping[str, str]) -> Generator[dict[str, Any], None, None]: ...


def _invalid(code: str = "invalid_usage_query") -> ApplicationError:
    return ApplicationError(code, "用量查询参数不合法；日期范围最多 90 天", 400)


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid() from exc
    if len(value) == 10:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        raise _invalid()
    return parsed.astimezone(timezone.utc)


def _counter(value: int) -> int | str:
    return value if value <= MAX_SAFE_INTEGER else str(value)


class UsageSummaryService:
    def __init__(self, repository: UsageSummaryReader, *, clock: Callable[[], float] = time.time):
        self.repository = repository
        self.clock = clock

    def summary(self, owner_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        if set(params) - {"from", "to", "group_by", *DIMENSIONS[1:]}:
            raise _invalid()
        group_by = params.get("group_by", "day")
        if not isinstance(group_by, str) or len(group_by) > 40:
            raise _invalid()
        groups = group_by.split(",")
        if not groups or len(groups) != len(set(groups)) or any(key not in DIMENSIONS for key in groups):
            raise _invalid()
        today = datetime.fromtimestamp(self.clock(), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = _utc(params["from"]) if params.get("from") else today - timedelta(days=6)
        end = _utc(params["to"]) if params.get("to") else today + timedelta(days=1)
        if not timedelta(0) < end - start <= timedelta(days=90):
            raise _invalid("invalid_usage_range")
        filters: dict[str, str] = {}
        for key in DIMENSIONS[1:]:
            value = params.get(key)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or len(value) > (160 if key == "model" else 128) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise _invalid()
            filters[key] = value
        try:
            with closing(self.repository.iter_summary_events(owner_id, from_time=start.timestamp(), to_time=end.timestamp(), filters=filters)) as rows:
                buckets = self._aggregate(rows, groups)
        except UsageRepositoryError as exc:
            raise ApplicationError("usage_unavailable", "用量数据暂时不可用", 503) from exc
        items = []
        for key in sorted(buckets, key=lambda values: tuple("" if item is None else item for item in values)):
            item = buckets[key]
            amount = item["estimated_cost"]
            item["estimated_cost"] = format(amount, "f") if amount is not None else None
            for field in ("requests", "unknown_count", *TOKEN_FIELDS):
                if item[field] is not None:
                    item[field] = _counter(item[field])
            items.append(item)
        return {"ok": True, "from": start.isoformat().replace("+00:00", "Z"),
                "to": end.isoformat().replace("+00:00", "Z"), "group_by": groups,
                "currency": "USD", "items": items}

    @classmethod
    def _aggregate(cls, rows: Iterator[dict[str, Any]], groups: list[str]) -> dict[tuple[Any, ...], dict[str, Any]]:
        buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
        with localcontext() as context:
            context.prec = 60
            for row in rows:
                row["day"] = datetime.fromtimestamp(row["occurred_at"], timezone.utc).date().isoformat()
                key = tuple(row[dimension] for dimension in groups)
                if key not in buckets and len(buckets) >= MAX_BUCKETS:
                    raise ApplicationError("usage_bucket_limit", "结果超过 1000 组，请缩小日期范围或减少分组维度", 400)
                if key not in buckets:
                    buckets[key] = {**dict(zip(groups, key)), "requests": 0, **dict.fromkeys(TOKEN_FIELDS, 0),
                                    "estimated_cost": Decimal(0), "cost_source": "provider", "unknown_count": 0}
                cls._add(buckets[key], row)
        return buckets

    @staticmethod
    def _add(bucket: dict[str, Any], row: Mapping[str, Any]) -> None:
        bucket["requests"] += 1
        complete = row["summary_complete"] == 1
        amount = row["estimated_cost"]
        source = row["cost_source"]
        known_cost = amount is not None and source in {"provider", "configured"}
        bucket["unknown_count"] += int(not complete or not known_cost)
        for field in TOKEN_FIELDS:
            bucket[field] = bucket[field] + row[field] if complete and bucket[field] is not None else None
        if not known_cost or bucket["estimated_cost"] is None:
            bucket["estimated_cost"], bucket["cost_source"] = None, "unknown"
            return
        bucket["estimated_cost"] += Decimal(amount)
        if source == "configured":
            bucket["cost_source"] = "configured"
