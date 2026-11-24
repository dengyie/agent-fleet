"""Drift sentinel: the Windows artifact modules must stay byte-identical.

``hub/platform/artifact_windows.py`` and ``tools/platform/artifact_windows.py``
are mirrored copies of the same NT-security implementation. They drift
silently unless a test forces them back together. Only the documented,
deliberate differences are whitelisted below; everything else must match
line for line.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "hub/platform/artifact_windows.py"
TOOLS = ROOT / "tools/platform/artifact_windows.py"

# The hub copy resolves the error type from its own package; the tools copy
# ships standalone and imports from fleet_core.
_IMPORT = re.compile(r"from (hub\.domain|fleet_core)\.artifact_receive import")

# The hub copy opens one extra pinned private child below an unchanged root;
# the tools copy instead creates every missing root component (mkdir -p).
_HUB_PRIVATE_CHILD_BLOCK = (
    "            if private_child is not None:\n"
    "                # Keep application ACLs intact; slots use a private pinned child.\n"
    "                handle = native.child(handle, private_child, access=0x87, directory=True, create=True)\n"
    "                stack.callback(native.close, handle)\n"
)

_DOCSTRING = re.compile(r'"""Pin every path component.*?"""', re.S)
_SIGNATURE = re.compile(r"root_handle\(root: Path, \*, [^)]+\)")


def _normalize(text: str) -> str:
    text = _IMPORT.sub("from <artifact_receive_module> import", text)
    text = text.replace(_HUB_PRIVATE_CHILD_BLOCK, "")
    text = _DOCSTRING.sub("<root_handle_docstring>", text)
    text = _SIGNATURE.sub("root_handle(root: Path, *, <parameter>)", text)
    text = text.replace(", directory=True, create=create_private", ", directory=True")
    return text


def test_windows_artifact_module_copies_stay_identical() -> None:
    hub = _normalize(HUB.read_text(encoding="utf-8"))
    tools = _normalize(TOOLS.read_text(encoding="utf-8"))
    assert hub == tools
