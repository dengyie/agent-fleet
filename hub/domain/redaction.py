"""Bounded public redaction metadata for the Hub side (Task 3).

Only a fixed allowlist of non-secret fields ever leaves this module:

- ``replaced``   : integer count of redacted values;
- ``categories`` : sorted, deduplicated, capped tuple of category names;
- ``uncertain``  : boolean - True when a value could not be safely classified.

No original text, secret, path, key value, or replacement detail ever enters
the public metadata.  Serialization is deterministic: categories are sorted
and deduplicated and capped at ``MAX_PUBLIC_CATEGORIES`` (32).
"""

from __future__ import annotations

from typing import Any

from tools.session.redact import RedactionReport

MAX_PUBLIC_CATEGORIES = 32

_PUBLIC_FIELDS = frozenset({"replaced", "categories", "uncertain"})


def public_redaction_metadata(report: RedactionReport) -> dict[str, Any]:
    """Return the bounded public field allowlist for a redaction report.

    ``categories`` are sorted, deduplicated and capped at
    ``MAX_PUBLIC_CATEGORIES`` so the output is deterministic and bounded even
    when a pathological report carries many rule names.  Unknown/unwanted
    report fields (details, original text, key material) are never surfaced.
    """
    categories = tuple(sorted(set(report.categories)))[
        :MAX_PUBLIC_CATEGORIES
    ]
    return {
        "replaced": int(report.replaced),
        "categories": categories,
        "uncertain": bool(report.uncertain),
    }


__all__ = [
    "MAX_PUBLIC_CATEGORIES",
    "public_redaction_metadata",
]