"""Agent-side pure-local discovery (push-only probe).

``tools.probe.discovery`` enumerates running agent processes on the local
machine and renders them as stable ``Instance`` metadata rows.  It performs
no network I/O, writes no state, and never signals or controls a process.
"""