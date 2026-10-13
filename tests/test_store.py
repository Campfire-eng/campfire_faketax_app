import time
from pathlib import Path

from store import Store


def test_claim_jti_rejects_replay(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    exp = time.time() + 300

    assert store.claim_jti("abc", exp)
    assert not store.claim_jti("abc", exp)


def test_frame_session_is_bound_to_customer(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    store.open_frame_session(jti="abc", customer_id=1, exp=time.time() + 300)

    assert store.frame_session_open(jti="abc", customer_id=1)
    assert not store.frame_session_open(jti="abc", customer_id=2)


def test_expired_frame_session_is_closed(tmp_path: Path) -> None:
    store = Store(path=str(tmp_path / "t.sqlite3"))
    store.open_frame_session(jti="abc", customer_id=1, exp=time.time() - 1)

    assert not store.frame_session_open(jti="abc", customer_id=1)
