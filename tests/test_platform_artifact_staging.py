from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.platform.artifacts import ArtifactError, ArtifactStore


def _staging(root: Path, artifact_id: str, suffix: str, *, mtime: float) -> Path:
    path = root / f".{artifact_id}.{suffix}"
    path.mkdir(parents=True)
    (path / "content").write_bytes(b"partial")
    os.utime(path, (mtime, mtime))
    return path


def test_cleanup_staging_removes_only_old_bounded_store_directories(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    old = _staging(root, "a" * 24, "abc", mtime=1.0)
    fresh = _staging(root, "b" * 24, "def", mtime=950.0)
    unrelated = root / ".not-a-store-entry"
    unrelated.mkdir()
    symlink_target = tmp_path / "outside"
    symlink_target.mkdir()
    symlink = root / f".{('c' * 24)}.ghi"
    symlink.symlink_to(symlink_target, target_is_directory=True)
    store = ArtifactStore(root, clock=lambda: 1000.0)

    assert store.cleanup_staging(max_age_s=100) == 1
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()
    assert symlink.is_symlink()
    assert symlink_target.exists()


def test_cleanup_staging_is_bounded_and_rejects_invalid_inputs(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    for index in range(130):
        _staging(root, f"{index:024d}", "tmp", mtime=float(index))
    store = ArtifactStore(root, clock=lambda: 10000.0)

    assert store.cleanup_staging() == 128
    assert len(list(root.glob(".*.tmp"))) == 2
    with pytest.raises(ArtifactError, match="invalid_staging_limit"):
        store.cleanup_staging(limit=129)
    with pytest.raises(ArtifactError, match="invalid_staging_age"):
        store.cleanup_staging(max_age_s=0)
    with pytest.raises(ArtifactError, match="invalid_staging_time"):
        store.cleanup_staging(now=float("nan"))
