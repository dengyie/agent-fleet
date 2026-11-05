"""Review regressions: torn/undecodable JSONL tails must not reset event_seq."""

import json
import threading
from pathlib import Path

import pytest

from hub.infrastructure.event_repository import JsonlEventRepository
from hub.infrastructure.jsonl import repair_jsonl_tail


def _row(seq: int, text: str = "ok") -> bytes:
    return (json.dumps({"event": "e", "event_seq": seq, "msg": text},
                       ensure_ascii=False) + "\n").encode("utf-8")


@pytest.mark.parametrize("tail", [
    # crash inside a multibyte UTF-8 sequence ("中" = e4 b8 ad)
    '{"event": "e", "event_seq": 3, "msg": "中'.encode("utf-8")[:-1],
    # crash in the middle of an ASCII row
    b'{"event": "e", "event_s',
])
def test_torn_tail_is_repaired_before_append_and_sequence_continues(tmp_path, tail):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_row(1, "你好") + _row(2) + tail)
    repo = JsonlEventRepository(path)
    # 读路径不因末尾残行丢失整个日志
    assert [e["event_seq"] for e in repo.read_recent(10)] == [1, 2]
    event = {"event": "after", "ts": 1.0}
    assert repo.append(event) == 3
    raw = path.read_bytes()
    assert raw.startswith(_row(1, "你好") + _row(2) + b'{"event": "after"')
    lines = raw.decode("utf-8").splitlines()
    assert [json.loads(line)["event_seq"] for line in lines] == [1, 2, 3]
    assert [e["event_seq"] for e in repo.read_since_sequence(1)] == [2, 3]
    assert repo.malformed_lines == 0


def test_complete_record_missing_newline_is_terminated_not_dropped(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_row(1) + _row(2)[:-1])
    assert repair_jsonl_tail(path) is True
    assert path.read_bytes() == _row(1) + _row(2)
    assert repair_jsonl_tail(path) is False
    assert repair_jsonl_tail(tmp_path / "missing.jsonl") is False


def test_undecodable_middle_line_is_skipped_and_counted(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_row(1) + b"\xff\xfe garbage\n" + b"not json\n" + _row(2))
    repo = JsonlEventRepository(path)
    assert [e["event_seq"] for e in repo.read_recent(10)] == [1, 2]
    assert repo.malformed_lines == 2
    event = {"event": "next"}
    assert repo.append(event) == 3


def test_rotation_survives_undecodable_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(_row(1) + b"\xff\n" + _row(2))
    repo = JsonlEventRepository(path, max_bytes=10, keep_lines=2)
    repo.append({"event": "x"})
    rows = [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]
    assert [r["event_seq"] for r in rows] == [2, 3]


def test_reads_are_incremental_and_do_not_take_the_append_lock(tmp_path):
    path = tmp_path / "events.jsonl"
    repo = JsonlEventRepository(path)
    for i in range(5):
        repo.append({"event": f"e{i}", "ts": float(i)})
    assert len(repo.read_since(-1, 100)) == 5
    offset = repo._cache_offset
    assert offset == path.stat().st_size
    # 复制返回值：调用方修改不污染缓存
    repo.read_recent(1)[0]["event"] = "mutated"
    assert repo.read_recent(1)[0]["event"] == "e4"
    done = threading.Event()
    with repo._lock:  # 模拟一个长时间持有追加锁的写者
        t = threading.Thread(target=lambda: (repo.read_since_sequence(3), done.set()))
        t.start()
        assert done.wait(2.0), "read_since_sequence blocked on the append lock"
        t.join()
    repo.append({"event": "e5", "ts": 5.0})
    assert [e["event"] for e in repo.read_since_sequence(4)] == ["e4", "e5"]
    assert repo._cache_offset == path.stat().st_size > offset


def test_partial_row_from_concurrent_writer_is_not_consumed_early(tmp_path):
    path = tmp_path / "events.jsonl"
    repo = JsonlEventRepository(path)
    repo.append({"event": "one"})
    with open(path, "ab") as handle:
        handle.write(b'{"event": "two", "event_seq": 2')
    assert [e["event"] for e in repo.read_recent(10)] == ["one"]
    with open(path, "ab") as handle:
        handle.write(b"}\n")
    assert [e["event"] for e in repo.read_recent(10)] == ["one", "two"]
    assert repo.malformed_lines == 0
