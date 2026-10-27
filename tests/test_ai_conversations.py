"""Tests for the device-side AI conversation store (review finding S-24)."""

import pytest

from nomothetic.ai_conversations import (
    AiConversationStore,
    ConversationBusyError,
    ConversationNotFoundError,
    trim_history,
)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_trim_history_drops_oldest_pairs_keeping_alternation():
    messages = []
    for i in range(13):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    trimmed = trim_history(messages)
    assert len(trimmed) == 20
    assert trimmed[0] == {"role": "user", "content": "u3"}
    assert [m["role"] for m in trimmed[:2]] == ["user", "assistant"]


def test_begin_new_conversation_issues_id_and_single_turn_history():
    store = AiConversationStore()
    cid, history = store.begin(None, "hello")
    assert cid
    assert history == [{"role": "user", "content": "hello"}]
    assert len(store) == 1


def test_ids_are_unique_and_url_safe():
    store = AiConversationStore(max_conversations=50)
    ids = set()
    for _ in range(20):
        cid, _ = store.begin(None, "hi")
        store.commit(cid, "hi", "ok")
        ids.add(cid)
    assert len(ids) == 20
    assert all(c.replace("-", "").replace("_", "").isalnum() for c in ids)


def test_commit_then_continue_carries_server_side_history():
    store = AiConversationStore()
    cid, _ = store.begin(None, "hello")
    store.commit(cid, "hello", "hi there")
    same, history = store.begin(cid, "drive forward")
    assert same == cid
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "drive forward"},
    ]


def test_unknown_conversation_raises_not_found():
    store = AiConversationStore()
    with pytest.raises(ConversationNotFoundError):
        store.begin("not-a-real-id", "hi")


def test_concurrent_command_in_same_conversation_is_busy():
    store = AiConversationStore()
    cid, _ = store.begin(None, "one")
    store.commit(cid, "one", "ok")
    store.begin(cid, "two")
    with pytest.raises(ConversationBusyError):
        store.begin(cid, "three")


def test_abort_records_nothing_and_releases():
    store = AiConversationStore()
    cid, _ = store.begin(None, "one")
    store.commit(cid, "one", "ok")
    store.begin(cid, "two")
    store.abort(cid)
    _, history = store.begin(cid, "three")
    assert [m["content"] for m in history] == ["one", "ok", "three"]


def test_abort_of_failed_first_command_drops_the_conversation():
    store = AiConversationStore()
    cid, _ = store.begin(None, "one")
    store.abort(cid)
    assert len(store) == 0
    with pytest.raises(ConversationNotFoundError):
        store.begin(cid, "again")


def test_idle_conversations_expire():
    clock = FakeClock()
    store = AiConversationStore(idle_ttl_s=60, clock=clock)
    cid, _ = store.begin(None, "one")
    store.commit(cid, "one", "ok")
    clock.now += 61
    with pytest.raises(ConversationNotFoundError):
        store.begin(cid, "two")


def test_busy_conversation_never_expires_mid_command():
    clock = FakeClock()
    store = AiConversationStore(idle_ttl_s=60, clock=clock)
    cid, _ = store.begin(None, "one")
    clock.now += 600
    store.begin(None, "other")  # triggers expiry sweep
    store.commit(cid, "one", "ok")
    assert store.begin(cid, "two")[0] == cid


def test_least_recently_used_conversation_is_evicted_at_capacity():
    clock = FakeClock()
    store = AiConversationStore(max_conversations=2, clock=clock)
    first, _ = store.begin(None, "a")
    store.commit(first, "a", "ok")
    clock.now += 1
    second, _ = store.begin(None, "b")
    store.commit(second, "b", "ok")
    clock.now += 1
    third, _ = store.begin(None, "c")
    assert len(store) == 2
    with pytest.raises(ConversationNotFoundError):
        store.begin(first, "again")
    store.commit(third, "c", "ok")
    assert store.begin(second, "again")[0] == second


def test_stored_history_is_trimmed():
    store = AiConversationStore()
    cid, _ = store.begin(None, "u0")
    store.commit(cid, "u0", "a0")
    for i in range(1, 15):
        store.begin(cid, f"u{i}")
        store.commit(cid, f"u{i}", f"a{i}")
    _, history = store.begin(cid, "last")
    assert len(history) <= 20
    assert history[0]["role"] == "user"
    assert history[-1] == {"role": "user", "content": "last"}
