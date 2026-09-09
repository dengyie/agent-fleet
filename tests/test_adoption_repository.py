"""Tests for the isolated Adoption domain and SQLite repository (Task 4).

The adoption repository owns agent adoption records ("纳管" records) in a
separate WAL SQLite store with its own schema version table and error boundary.
It never issues commands, never touches the legacy observation JSONL, the
``events.jsonl`` log, the session store, or the task SQLite.

Coverage:

- the adoption store is a physically separate SQLite file;
- status round-trips ``pending -> adopted -> revoked`` and ``list_active``
  only surfaces currently-adopted rows (revoked rows are retained);
- bounded errors only: ``str(exc) == code``, never paths / raw input / sqlite
  text;
- session ids must pass the shared ``is_valid_session_id`` boundary;
- status transitions are atomic and only the allowed chain is accepted;
- ``capture_quality`` must be a member of ``CAPTURE_QUALITIES``;
- upsert is idempotent on ``session_id`` (one row per session);
- schema versioning is recorded and ``init()`` is idempotent;
- ``FleetConfig`` gains additive adoption fields that default disabled and
  default to ``<root>/var/adoptions/meta.db`` when enabled.
"""

from pathlib import Path

import pytest

from hub.config import FleetConfig
from hub.domain.adoption import (
    ADOPTION_STATUSES,
    Adoption,
    AdoptionStatus,
    allowed_old_statuses,
    is_valid_adoption_status,
    normalize_adoption_spec,
)
from hub.infrastructure.adoption_repository import (
    SCHEMA_VERSION,
    AdoptionRepository,
    AdoptionRepositoryError,
)
from session_schema import CAPTURE_QUALITIES


def adoption_dict(session_id=None, **overrides):
    """A valid Adoption payload with a bounded, path-free opaque session id."""
    data = {
        "adoption_id": "adp_0001",
        "machine_id": "m1",
        "session_id": session_id or "adopt_a3f2b3",
        "pid": 4242,
        "pgid": 4242,
        "started_at": "2026-08-30T00:00:00Z",
        "exe_path": "/usr/local/bin/codex",
        "agent_family": "codex",
        "native_file_path": None,
        "status": "pending",
        "capture_quality": "best_effort",
        "actor": "test@local",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    data.update(overrides)
    return data


def ready_repo(tmp_path):
    """An initialized AdoptionRepository inside a fresh temp dir."""
    repo = AdoptionRepository(tmp_path / "adoptions.db")
    repo.init()
    return repo


# ---------------------------------------------------------------------------
# Step 1 fixtures from the brief
# ---------------------------------------------------------------------------


def test_adoption_store_is_separate(tmp_path):
    repo = AdoptionRepository(tmp_path / "adoptions.db")
    repo.init()
    assert (tmp_path / "adoptions.db").exists()
    assert repo.path != tmp_path / "state" / "events.jsonl"


def test_status_round_trip_and_active_filter(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    assert repo.get(row.session_id).status == "pending"
    repo.update_status(row.session_id, "adopted")
    assert len(repo.list_active("m1")) == 1
    repo.update_status(row.session_id, "revoked")
    assert repo.list_active("m1") == []


def test_path_shaped_id_is_bounded_error(tmp_path):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="../../etc/passwd"))
    assert exc.value.code == "invalid_adoption"
    assert str(exc.value) == "invalid_adoption"


# ---------------------------------------------------------------------------
# Domain contract
# ---------------------------------------------------------------------------


def test_adoption_status_enum_values():
    assert AdoptionStatus.PENDING.value == "pending"
    assert AdoptionStatus.ADOPTED.value == "adopted"
    assert AdoptionStatus.REVOKED.value == "revoked"


def test_domain_exports_validators_and_quality_set():
    assert is_valid_adoption_status("adopted")
    assert not is_valid_adoption_status("adopted_x")
    assert "best_effort" in CAPTURE_QUALITIES
    assert tuple(sorted(ADOPTION_STATUSES)) == (
        "adopted", "pending", "revoked")


def test_allowed_old_statuses_are_lifecycle_chain():
    assert allowed_old_statuses("pending") == ("pending",)
    assert allowed_old_statuses("adopted") == ("pending", "adopted")
    assert allowed_old_statuses("revoked") == ("adopted", "revoked")
    with pytest.raises(ValueError):
        allowed_old_statuses("nope")


