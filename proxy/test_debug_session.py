from datetime import datetime, timedelta, timezone

from .tunnel import DebugSessionStore


def test_debug_session_persists_and_revokes(tmp_path):
    store = DebugSessionStore(str(tmp_path))
    session = store.create("alpha", "container-a", 60)
    recovered = DebugSessionStore(str(tmp_path))
    recovered.recover()
    assert recovered.get(session.id).project == "alpha"
    assert recovered.revoke(session.id).container_id == "container-a"
    assert recovered.get(session.id) is None


def test_expired_debug_session_is_not_active(tmp_path):
    store = DebugSessionStore(str(tmp_path))
    session = store.create("alpha", "container-a", 60)
    session.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store._save(session)
    assert store.get(session.id) is None
