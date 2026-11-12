"""Independent command peer using the production provider against a strict HTTP fixture."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main(arguments: list[str]) -> int:
    repository, endpoint = arguments
    sys.path.insert(0, repository)
    from hub.platform.providers.openai_compatible import OpenAICompatibleProvider

    assert "FLEET_TEST_SECRET" not in os.environ
    request = json.loads(Path(os.environ["FLEET_REVIEW_REQUEST"]).read_bytes())
    source = Path(os.environ["FLEET_REVIEW_SOURCE"])
    output = (source / "generated.txt").read_bytes()
    payload = {"request": request, "source_sha256": hashlib.sha256(output).hexdigest(),
        "source_text": output.decode("utf-8"), "test_present": (source / "test_generated.py").is_file()}
    provider = OpenAICompatibleProvider(model="fixture-review-model", endpoint=endpoint, api_key="fixture-review-key",
        allow_network=True, timeout_s=5, max_retries=0)
    response = provider.complete([{"role": "system", "content": "Review this fixed source and return the registered JSON report."},
        {"role": "user", "content": json.dumps(payload, separators=(",", ":"))}], [])
    assert response.kind == "final" and response.finish_reason == "stop"
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