def test_normalize_builds_bounded_clean_dict():
    clean = normalize_adoption_spec(adoption_dict())
    assert clean["session_id"] == "adopt_a3f2b3"
    assert clean["status"] == "pending"
    assert clean["capture_quality"] == "best_effort"
    assert "extra" not in clean


def test_normalize_accepts_frozen_adoption_object():
    obj = Adoption(**adoption_dict())
    clean = normalize_adoption_spec(obj)
    assert clean["session_id"] == obj.session_id
    assert clean["machine_id"] == "m1"


# ---------------------------------------------------------------------------
# session id boundaries
# ---------------------------------------------------------------------------

BAD_IDS = [
    "../../etc/passwd", "a/b", "a\\b", "x.key", "x.secret", "x_token",
    "x.db", "x.json", "secret_x", "x..y",
]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_upsert_refuses_path_shaped_session_ids(tmp_path, bad):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id=bad))
    assert exc.value.code == "invalid_adoption"


@pytest.mark.parametrize("bad", BAD_IDS)
def test_id_scoped_methods_refuse_path_shaped_ids(tmp_path, bad):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.get(bad)
    assert exc.value.code == "invalid_adoption"
    with pytest.raises(AdoptionRepositoryError):
        repo.update_status(bad, "revoked")
    with pytest.raises(AdoptionRepositoryError):
        repo.update_capture_quality(bad, "exact")


def test_invalid_spec_rejected_with_bounded_code(tmp_path):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id=123))
    assert exc.value.code == "invalid_adoption"
    assert str(exc.value) == "invalid_adoption"


# ---------------------------------------------------------------------------
# status lifecycle / atomic updates
# ---------------------------------------------------------------------------

TRANSITION_TABLE = [
    # (old, new, allowed?)
    ("pending", "pending", True),
    ("pending", "adopted", True),
    ("adopted", "adopted", True),
    ("adopted", "revoked", True),
    ("revoked", "revoked", True),
    ("pending", "revoked", False),   # must go through adopted first
    ("adopted", "pending", False),   # regression
    ("revoked", "adopted", False),   # revoked is terminal
    ("revoked", "pending", False),
]


@pytest.mark.parametrize("old,new,allowed", TRANSITION_TABLE)
def test_status_transition_table(tmp_path, old, new, allowed):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status=old))
    if allowed:
        repo.update_status(row.session_id, new)
        assert repo.get(row.session_id).status == new
    else:
        with pytest.raises(AdoptionRepositoryError) as exc:
            repo.update_status(row.session_id, new)
        assert exc.value.code == "invalid_status_transition"
        # atomic: nothing changed
        assert repo.get(row.session_id).status == old


def test_missing_row_update_is_bounded(tmp_path):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.update_status("adopt_missing", "adopted")
    assert exc.value.code == "invalid_adoption"


def test_address_unknown_new_status_rejected(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict())
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.update_status(row.session_id, "terminated")
    assert exc.value.code == "invalid_status_transition"
    # nothing changed
    assert repo.get(row.session_id).status == "pending"


def test_revoked_rows_are_retained(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    repo.update_status(row.session_id, "adopted")
    repo.update_status(row.session_id, "revoked")
    # retained for auditing, excluded from the active set
    assert repo.get(row.session_id).status == "revoked"
    assert repo.list_active("m1") == []
    assert row.session_id in {r.session_id for r in repo.list("m1")}


def test_concurrent_status_transitions_stay_consistent(tmp_path):
    import threading

    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        repo.update_status(row.session_id, "adopted")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert repo.get(row.session_id).status == "adopted"
    assert len(repo.list_active("m1")) == 1


# ---------------------------------------------------------------------------
# capture quality
# ---------------------------------------------------------------------------

BAD_QUALITIES = ["bad_quality", "", "EXACT", None, 42]


@pytest.mark.parametrize("quality", BAD_QUALITIES)
def test_upsert_rejects_invalid_capture_quality(tmp_path, quality):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(capture_quality=quality))
    assert exc.value.code == "invalid_adoption"


def test_update_capture_quality_round_trip(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(capture_quality="structured"))
    assert row.capture_quality == "structured"
    updated = repo.update_capture_quality(row.session_id, "exact")
    assert updated.capture_quality == "exact"
    assert repo.get(row.session_id).capture_quality == "exact"


