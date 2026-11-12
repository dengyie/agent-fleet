"""Shared candidate filter for Run and owner selection.

Final capacity admission remains transactional in claim_worker_slot. This
filter avoids repeatedly claiming an older Run whose workspace is occupied.
Interrupted executions must still be selected for recovery regardless of
workspace occupancy.
"""
from __future__ import annotations


def workspace_capacity_filter(limit: int | None, now: float) -> tuple[str, tuple]:
    if limit is None:
        return "", ()
    limit = max(1, min(64, int(limit)))
    return (
        " AND (r.state!='queued' OR (SELECT COUNT(*) FROM platform_worker_slots s "
        "WHERE s.workspace_key=r.owner_id || ':' || COALESCE("
        "NULLIF(json_extract(r.config_snapshot,'$.workspace_id'),''),"
        "(SELECT c.workspace_id FROM conversations c "
        "WHERE c.owner_id=r.owner_id AND c.conversation_id=r.conversation_id)) "
        "AND s.expires_at>?) + (SELECT COUNT(*) FROM runs w WHERE w.run_id!=r.run_id "
        "AND w.runtime_wait_command_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM platform_worker_slots ws WHERE ws.run_id=w.run_id AND ws.expires_at>?) "
        "AND w.owner_id=r.owner_id AND COALESCE(NULLIF(json_extract(w.config_snapshot,'$.workspace_id'),''),"
        "(SELECT wc.workspace_id FROM conversations wc WHERE wc.conversation_id=w.conversation_id))="
        "COALESCE(NULLIF(json_extract(r.config_snapshot,'$.workspace_id'),''),"
        "(SELECT rc.workspace_id FROM conversations rc WHERE rc.conversation_id=r.conversation_id))) < ?)",
        (float(now), float(now), limit),
    )
