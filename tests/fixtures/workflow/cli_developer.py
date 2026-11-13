"""Offline CLI test peer; an exclusive trace detects unintended execution replay."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main(arguments: list[str]) -> int:
    trace_path, *registration, instruction = arguments
    adapter_id, = registration or ["fixture-cli"]
    contract = json.loads(instruction)
    assert contract["step_key"] == "develop" and contract["adapter_ref"] == {"id": adapter_id, "revision": 1}
    Path("generated.txt").write_text("HTTP workflow output\n", encoding="utf-8")
    Path("test_generated.py").write_text(
        "from pathlib import Path\ndef test_generated():\n    assert Path('generated.txt').read_text() == 'HTTP workflow output\\n'\n",
        encoding="utf-8",
    )
    with Path(trace_path).open("x", encoding="utf-8") as trace:
        json.dump({"instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "owner_id": contract["owner_id"], "workspace_id": contract["workspace_id"],
            "generated_sha256": hashlib.sha256(Path("generated.txt").read_bytes()).hexdigest()}, trace)
    print("fixture CLI completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