@pytest.mark.parametrize("quality", ["bad", "", "not_exact"])
def test_update_capture_quality_rejects_unknown(tmp_path, quality):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict())
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.update_capture_quality(row.session_id, quality)
    assert exc.value.code == "invalid_capture_quality"
    assert repo.get(row.session_id).capture_quality == "best_effort"


# ---------------------------------------------------------------------------
# upsert idempotence
# ---------------------------------------------------------------------------


def test_upsert_is_idempotent_for_session(tmp_path):
    repo = ready_repo(tmp_path)
    first = repo.upsert(adoption_dict(session_id="adopt_dup_001", pid=100))
    second = repo.upsert(adoption_dict(session_id="adopt_dup_001", pid=200,
                                       status="adopted"))
    assert first.session_id == second.session_id
    assert repo.get("adopt_dup_001").pid == 200
    assert repo.get("adopt_dup_001").status == "adopted"
    assert len(repo.list("m1")) == 1


def test_upsert_preserves_distinct_sessions_on_same_machine(tmp_path):
    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict(session_id="adopt_a", adoption_id="adp_a"))
    # distinct process identity (different pid/started_at) → both rows coexist
    repo.upsert(adoption_dict(session_id="adopt_b", adoption_id="adp_b",
                                pid=777, started_at="2026-08-31T00:00:00Z"))
    assert len(repo.list("m1")) == 2


def test_upsert_accepts_domain_object(tmp_path):
    repo = ready_repo(tmp_path)
    adoption = Adoption(**adoption_dict(session_id="adopt_obj_01"))
    stored = repo.upsert(adoption)
    assert stored.session_id == "adopt_obj_01"
    assert repr(stored).startswith("Adoption(")


def test_missing_fields_rejected(tmp_path):
    repo = ready_repo(tmp_path)
    data = adoption_dict()
    del data["exe_path"]
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(data)
    assert exc.value.code == "invalid_adoption"


@pytest.mark.parametrize("bad_pid", [-1, 0, "4242", None, True])
def test_non_positive_pid_rejected(tmp_path, bad_pid):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(pid=bad_pid))
    assert exc.value.code == "invalid_adoption"


def test_pgid_must_be_positive_int_or_none(tmp_path):
    repo = ready_repo(tmp_path)
    ok = repo.upsert(adoption_dict(pgid=None))
    assert ok.pgid is None
    # distinct process identity so the active-row uniqueness does not conflict
    ok2 = repo.upsert(adoption_dict(session_id="adopt_pg2", pgid=999,
                                    adoption_id="adp_pg2", pid=999,
                                    started_at="2026-08-31T00:00:00Z"))
    assert ok2.pgid == 999
    with pytest.raises(AdoptionRepositoryError):
        repo.upsert(adoption_dict(session_id="adopt_pgbad", pgid=-4,
                                  adoption_id="adp_pgbad"))


# ---------------------------------------------------------------------------
# schema / versioning / separation
# ---------------------------------------------------------------------------


def test_init_records_schema_version(tmp_path):
    repo = ready_repo(tmp_path)
    assert SCHEMA_VERSION == 1
    assert repo.schema_version() == 1


def test_init_is_idempotent(tmp_path):
    repo = AdoptionRepository(tmp_path / "adoptions.db")
    repo.init()
    repo.init()
    repo.upsert(adoption_dict())
    repo.init()
    assert repo.schema_version() == 1


def test_repository_does_not_touch_session_store(tmp_path):
    import os

    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict())
    files = {os.path.basename(p) for p in os.listdir(tmp_path)}
    assert "adoptions.db" in files
    assert "sessions.db" not in files


# ---------------------------------------------------------------------------
# FleetConfig integration (additive)
# ---------------------------------------------------------------------------


def test_adoption_config_disabled_by_default():
    cfg = FleetConfig.from_root(Path("/tmp/fleet_root"))
    assert cfg.adoption_repositories_enabled is False
    assert cfg.adoption_db is None


def test_adoption_config_defaults_to_var_adoptions(tmp_path):
    cfg = FleetConfig.from_root(
        tmp_path, adoption_repositories_enabled=True)
    assert cfg.adoption_repositories_enabled is True
    assert cfg.adoption_db == tmp_path / "var" / "adoptions" / "meta.db"
    # it must live outside the legacy state tree and not collide with the
    # session store when both are enabled.
    assert cfg.adoption_db.parent != (tmp_path / "state")
    both = FleetConfig.from_root(
        tmp_path, adoption_repositories_enabled=True,
        session_repositories_enabled=True)
    assert both.adoption_db != both.session_db


