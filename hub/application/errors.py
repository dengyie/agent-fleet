"""Canonical bounded application error (leaf module, no hub imports).

Kept separate from ``task_service`` so low-level modules such as
``hub.platform.tool_broker`` can raise it without importing the task service
(which would close an import cycle through ``run_worker_service``).
"""


class ApplicationError(Exception):
    """Stable, bounded application error.

    ``code`` is the public error key; ``detail`` is a user-visible bounded
    description; ``status`` is the HTTP status mapped by the adapter layer.
    """

    def __init__(self, code: str, detail: str = "", status: int = 400) -> None:
        self.code = code
        self.detail = str(detail)[:200]
        self.status = int(status)
        super().__init__(self.detail)
