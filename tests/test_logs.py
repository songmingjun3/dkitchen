"""Session-log reading: the compressed current format, plain logs, and graceful refusal.

Every fixture here writes its own log, so the tests never read a real harness session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

zstandard = pytest.importorskip("zstandard")

from dkitchen import logs, paths


def write_log(directory: Path, session_id: str, events: list[dict], *, version: int = 4,
              compress: bool = True) -> Path:
    folder = directory / session_id
    folder.mkdir(parents=True)
    payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
    if compress:
        target = folder / f"session.v{version}.jsonl.zstd"
        target.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
    else:
        target = folder / f"session.v{version}.jsonl"
        target.write_text(payload, encoding="utf-8")
    return target


def header(session_id: str, cwd: str = "C:\\work", created: int = 1_700_000_000_000) -> dict:
    return {"type": "session", "version": 4, "id": session_id, "createdAt": created,
            "cwd": cwd, "isSeeded": False}


def assistant(text: str, turn: int = 1) -> dict:
    return {"type": "assistant/message",
            "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                     "turn": turn}}


def test_read_folds_the_latest_reply_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    session_id = "11111111-1111-4111-8111-111111111111"
    path = write_log(tmp_path / "sessions" / "--work--", session_id,
                     [header(session_id), assistant("first"), {"type": "turn/end"}, assistant("second")])
    session = logs.read(path)
    assert session.id == session_id
    assert session.cwd == "C:\\work"
    assert session.reply == "second"
    assert session.turns == 1


def test_plain_logs_are_read_too(tmp_path):
    session_id = "22222222-2222-4222-8222-222222222222"
    path = write_log(tmp_path, session_id, [header(session_id), assistant("plain")], compress=False)
    assert logs.read(path).reply == "plain"


def test_a_torn_last_row_is_skipped(tmp_path):
    session_id = "33333333-3333-4333-8333-333333333333"
    path = write_log(tmp_path, session_id, [header(session_id), assistant("kept")])
    import zstandard

    raw = zstandard.ZstdDecompressor().stream_reader(path.open("rb")).read()
    broken = raw + b'{"type": "assistant/message", "data": '
    path.write_bytes(zstandard.ZstdCompressor().compress(broken))
    assert logs.read(path).reply == "kept"


def test_a_newer_format_is_refused_with_a_readable_message(tmp_path):
    session_id = "44444444-4444-4444-8444-444444444444"
    folder = tmp_path / session_id
    folder.mkdir()
    target = folder / "session.v9.jsonl"
    target.write_text(json.dumps({"type": "session", "version": 9, "cwd": tmp_path.as_posix()}) + "\n",
                      encoding="utf-8")
    with pytest.raises(logs.Unreadable) as error:
        logs.read(target)
    assert "format 9" in str(error.value)


def test_locate_finds_a_session_under_any_project_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    session_id = "55555555-5555-4555-8555-555555555555"
    write_log(tmp_path / "sessions" / "--one--", session_id, [header(session_id)])
    assert logs.locate(session_id) == tmp_path / "sessions" / "--one--" / session_id / "session.v4.jsonl.zstd"
    assert logs.locate("00000000-0000-4000-8000-000000000000") is None


def test_known_sessions_lists_every_persisted_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    for index, session_id in enumerate(["66666666-6666-4666-8666-666666666666",
                                        "77777777-7777-4777-8777-777777777777"]):
        write_log(tmp_path / "sessions" / "--one--", session_id,
                  [header(session_id, created=1_700_000_000_000 + index)], compress=False)
    found = logs.known_sessions()
    assert set(found) == {"66666666-6666-4666-8666-666666666666",
                          "77777777-7777-4777-8777-777777777777"}


def test_the_newest_version_wins_in_one_directory(tmp_path):
    session_id = "88888888-8888-4888-8888-888888888888"
    folder = tmp_path / session_id
    folder.mkdir()
    (folder / "session.v3.jsonl").write_text(json.dumps(header(session_id)) + "\n", encoding="utf-8")
    (folder / "session.v4.jsonl").write_text(json.dumps(header(session_id)) + "\n", encoding="utf-8")
    assert logs._newest_log(folder).name == "session.v4.jsonl"


def test_chinese_replies_survive_the_round_trip(tmp_path):
    session_id = "99999999-9999-4999-8999-999999999999"
    path = write_log(tmp_path, session_id, [header(session_id), assistant("已合并 PR #57。")])
    assert logs.read(path).reply == "已合并 PR #57。"