def test_adoption_config_supports_explicit_override(tmp_path):
    db = tmp_path / "custom" / "meta.db"
    cfg = FleetConfig.from_root(
        tmp_path, adoption_repositories_enabled=True, adoption_db=db)
    assert cfg.adoption_db == db


def test_error_str_is_bounded_code():
    err = AdoptionRepositoryError("bounded_code_x")
    assert str(err) == "bounded_code_x"
    assert err.code == "bounded_code_x"


# ---------------------------------------------------------------------------
# CAS revoke — adopted -> revoked exactly once
# ---------------------------------------------------------------------------


def test_revoke_cas_captures_adopted_transition(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    repo.update_status(row.session_id, "adopted")
    revoked, captured = repo.revoke_cas(row.session_id)
    assert captured is True
    assert revoked.status == "revoked"
    again, captured_again = repo.revoke_cas(row.session_id)
    assert captured_again is False
    assert again.status == "revoked"


def test_revoke_cas_pending_is_blocked(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.revoke_cas(row.session_id)
    assert exc.value.code == "invalid_status_transition"
    assert repo.get(row.session_id).status == "pending"


def test_revoke_cas_missing_is_bounded(tmp_path):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.revoke_cas("adopt_missing")
    assert exc.value.code == "invalid_adoption"


def test_delete_pending_removes_only_pending_row(tmp_path):
    repo = ready_repo(tmp_path)
    pending = repo.upsert(adoption_dict(status="pending"))
    assert repo.delete_pending(pending.session_id) is True
    assert repo.get(pending.session_id) is None
    adopted = repo.upsert(adoption_dict(
        session_id="adopt_keep", adoption_id="adp_keep",
        pid=777, started_at="2026-08-31T00:00:00Z", status="pending"))
    repo.update_status(adopted.session_id, "adopted")
    assert repo.delete_pending(adopted.session_id) is False
    assert repo.get(adopted.session_id).status == "adopted"


def test_concurrent_revoke_cas_exactly_one_winner(tmp_path):
    import threading

    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    repo.update_status(row.session_id, "adopted")
    barrier = threading.Barrier(2)
    captured = []

    def worker():
        barrier.wait()
        _revoked, won = repo.revoke_cas(row.session_id)
        captured.append(won)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert captured.count(True) == 1
    assert captured.count(False) == 1
    assert repo.get(row.session_id).status == "revoked"


# ---------------------------------------------------------------------------
# Review gate regressions (coordinator follow-up on 95296f7)
# ---------------------------------------------------------------------------


def test_init_sqlite_failure_is_bounded(tmp_path):
    # A path that is an existing directory makes sqlite3.connect fail; init()
    # must surface that as a bounded code, never a raw sqlite3.Error.
    (tmp_path / "not_a_file").mkdir()
    repo = AdoptionRepository(tmp_path / "not_a_file")
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.init()
    assert exc.value.code == "adoption_store"
    assert str(exc.value) == "adoption_store"


def test_schema_version_sqlite_failure_is_bounded(tmp_path):
    (tmp_path / "not_a_file").mkdir()
    repo = AdoptionRepository(tmp_path / "not_a_file")
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.schema_version()
    assert exc.value.code == "adoption_store"
    assert str(exc.value) == "adoption_store"


def test_upsert_follows_status_chain_in_steps(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(session_id="adopt_chain1"))
    assert row.status == "pending"
    repo.upsert(adoption_dict(session_id="adopt_chain1", status="adopted"))
    assert repo.get("adopt_chain1").status == "adopted"
    repo.upsert(adoption_dict(session_id="adopt_chain1", status="revoked"))
    assert repo.get("adopt_chain1").status == "revoked"


def test_upsert_rejects_skipping_pending_to_revoked(tmp_path):
    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict(session_id="adopt_skip", pid=100))
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="adopt_skip", pid=200,
                                  status="revoked"))
    assert exc.value.code == "invalid_status_transition"
    # atomic: neither status nor any other field changed
    stored = repo.get("adopt_skip")
    assert stored.status == "pending"
    assert stored.pid == 100


def test_upsert_rejects_status_regression_on_existing_row(tmp_path):
    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict(session_id="adopt_regr", status="adopted"))
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="adopt_regr", status="pending"))
    assert exc.value.code == "invalid_status_transition"
    assert repo.get("adopt_regr").status == "adopted"


