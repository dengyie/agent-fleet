"""Owner-scoped, finite-lived streaming of durable public Run snapshots."""
from __future__ import annotations

import json
import time

from flask import Response, stream_with_context

from hub.domain.run import TERMINAL_RUN_STATES
from hub.http.errors import ApplicationError
from hub.http.observe_routes import _http_state


def stream_run(owner_id, run_id, runs, events, *, after=0, lifetime_s=25.0):
    # Validate owner and cursor before sending successful streaming headers.
    if type(after) is not int or not 0 <= after <= 2 ** 63 - 1:
        raise ApplicationError('invalid_cursor', '游标不合法', 400)
    initial = runs.get(owner_id, run_id)
    initial_page = events.list(owner_id, run_id, after=after, limit=200)
    slots = _http_state()['sse']  # Share the existing app-wide HTTP stream budget.
    if not slots.try_acquire():
        raise ApplicationError('stream_capacity', '实时连接繁忙，正在使用状态轮询', 503)
    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            slots.release()

    def frame(run, cursor):
        return 'id: ' + str(cursor) + '\nevent: run\ndata: ' + json.dumps(
            run, ensure_ascii=False, separators=(',', ':')) + '\n\n'

    @stream_with_context
    def generate():
        cursor = after
        page = initial_page
        deadline = time.monotonic() + max(.01, min(25.0, lifetime_s))
        last_check = time.monotonic()
        try:
            # A full projection heals reloads and cursors pruned by retention.
            # It never contains private checkpoints, raw deltas or tool arguments.
            yield frame(initial, cursor)
            if initial.get('state') in TERMINAL_RUN_STATES:
                return
            while time.monotonic() < deadline:
                rows = page.get('events') or []
                if rows:
                    cursor = page.get('next_cursor') or rows[-1]['sequence']
                now = time.monotonic()
                if rows or now - last_check >= 1.0:
                    run = runs.get(owner_id, run_id)
                    yield frame(run, cursor)
                    last_check = now
                    if run.get('state') in TERMINAL_RUN_STATES:
                        return
                if len(rows) < 200:
                    time.sleep(.15)
                page = events.list(owner_id, run_id, after=cursor, limit=200)
        finally:
            release()

    response = Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
    })
    response.call_on_close(release)
    return response
