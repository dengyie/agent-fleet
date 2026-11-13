"""Measure one PNG parser invocation without unrelated parent allocations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tracemalloc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(path: Path) -> None:
    from tools.testing.png_evidence import verify_png

    tracemalloc.start()
    try:
        with path.open("rb") as source:
            verify_png(source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result = {"pid": os.getpid(), "peak_bytes": peak, "limit_bytes": 2 * 1024 * 1024}
    print(json.dumps(result), flush=True)
    assert peak < result["limit_bytes"], result


if __name__ == "__main__":
    main(Path(sys.argv[1]))
