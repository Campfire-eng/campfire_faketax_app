import time
from pathlib import Path

from store import Store


def test_claim_jti_rejects_replay(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    exp = time.time() + 300

    assert store.claim_jti("abc", exp)
    assert not store.claim_jti("abc", exp)


def test_frame_session_remembers_the_workspace_and_records(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    store.open_frame_session(jti="abc", customer_id=1, exp=time.time() + 300, object_ids=["11"])

    opened = store.frame_session(jti="abc")

    assert opened is not None and opened.customer_id == 1 and opened.object_ids == ["11"]
    assert store.frame_session(jti="other") is None


def test_expired_frame_session_is_closed(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    store.open_frame_session(jti="abc", customer_id=1, exp=time.time() - 1, object_ids=[])

    assert store.frame_session(jti="abc") is None


def test_an_older_database_gains_the_object_ids_column(tmp_path: Path) -> None:
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE frame_sessions (jti TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, exp REAL NOT NULL)")
    store = Store(path=path)
    store.open_frame_session(jti="abc", customer_id=1, exp=time.time() + 300, object_ids=["11"])

    opened = store.frame_session(jti="abc")

    assert opened is not None and opened.object_ids == ["11"]