def test_upsert_rejects_post_revoke_resurrection(tmp_path):
    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict(session_id="adopt_term"))
    repo.update_status("adopt_term", "adopted")
    repo.update_status("adopt_term", "revoked")
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="adopt_term", status="adopted"))
    assert exc.value.code == "invalid_status_transition"
    assert repo.get("adopt_term").status == "revoked"
    assert repo.list_active("m1") == []


def test_upsert_idempotent_same_status_still_allowed(tmp_path):
    repo = ready_repo(tmp_path)
    repo.upsert(adoption_dict(session_id="adopt_same", status="adopted"))
    repo.upsert(adoption_dict(session_id="adopt_same", status="adopted",
                              pid=77))
    stored = repo.get("adopt_same")
    assert stored.status == "adopted"
    assert stored.pid == 77


# ---------------------------------------------------------------------------
# Fix I2 — uniqueness of an ACTIVE row per (machine_id, pid, started_at)
# ---------------------------------------------------------------------------

_ADOPTION_COLUMNS = (
    "adoption_id", "machine_id", "session_id", "pid", "pgid", "started_at",
    "exe_path", "agent_family", "native_file_path", "status",
    "capture_quality", "actor", "created_at", "updated_at",
)


def _raw_insert_adoption(conn, record):
    """Insert a legacy-format Adoption row directly, bypassing the repo API.

    Used to seed a store that predates the active-identity uniqueness so
    :meth:`AdoptionRepository.init` can be shown deduping it.
    """
    conn.execute(
        "INSERT INTO adoptions (adoption_id, machine_id, session_id,"
        " pid, pgid, started_at, exe_path, agent_family,"
        " native_file_path, status, capture_quality, actor, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(record[k] for k in _ADOPTION_COLUMNS))


def test_second_active_row_for_same_identity_conflicts_and_keeps_one(tmp_path):
    """Inserting the same identity (different session ids) keeps ONE row; the
    second insert conflicts (I2 repo-level regression)."""
    repo = ready_repo(tmp_path)
    first = repo.upsert(adoption_dict(session_id="adopt_ident_a",
                                      adoption_id="adp_ident_a"))
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="adopt_ident_b",
                                  adoption_id="adp_ident_b"))
    assert exc.value.code == "invalid_adoption"
    rows = repo.list("m1")
    assert len(rows) == 1
    assert rows[0].session_id == first.session_id == "adopt_ident_a"


def test_revoked_row_does_not_block_a_fresh_adopt(tmp_path):
    """The unique index is PARTIAL over active rows: a pre-existing revoked
    row for the same identity must not block a fresh pending adopt."""
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(session_id="adopt_old", adoption_id="adp_old"))
    repo.update_status("adopt_old", "adopted")
    repo.update_status("adopt_old", "revoked")
    fresh = repo.upsert(adoption_dict(session_id="adopt_fresh",
                                      adoption_id="adp_fresh"))
    assert fresh.status == "pending"
    assert fresh.session_id == "adopt_fresh"
    assert len(repo.list("m1")) == 2  # revoked retained + fresh active


def test_int_disambiguation_pre_existing_active_duplicates(tmp_path):
    """init() must dedupe pre-existing duplicate active rows (keeping the
    EARLIEST) so the new unique partial index can be created."""
    import sqlite3

    db = tmp_path / "adoptions.db"
    repo = AdoptionRepository(db)
    repo.init()
    conn = sqlite3.connect(db)
    try:
        # a store created before the uniqueness invariant existed
        conn.execute("DROP INDEX IF EXISTS uq_adoption_active_identity")
        _raw_insert_adoption(conn, adoption_dict(
            session_id="adopt_early", adoption_id="adp_early",
            created_at="2026-01-01T00:00:00Z"))
        _raw_insert_adoption(conn, adoption_dict(
            session_id="adopt_late", adoption_id="adp_late",
            created_at="2026-01-02T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    repo.init()   # dedupe BEFORE the unique index is (re)created
    rows = repo.list("m1")
    assert len(rows) == 1
    assert rows[0].session_id == "adopt_early"   # EARLIEST kept
    # and the unique partial index now rejects a second active row
    with pytest.raises(AdoptionRepositoryError):
        repo.upsert(adoption_dict(session_id="adopt_new", pid=4242))
    assert len(repo.list("m1")) == 1