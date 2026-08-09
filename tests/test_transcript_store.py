"""Regression tests for the shared local-first Slack transcript."""

from __future__ import annotations

import asyncio
import sqlite3
import time

from state_store import StateStore
from transcript_store import TranscriptStore


def _message(
    ts: str,
    *,
    thread_ts: str | None = None,
    text: str = "",
    user: str | None = "U1",
    bot_id: str | None = None,
    channel: str = "C1",
    subtype: str | None = None,
) -> dict:
    event = {
        "type": "message",
        "channel": channel,
        "ts": ts,
        "text": text,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if user is not None:
        event["user"] = user
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    return event


def _edit(
    message: dict,
    *,
    event_ts: str,
    text: str,
    user: str | None = None,
    bot_id: str | None = None,
) -> dict:
    edited = dict(message)
    edited["text"] = text
    edited["edited"] = {"ts": event_ts, "user": user or "UEDITOR"}
    if user is not None:
        edited["user"] = user
    if bot_id is not None:
        edited["bot_id"] = bot_id
        edited.pop("user", None)
    return {
        "type": "message",
        "subtype": "message_changed",
        "channel": message["channel"],
        "event_ts": event_ts,
        "message": edited,
        "previous_message": message,
    }


def _delete(message: dict, *, event_ts: str) -> dict:
    return {
        "type": "message",
        "subtype": "message_deleted",
        "channel": message["channel"],
        "event_ts": event_ts,
        "deleted_ts": message["ts"],
        "previous_message": message,
    }


def test_ingest_deduplicates_edits_deletes_and_out_of_order_events(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore(state)
    root = _message(
        "1.000001",
        text="original",
        user="U1",
    )

    assert transcript.ingest_event("T1", root, event_id="Ev-root")
    assert not transcript.ingest_event("T1", root, event_id="Ev-root-copy")
    snapshot = transcript.read_thread("T1", "C1", "1.000001")
    assert snapshot.complete is True
    assert snapshot.from_root is True
    assert [item["text"] for item in snapshot.messages] == ["original"]

    assert transcript.ingest_event(
        "T1",
        _edit(
            root,
            event_ts="2.000001",
            text="edited",
            user="U2",
        ),
    )
    # An older original delivery/backfill cannot overwrite the edit.
    assert not transcript.ingest_event("T1", root)
    edited = transcript.read_thread("T1", "C1", "1.000001")
    assert len(edited.messages) == 1
    assert edited.messages[0]["text"] == "edited"
    assert edited.messages[0]["user"] == "U2"
    assert edited.messages[0]["edited"]["ts"] == "2.000001"

    assert transcript.ingest_event(
        "T1", _delete(root, event_ts="3.000001")
    )
    assert not transcript.ingest_event("T1", root)
    assert transcript.read_thread(
        "T1", "C1", "1.000001"
    ).messages == []

    # An edit arriving before its original still creates one latest record.
    reply = _message(
        "4.000001",
        thread_ts="1.000001",
        text="reply-old",
    )
    assert transcript.ingest_event(
        "T1", _edit(reply, event_ts="5.000001", text="reply-new")
    )
    assert not transcript.ingest_event("T1", reply)
    messages = transcript.read_thread("T1", "C1", "1.000001").messages
    assert [(item["ts"], item["text"]) for item in messages] == [
        ("4.000001", "reply-new")
    ]


def test_changed_envelope_preserves_previous_identity_when_inner_is_sparse(
    tmp_path,
):
    transcript = TranscriptStore(StateStore(str(tmp_path / "state.db")))
    previous = _message(
        "2.0",
        thread_ts="1.0",
        text="before",
        user="UAUTHOR",
    )
    changed = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "event_ts": "3.0",
        "message": {
            "type": "message",
            "ts": "2.0",
            "text": "after",
            "edited": {"ts": "3.0"},
        },
        "previous_message": previous,
    }

    transcript.ingest_event("T1", changed)
    messages = transcript.read_thread("T1", "C1", "1.0").messages
    assert len(messages) == 1
    assert messages[0]["text"] == "after"
    assert messages[0]["user"] == "UAUTHOR"
    assert messages[0]["thread_ts"] == "1.0"


def test_team_channel_dm_and_thread_identities_are_isolated(tmp_path):
    transcript = TranscriptStore(StateStore(str(tmp_path / "state.db")))
    for team, channel, text in (
        ("T1", "C1", "team-one"),
        ("T2", "C1", "team-two"),
        ("T1", "C2", "channel-two"),
        ("T1", "D1", "direct-message"),
    ):
        transcript.ingest_event(
            team,
            _message("1.0", channel=channel, text=text),
        )

    assert transcript.read_thread("T1", "C1", "1.0").messages[0][
        "text"
    ] == "team-one"
    assert transcript.read_thread("T2", "C1", "1.0").messages[0][
        "text"
    ] == "team-two"
    assert transcript.read_thread("T1", "C2", "1.0").messages[0][
        "text"
    ] == "channel-two"
    assert transcript.read_thread("T1", "D1", "1.0").messages[0][
        "text"
    ] == "direct-message"


def test_restart_requires_one_full_revalidation_then_stays_warm(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    first.ingest_event("T1", _message("1.0", text="root"))
    assert first.read_thread("T1", "C1", "1.0").complete is True
    first_state.close()

    second_state = StateStore(path)
    second = TranscriptStore(second_state)
    restored = second.read_thread("T1", "C1", "1.0")
    assert [item["text"] for item in restored.messages] == ["root"]
    assert restored.persisted_complete is True
    assert restored.complete is False
    assert restored.needs_revalidate is True

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class Client:
            def __init__(self):
                self.calls = []

            async def conversations_replies(self, **kwargs):
                self.calls.append(kwargs)
                started.set()
                await release.wait()
                return {
                    "messages": [_message("1.0", text="root")],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                }

        client = Client()
        one = asyncio.create_task(
            second.ensure_thread(client, "T1", "C1", "1.0")
        )
        await started.wait()
        two = asyncio.create_task(
            second.ensure_thread(client, "T1", "C1", "1.0")
        )
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(one, two)
        warm = await second.ensure_thread(client, "T1", "C1", "1.0")
        return client, first_result, second_result, warm

    client, one, two, warm = asyncio.run(scenario())
    assert len(client.calls) == 1
    assert client.calls[0]["limit"] == 15
    assert "oldest" not in client.calls[0]
    assert "inclusive" not in client.calls[0]
    assert one.complete and two.complete and warm.complete
    assert second.status_snapshot()["singleflight_waits"] == 1


def test_restart_full_revalidation_still_starts_at_root_after_live_event(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    first.ingest_event("T1", _message("1.0", text="root"))
    first_state.close()

    second_state = StateStore(path)
    second = TranscriptStore(second_state)
    # Socket Mode can deliver a current event before the first activation.
    # Revalidation still requests a complete root-based remote snapshot.
    second.ingest_event(
        "T1",
        _message(
            "3.0",
            thread_ts="1.0",
            text="current live reply",
        ),
    )
    class Client:
        def __init__(self):
            self.calls = []

        async def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "messages": [
                    _message("1.0", text="root"),
                    _message(
                        "2.0",
                        thread_ts="1.0",
                        text="offline reply",
                    ),
                    _message(
                        "3.0",
                        thread_ts="1.0",
                        text="current live reply",
                    ),
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    snapshot = asyncio.run(
        second.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert "oldest" not in client.calls[0]
    assert "inclusive" not in client.calls[0]
    assert [message["ts"] for message in snapshot.messages] == [
        "1.0",
        "2.0",
        "3.0",
    ]
    assert snapshot.complete is True


def test_reply_only_terminal_page_never_proves_root_coverage(tmp_path):
    transcript = TranscriptStore(StateStore(str(tmp_path / "state.db")))
    transcript.ingest_event(
        "T1",
        _message("2.0", thread_ts="1.0", text="local reply"),
    )

    class Client:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            return {
                "messages": [
                    _message("2.0", thread_ts="1.0", text="local reply")
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    snapshot = asyncio.run(
        transcript.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert client.calls == 1
    assert snapshot.complete is False
    assert snapshot.from_root is False
    assert [message["ts"] for message in snapshot.messages] == ["2.0"]


def test_restart_full_snapshot_updates_offline_edit(tmp_path):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    root = _message("1.0", text="old root", user="UROOT")
    reply = _message("2.0", thread_ts="1.0", text="reply")
    first.ingest_event("T1", root)
    first.ingest_event("T1", reply)
    first_state.close()

    second = TranscriptStore(StateStore(path))

    class Client:
        def __init__(self):
            self.calls = []

        async def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            edited_root = dict(root)
            edited_root["text"] = "offline edited root"
            edited_root["edited"] = {"ts": "3.0", "user": "UROOT"}
            return {
                "messages": [edited_root, reply],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    snapshot = asyncio.run(
        second.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert "oldest" not in client.calls[0]
    assert snapshot.complete is True
    assert snapshot.messages[0]["text"] == "offline edited root"
    assert snapshot.messages[0]["edited"]["ts"] == "3.0"


def test_restart_full_snapshot_tombstones_offline_delete_and_persists(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    root = _message("1.0", text="root")
    deleted_reply = _message(
        "2.0", thread_ts="1.0", text="deleted offline"
    )
    first.ingest_event("T1", root)
    first.ingest_event("T1", deleted_reply)
    first_state.close()

    second_state = StateStore(path)
    second = TranscriptStore(second_state)

    class Client:
        async def conversations_replies(self, **_kwargs):
            return {
                "messages": [root],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    snapshot = asyncio.run(
        second.ensure_thread(Client(), "T1", "C1", "1.0")
    )
    assert snapshot.complete is True
    assert [message["ts"] for message in snapshot.messages] == ["1.0"]
    stored = second_state._conn.execute(
        "SELECT tombstone FROM transcript_messages "
        "WHERE team_id='T1' AND channel_id='C1' "
        "AND thread_ts='1.0' AND message_ts='2.0'"
    ).fetchone()
    assert stored is not None and stored["tombstone"] == 1
    second_state.close()

    restored = TranscriptStore(StateStore(path)).read_thread(
        "T1", "C1", "1.0"
    )
    assert [message["ts"] for message in restored.messages] == ["1.0"]
    assert restored.complete is False


def test_live_new_edit_and_delete_win_during_full_backfill(tmp_path):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    root = _message("1.0", text="root")
    edit_target = _message(
        "2.0", thread_ts="1.0", text="persisted old"
    )
    delete_target = _message(
        "3.0", thread_ts="1.0", text="persisted delete target"
    )
    for event in (root, edit_target, delete_target):
        first.ingest_event("T1", event)
    first_state.close()

    second = TranscriptStore(StateStore(path))

    class Client:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def conversations_replies(self, **_kwargs):
            self.started.set()
            await self.release.wait()
            return {
                "messages": [root, edit_target, delete_target],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    async def scenario():
        client = Client()
        pending = asyncio.create_task(
            second.ensure_thread(client, "T1", "C1", "1.0")
        )
        await client.started.wait()
        second.ingest_event(
            "T1",
            _message(
                "4.0",
                thread_ts="1.0",
                text="concurrent new",
            ),
        )
        second.ingest_event(
            "T1",
            _edit(
                edit_target,
                event_ts="10.0",
                text="concurrent live edit",
            ),
        )
        second.ingest_event(
            "T1", _delete(delete_target, event_ts="11.0")
        )
        client.release.set()
        return await pending

    snapshot = asyncio.run(scenario())
    by_ts = {message["ts"]: message for message in snapshot.messages}
    assert snapshot.complete is True
    assert by_ts["2.0"]["text"] == "concurrent live edit"
    assert "3.0" not in by_ts
    assert by_ts["4.0"]["text"] == "concurrent new"


def test_failed_multi_page_backfill_never_tombstones_missing_local_rows(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    first_state = StateStore(path)
    first = TranscriptStore(first_state)
    root = _message("1.0", text="root")
    reply = _message("2.0", thread_ts="1.0", text="keep on failure")
    first.ingest_event("T1", root)
    first.ingest_event("T1", reply)
    first_state.close()

    second_state = StateStore(path)
    second = TranscriptStore(second_state)

    class Client:
        calls = 0

        async def conversations_replies(self, **kwargs):
            self.calls += 1
            if not kwargs.get("cursor"):
                return {
                    "messages": [root],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "page-2"},
                }
            raise RuntimeError("page two failed")

    snapshot = asyncio.run(
        second.ensure_thread(Client(), "T1", "C1", "1.0")
    )
    assert snapshot.complete is False
    assert {message["ts"] for message in snapshot.messages} == {
        "1.0",
        "2.0",
    }
    stored = second_state._conn.execute(
        "SELECT tombstone FROM transcript_messages "
        "WHERE team_id='T1' AND channel_id='C1' "
        "AND thread_ts='1.0' AND message_ts='2.0'"
    ).fetchone()
    assert stored is not None and stored["tombstone"] == 0


def test_paginated_backfill_and_interleaved_live_events_use_strict_revision_order(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    state = StateStore(path)
    transcript = TranscriptStore(state)
    root = _message("1.0", text="root")

    stale_target = _message(
        "2.0", thread_ts="1.0", text="stale original"
    )
    remote_new = dict(stale_target)
    remote_new["text"] = "remote rev10"
    remote_new["edited"] = {"ts": "10.0", "user": "U1"}

    edit_target = _message(
        "3.0", thread_ts="1.0", text="edit original"
    )
    remote_before_edit = dict(edit_target)
    remote_before_edit["text"] = "remote edit rev10"
    remote_before_edit["edited"] = {"ts": "10.0", "user": "U1"}

    delete_target = _message(
        "4.0", thread_ts="1.0", text="delete original"
    )
    remote_before_delete = dict(delete_target)
    remote_before_delete["text"] = "remote delete rev10"
    remote_before_delete["edited"] = {
        "ts": "10.0",
        "user": "U1",
    }
    equal_revision_target = _message(
        "5.0", thread_ts="1.0", text="equal original"
    )
    remote_equal_revision = dict(equal_revision_target)
    remote_equal_revision["text"] = "remote equal rev10"
    remote_equal_revision["edited"] = {
        "ts": "10.0",
        "user": "U1",
    }

    class Client:
        def __init__(self):
            self.calls = 0
            self.waiting_page_two = asyncio.Event()
            self.release_page_two = asyncio.Event()

        async def conversations_replies(self, **kwargs):
            self.calls += 1
            if not kwargs.get("cursor"):
                return {
                    "messages": [
                        root,
                        remote_new,
                        remote_before_edit,
                        remote_before_delete,
                        remote_equal_revision,
                    ],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "page-2"},
                }
            self.waiting_page_two.set()
            await self.release_page_two.wait()
            return {
                "messages": [],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    async def scenario():
        client = Client()
        pending = asyncio.create_task(
            transcript.ensure_thread(client, "T1", "C1", "1.0")
        )
        await client.waiting_page_two.wait()

        # A delayed original event has revision 2 and must not overwrite the
        # already-merged authoritative rev10 edit from page one.
        assert transcript.ingest_event("T1", stale_target) is False

        live_edit = _edit(
            edit_target,
            event_ts="11.0",
            text="live edit rev11",
        )
        assert transcript.ingest_event("T1", live_edit) is True
        # Exact same-revision redelivery is idempotent.
        assert transcript.ingest_event("T1", live_edit) is False
        assert transcript.ingest_event(
            "T1", _delete(delete_target, event_ts="11.0")
        ) is True
        equal_revision_live = _edit(
            equal_revision_target,
            event_ts="10.0",
            text="live wins equal rev10",
        )
        assert transcript.ingest_event(
            "T1", equal_revision_live
        ) is True
        assert transcript.ingest_event(
            "T1", equal_revision_live
        ) is False

        client.release_page_two.set()
        return client, await pending

    client, snapshot = asyncio.run(scenario())
    assert client.calls == 2
    assert snapshot.complete is True
    by_ts = {message["ts"]: message for message in snapshot.messages}
    assert by_ts["2.0"]["text"] == "remote rev10"
    assert by_ts["3.0"]["text"] == "live edit rev11"
    assert "4.0" not in by_ts
    assert by_ts["5.0"]["text"] == "live wins equal rev10"
    state.close()

    restored = TranscriptStore(StateStore(path)).read_thread(
        "T1", "C1", "1.0"
    )
    restored_by_ts = {
        message["ts"]: message for message in restored.messages
    }
    assert restored_by_ts["2.0"]["text"] == "remote rev10"
    assert restored_by_ts["3.0"]["text"] == "live edit rev11"
    assert "4.0" not in restored_by_ts
    assert (
        restored_by_ts["5.0"]["text"] == "live wins equal rev10"
    )


def test_cold_backfill_paginates_at_15_and_preserves_newer_local_mutations(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore(state, max_messages_per_thread=5)
    root = _message("1.0", text="root")
    edited_source = _message("2.0", thread_ts="1.0", text="old")
    deleted_source = _message("3.0", thread_ts="1.0", text="delete-me")
    transcript.ingest_event(
        "T1",
        _edit(edited_source, event_ts="9.0", text="local-new"),
    )
    transcript.ingest_event(
        "T1", _delete(deleted_source, event_ts="10.0")
    )

    class Client:
        def __init__(self):
            self.calls = []

        async def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {
                    "messages": [
                        root,
                        edited_source,
                        deleted_source,
                        _message("4.0", thread_ts="1.0", text="four"),
                    ],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "page-2"},
                }
            return {
                "messages": [
                    _message("5.0", thread_ts="1.0", text="five"),
                    _message("6.0", thread_ts="1.0", text="six"),
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    snapshot = asyncio.run(
        transcript.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert len(client.calls) == 2
    assert all(call["limit"] <= 15 for call in client.calls)
    assert client.calls[1]["cursor"] == "page-2"
    assert snapshot.complete is True
    assert snapshot.truncated_before_ts
    by_ts = {item["ts"]: item for item in snapshot.messages}
    assert by_ts["2.0"]["text"] == "local-new"
    assert "3.0" not in by_ts
    assert [item["ts"] for item in snapshot.messages] == sorted(
        (item["ts"] for item in snapshot.messages), key=float
    )


def test_partial_and_429_are_cooled_down_and_return_local_context(tmp_path):
    now = [100.0]
    transcript = TranscriptStore(
        StateStore(str(tmp_path / "state.db")),
        retry_cooldown_seconds=30,
        clock=lambda: now[0],
    )
    transcript.ingest_event(
        "T1",
        _message("2.0", thread_ts="1.0", text="local reply"),
    )

    class Response:
        status_code = 429
        headers = {"Retry-After": "60"}

    class RateLimited(Exception):
        response = Response()

    class Client:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            raise RateLimited("ratelimited")

    client = Client()
    first = asyncio.run(
        transcript.ensure_thread(client, "T1", "C1", "1.0")
    )
    second = asyncio.run(
        transcript.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert client.calls == 1
    assert first.complete is False and second.complete is False
    assert [item["text"] for item in second.messages] == ["local reply"]
    assert transcript.status_snapshot()["backfill_failures"] == 1

    now[0] += 61
    asyncio.run(transcript.ensure_thread(client, "T1", "C1", "1.0"))
    assert client.calls == 2


def test_memory_and_sqlite_bounds_do_not_delete_agent_state(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    state.save_turn(
        "owner-a",
        "C-STATE:1",
        session_id="session",
        runtime="claude",
        workspace="/ws",
        summary="keep",
        input_tokens=1,
        num_turns=1,
        last_seen_ts="1",
        scope="T1",
    )
    transcript = TranscriptStore(
        state,
        max_threads=2,
        max_messages_per_thread=3,
        db_max_threads=3,
    )
    for thread_number in range(4):
        root_ts = f"{thread_number + 1}.0"
        transcript.ingest_event(
            "T1", _message(root_ts, text=f"root-{thread_number}")
        )
        for reply_number in range(5):
            transcript.ingest_event(
                "T1",
                _message(
                    f"{thread_number + 1}.{reply_number + 1}",
                    thread_ts=root_ts,
                    text=f"reply-{reply_number}",
                ),
            )

    status = transcript.status_snapshot()
    assert status["threads"] == 2
    assert status["messages"] <= 6
    assert status["evicted_threads"] >= 2
    assert state.transcript_counts()["threads"] <= 3
    assert state.transcript_counts()["messages"] <= 9
    assert state.load_agent(
        "owner-a",
        runtime="claude",
        workspace="/ws",
        ttl_seconds=3600,
        scope="T1",
    )["C-STATE:1"]["session_id"] == "session"


def test_transcript_schema_migrates_without_touching_legacy_state(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE thread_state (
            agent TEXT NOT NULL, thread_key TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '', runtime TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            num_turns INTEGER NOT NULL DEFAULT 0,
            last_seen_ts TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
            PRIMARY KEY (agent, thread_key)
        )
        """
    )
    conn.execute(
        "INSERT INTO thread_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a", "C:1", "legacy", "claude", "/ws", "summary", 1, 1, "1", 9999999999),
    )
    conn.commit()
    conn.close()

    state = StateStore(path)
    tables = {
        row[0]
        for row in state._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"transcript_threads", "transcript_messages"} <= tables
    legacy = state._conn.execute(
        "SELECT session_id, summary FROM thread_state"
    ).fetchone()
    assert tuple(legacy) == ("legacy", "summary")


def test_snapshot_and_observability_never_expose_message_text_or_tokens(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore(state)
    event = _message("1.0", text="private transcript")
    event["token"] = "xoxb-never-store"
    event["api_app_id"] = "A-PRIVATE"
    transcript.ingest_event("T1", event)

    status_text = repr(transcript.status_snapshot())
    assert "private transcript" not in status_text
    assert "xoxb-" not in status_text
    columns = {
        row["name"]
        for row in state._conn.execute(
            "PRAGMA table_info(transcript_messages)"
        )
    }
    assert "token" not in columns
    stored = state._conn.execute(
        "SELECT text, blocks_json, attachments_json "
        "FROM transcript_messages"
    ).fetchone()
    assert "xoxb-" not in " ".join(str(value or "") for value in stored)


def test_environment_capacity_configuration_is_positive_and_observable(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore.from_environment(
        state,
        {
            "TRANSCRIPT_MAX_THREADS": "7",
            "TRANSCRIPT_MAX_MESSAGES_PER_THREAD": "9",
            "TRANSCRIPT_DB_MAX_THREADS": "11",
            "TRANSCRIPT_TTL_SECONDS": "120",
            "TRANSCRIPT_RETRY_COOLDOWN_SECONDS": "45",
        },
    )

    assert transcript.max_threads == 7
    assert transcript.max_messages_per_thread == 9
    assert transcript.db_max_threads == 11
    assert transcript.ttl_seconds == 120
    assert transcript.retry_cooldown_seconds == 45
    assert transcript.status_snapshot()["capacity"] == {
        "memory_threads": 7,
        "messages_per_thread": 9,
        "persisted_threads": 11,
        "record_bytes": 64 * 1024,
        "memory_bytes": 32 * 1024 * 1024,
        "persisted_bytes": 256 * 1024 * 1024,
        "ttl_seconds": 120,
        "retry_cooldown_seconds": 45,
    }

    for name, value in (
        ("TRANSCRIPT_MAX_THREADS", "0"),
        ("TRANSCRIPT_TTL_SECONDS", "not-a-number"),
    ):
        try:
            TranscriptStore.from_environment(state, {name: value})
        except ValueError as exc:
            assert name in str(exc)
        else:
            raise AssertionError(f"{name} should reject {value!r}")


def test_same_boot_lru_sqlite_reload_is_warm_but_new_boot_revalidates_once(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    state = StateStore(path)

    class Client:
        def __init__(self):
            self.calls = []

        async def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            root_ts = kwargs["ts"]
            return {
                "messages": [
                    _message(
                        root_ts,
                        channel=kwargs["channel"],
                        text=f"root-{root_ts}",
                    )
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    first = TranscriptStore(
        state, max_threads=1, boot_id="boot-a"
    )
    asyncio.run(first.ensure_thread(client, "T1", "C1", "1.0"))
    asyncio.run(first.ensure_thread(client, "T1", "C1", "2.0"))
    assert len(client.calls) == 2

    # A was evicted from memory, but its current-boot DB validation remains
    # authoritative and must not cause API calls while A/B alternate.
    asyncio.run(first.ensure_thread(client, "T1", "C1", "1.0"))
    asyncio.run(first.ensure_thread(client, "T1", "C1", "2.0"))
    assert len(client.calls) == 2

    state.close()
    reopened_state = StateStore(path)
    same_boot = TranscriptStore(
        reopened_state, max_threads=1, boot_id="boot-a"
    )
    asyncio.run(
        same_boot.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert len(client.calls) == 2

    restarted = TranscriptStore(
        reopened_state, max_threads=1, boot_id="boot-b"
    )
    asyncio.run(
        restarted.ensure_thread(client, "T1", "C1", "1.0")
    )
    asyncio.run(
        restarted.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert len(client.calls) == 3


def test_live_root_is_context_warm_but_not_authoritative_past_watermark(
    tmp_path,
):
    transcript = TranscriptStore(
        StateStore(str(tmp_path / "state.db")),
        boot_id="boot-a",
    )
    root = _message("1.0", text="root")
    current = _message(
        "3.0", thread_ts="1.0", text="current handoff"
    )
    transcript.ingest_event("T1", root)
    transcript.ingest_event("T1", current)

    class Client:
        def __init__(self):
            self.calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            return {
                "messages": [
                    root,
                    _message(
                        "2.0",
                        thread_ts="1.0",
                        text="delayed handoff",
                    ),
                    current,
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    context = asyncio.run(
        transcript.ensure_thread(client, "T1", "C1", "1.0")
    )
    assert client.calls == 0
    assert context.complete is True
    assert context.authoritative is False

    verified = asyncio.run(
        transcript.ensure_authoritative(
            client, "T1", "C1", "1.0", current_ts="3.0"
        )
    )
    assert client.calls == 1
    assert verified.authoritative is True
    assert verified.verified_through_ts == "3.0"
    assert [message["ts"] for message in verified.messages] == [
        "1.0",
        "2.0",
        "3.0",
    ]
    asyncio.run(
        transcript.ensure_authoritative(
            client, "T1", "C1", "1.0", current_ts="3.0"
        )
    )
    assert client.calls == 1


def test_cancelled_leader_does_not_cancel_shared_backfill(tmp_path):
    transcript = TranscriptStore(
        StateStore(str(tmp_path / "state.db"))
    )

    class Client:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return {
                "messages": [_message("1.0", text="root")],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    async def scenario():
        client = Client()
        leader = asyncio.create_task(
            transcript.ensure_thread(client, "T1", "C1", "1.0")
        )
        await client.started.wait()
        waiter = asyncio.create_task(
            transcript.ensure_thread(client, "T1", "C1", "1.0")
        )
        await asyncio.sleep(0)
        leader.cancel()
        try:
            await leader
        except asyncio.CancelledError:
            pass
        client.release.set()
        result = await waiter
        warm = await transcript.ensure_thread(
            client, "T1", "C1", "1.0"
        )
        return client, result, warm

    client, result, warm = asyncio.run(scenario())
    assert client.calls == 1
    assert result.complete is True and warm.complete is True


def test_record_utf8_and_structured_payload_are_hard_bounded(tmp_path):
    path = str(tmp_path / "state.db")
    state = StateStore(path)
    transcript = TranscriptStore(
        state,
        max_record_bytes=512,
        max_memory_bytes=2048,
        max_db_bytes=4096,
    )
    event = _message("1.0", text="漢🙂" * 2000)
    event["blocks"] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "B" * 5000}}
        for _ in range(100)
    ]
    event["attachments"] = [
        {"title": "A" * 5000} for _ in range(100)
    ]
    transcript.ingest_event("T1", event)

    snapshot = transcript.read_thread("T1", "C1", "1.0")
    assert len(snapshot.messages) == 1
    encoded = snapshot.messages[0]["text"].encode("utf-8")
    encoded.decode("utf-8")
    assert snapshot.messages[0]["content_truncated"] is True
    assert snapshot.messages[0].get("blocks", []) == []
    assert snapshot.messages[0].get("attachments", []) == []
    status = transcript.status_snapshot()
    assert status["memory_bytes"] <= 2048
    assert status["truncated_records"] == 1
    stored = state._conn.execute(
        "SELECT payload_bytes, content_truncated "
        "FROM transcript_messages"
    ).fetchone()
    assert stored["payload_bytes"] <= 512
    assert stored["content_truncated"] == 1


def test_memory_and_sqlite_logical_byte_caps_evict_oldest_threads(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    state = StateStore(path)
    transcript = TranscriptStore(
        state,
        max_threads=20,
        max_messages_per_thread=10,
        db_max_threads=20,
        max_record_bytes=1024,
        max_memory_bytes=1500,
        max_db_bytes=1800,
    )
    for index in range(6):
        transcript.ingest_event(
            "T1",
            _message(
                f"{index + 1}.0",
                text=(str(index) * 700),
            ),
        )

    status = transcript.status_snapshot()
    assert status["memory_bytes"] <= 1500
    assert status["byte_evicted_threads"] > 0
    assert status["memory_byte_evicted_threads"] > 0
    assert status["persisted_byte_evicted_threads"] > 0
    counts = state.transcript_counts()
    assert counts["bytes"] <= 1800
    state.close()
    assert (tmp_path / "state.db").stat().st_size < 2 * 1024 * 1024

    reopened = StateStore(path)
    assert reopened.transcript_counts()["bytes"] <= 1800


def test_db_byte_eviction_never_leaves_empty_authoritative_meta(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=256,
        max_memory_bytes=2048,
        max_db_bytes=300,
    )

    class Client:
        async def conversations_replies(self, **_kwargs):
            return {
                "messages": [
                    _message("1.0", text="root"),
                    _message("2.0", thread_ts="1.0", text="reply"),
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    live = asyncio.run(
        transcript.ensure_authoritative(
            Client(), "T1", "C1", "1.0", current_ts="2.0"
        )
    )
    assert live.authoritative is True
    assert state.transcript_counts()["threads"] == 0

    reloaded = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=256,
        max_memory_bytes=2048,
        max_db_bytes=300,
    ).read_thread("T1", "C1", "1.0")
    assert reloaded.complete is False
    assert reloaded.authoritative is False


def test_legacy_payload_bytes_are_recalculated_before_byte_pruning(
    tmp_path,
):
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE transcript_threads (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            from_root INTEGER NOT NULL DEFAULT 0,
            truncated_before_ts TEXT NOT NULL DEFAULT '',
            last_message_ts TEXT NOT NULL DEFAULT '',
            hydrated_at REAL NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts)
        );
        CREATE TABLE transcript_messages (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            bot_id TEXT NOT NULL DEFAULT '',
            subtype TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            blocks_json TEXT NOT NULL DEFAULT '[]',
            attachments_json TEXT NOT NULL DEFAULT '[]',
            edited_ts TEXT NOT NULL DEFAULT '',
            revision_ts TEXT NOT NULL DEFAULT '',
            tombstone INTEGER NOT NULL DEFAULT 0,
            source_rank INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts, message_ts)
        );
        """
    )
    for index in range(3):
        key = (f"C{index}", f"{index + 1}.0")
        connection.execute(
            "INSERT INTO transcript_threads "
            "(team_id, channel_id, thread_ts, updated_at) "
            "VALUES ('T1', ?, ?, ?)",
            (key[0], key[1], float(index)),
        )
        connection.execute(
            "INSERT INTO transcript_messages "
            "(team_id, channel_id, thread_ts, message_ts, text, "
            "revision_ts, updated_at) VALUES ('T1', ?, ?, ?, ?, ?, ?)",
            (
                key[0],
                key[1],
                key[1],
                "旧" * 400,
                key[1],
                float(index),
            ),
        )
    connection.commit()
    connection.close()

    state = StateStore(path)
    before = state.transcript_counts()
    assert before["bytes"] > 1000
    TranscriptStore(
        state,
        max_record_bytes=1024,
        max_memory_bytes=2048,
        max_db_bytes=1024,
    )
    after = state.transcript_counts()
    assert after["bytes"] <= 1000
    assert after["threads"] < before["threads"]


def test_byte_capacity_environment_rejects_values_above_hard_max(tmp_path):
    from transcript_store import (
        TRANSCRIPT_HARD_MAX_DB_BYTES,
        TRANSCRIPT_HARD_MAX_MEMORY_BYTES,
        TRANSCRIPT_HARD_MAX_RECORD_BYTES,
    )

    state = StateStore(str(tmp_path / "state.db"))
    for name, value in (
        (
            "TRANSCRIPT_MAX_RECORD_BYTES",
            TRANSCRIPT_HARD_MAX_RECORD_BYTES + 1,
        ),
        (
            "TRANSCRIPT_MEMORY_MAX_BYTES",
            TRANSCRIPT_HARD_MAX_MEMORY_BYTES + 1,
        ),
        (
            "TRANSCRIPT_DB_MAX_BYTES",
            TRANSCRIPT_HARD_MAX_DB_BYTES + 1,
        ),
    ):
        try:
            TranscriptStore.from_environment(
                state, {name: str(value)}
            )
        except ValueError as exc:
            assert name in str(exc)
        else:
            raise AssertionError(f"{name} should enforce its hard max")


def test_truncated_remote_record_never_becomes_canonical_authority(
    tmp_path,
):
    transcript = TranscriptStore(
        StateStore(str(tmp_path / "state.db")),
        max_record_bytes=512,
    )
    root = _message("1.0", text="root")
    huge = _message(
        "2.0",
        thread_ts="1.0",
        text="<@UAGENT> " + ("攻" * 5000),
        bot_id="BPEER",
        user=None,
        subtype="bot_message",
    )

    class Client:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            return {
                "messages": [root, huge],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    snapshot = asyncio.run(
        transcript.ensure_authoritative(
            client, "T1", "C1", "1.0", current_ts="2.0"
        )
    )
    assert client.calls == 1
    assert snapshot.complete is True
    assert snapshot.authority_truncated is True
    assert snapshot.authoritative is False


def test_trimmed_away_truncated_remote_record_still_blocks_authority(
    tmp_path,
):
    transcript = TranscriptStore(
        StateStore(str(tmp_path / "state.db")),
        max_messages_per_thread=1,
        max_record_bytes=512,
    )
    huge_root = _message("1.0", text="攻" * 5000)
    latest = _message("2.0", thread_ts="1.0", text="latest")

    class Client:
        async def conversations_replies(self, **_kwargs):
            return {
                "messages": [huge_root, latest],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    snapshot = asyncio.run(
        transcript.ensure_authoritative(
            Client(), "T1", "C1", "1.0", current_ts="2.0"
        )
    )
    assert [item["ts"] for item in snapshot.messages] == ["2.0"]
    assert snapshot.authority_truncated is True
    assert snapshot.authoritative is False


def test_db_byte_eviction_during_hydration_cannot_revive_authority(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    transcript = TranscriptStore(
        state,
        boot_id="boot-a",
        max_threads=1,
        max_record_bytes=512,
        max_memory_bytes=4096,
        max_db_bytes=600,
    )
    messages = [
        _message(
            f"{index}.0",
            thread_ts=None if index == 1 else "1.0",
            text=str(index) * 200,
        )
        for index in range(1, 4)
    ]

    class FullClient:
        async def conversations_replies(self, **_kwargs):
            return {
                "messages": messages,
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    live = asyncio.run(
        transcript.ensure_authoritative(
            FullClient(), "T1", "C1", "1.0", current_ts="3.0"
        )
    )
    assert live.authoritative is True
    transcript.ingest_event(
        "T1",
        _message(
            "4.0",
            thread_ts="1.0",
            channel="C1",
            text="live after unpersistable hydration",
        ),
    )
    partial_meta = state._conn.execute(
        "SELECT complete, authoritative, validated_boot_id "
        "FROM transcript_threads WHERE team_id='T1' AND channel_id='C1' "
        "AND thread_ts='1.0'"
    ).fetchone()
    assert partial_meta is not None
    assert partial_meta["complete"] == 0
    assert partial_meta["authoritative"] == 0
    assert partial_meta["validated_boot_id"] == ""
    transcript.ingest_event(
        "T1", _message("9.0", channel="C2", text="evict memory")
    )

    class FailedReload:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("must revalidate")

    client = FailedReload()
    reloaded = asyncio.run(
        transcript.ensure_authoritative(
            client, "T1", "C1", "1.0", current_ts="4.0"
        )
    )
    assert client.calls == 1
    assert reloaded.authoritative is False
    persisted = state.load_transcript_thread(
        "T1", "C1", "1.0", limit=10, max_record_bytes=512
    )
    assert persisted is None or persisted["complete"] is False


def test_count_and_ttl_eviction_epochs_block_terminal_authority(
    tmp_path,
):
    for mode in ("count", "ttl"):
        state = StateStore(str(tmp_path / f"{mode}.db"))
        transcript = TranscriptStore(
            state,
            boot_id="boot-a",
            max_record_bytes=512,
            max_memory_bytes=4096,
            max_db_bytes=4096,
            db_max_threads=10,
        )

        class EvictingClient:
            async def conversations_replies(self, **_kwargs):
                if mode == "count":
                    state.save_transcript_meta(
                        "T1",
                        "C-other",
                        "9.0",
                        meta={},
                        max_threads=1,
                        max_bytes=4096,
                    )
                else:
                    state.sweep_transcripts(
                        0,
                        max_threads=10,
                        max_bytes=4096,
                        now=time.time() + 1,
                    )
                return {
                    "messages": [_message("1.0", text="root")],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                }

        memory = asyncio.run(
            transcript.ensure_authoritative(
                EvictingClient(),
                "T1",
                "C1",
                "1.0",
                current_ts="1.0",
            )
        )
        assert memory.authoritative is True
        assert state.transcript_eviction_epoch(
            "T1", "C1", "1.0"
        ) > 0
        persisted = state.load_transcript_thread(
            "T1",
            "C1",
            "1.0",
            limit=10,
            max_record_bytes=512,
        )
        assert persisted is None or persisted["complete"] is False


def test_global_byte_eviction_of_other_thread_does_not_block_current(
    tmp_path,
):
    state = StateStore(str(tmp_path / "state.db"))
    state.save_transcript_record(
        "T1",
        "C-old",
        "9.0",
        record={
            "ts": "9.0",
            "user": "U1",
            "text": "o" * 200,
            "revision_ts": "9.0",
            "source_rank": 2,
            "payload_bytes": 400,
        },
        meta={},
        max_messages=10,
        max_threads=10,
        max_bytes=600,
    )
    transcript = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=512,
        max_memory_bytes=4096,
        max_db_bytes=600,
    )

    class Client:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            return {
                "messages": [_message("1.0", text="n" * 200)],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

    client = Client()
    first = asyncio.run(
        transcript.ensure_authoritative(
            client, "T1", "C1", "1.0", current_ts="1.0"
        )
    )
    assert first.authoritative is True
    assert state.transcript_eviction_epoch(
        "T1", "C-old", "9.0"
    ) > 0
    assert state.transcript_eviction_epoch("T1", "C1", "1.0") == 0

    same_boot = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=512,
        max_memory_bytes=4096,
        max_db_bytes=600,
    )

    class NoHistory:
        calls = 0

        async def conversations_replies(self, **_kwargs):
            self.calls += 1
            raise AssertionError("intact current thread must stay warm")

    no_history = NoHistory()
    reloaded = asyncio.run(
        same_boot.ensure_authoritative(
            no_history,
            "T1",
            "C1",
            "1.0",
            current_ts="1.0",
        )
    )
    assert no_history.calls == 0
    assert reloaded.authoritative is True


def test_db_load_rebounds_forged_oversized_row_and_persists_truncation(
    tmp_path,
):
    path = str(tmp_path / "state.db")
    state = StateStore(path)
    now = time.time()
    state._conn.execute(
        "INSERT INTO transcript_threads "
        "(team_id, channel_id, thread_ts, complete, from_root, "
        "last_message_ts, validated_boot_id, authoritative, "
        "verified_through_ts, updated_at) "
        "VALUES ('T1', 'C1', '1.0', 1, 1, '1.0', 'boot-a', 1, "
        "'1.0', ?)",
        (now,),
    )
    state._conn.execute(
        "INSERT INTO transcript_messages "
        "(team_id, channel_id, thread_ts, message_ts, user_id, text, "
        "revision_ts, source_rank, payload_bytes, content_truncated, "
        "updated_at) VALUES "
        "('T1', 'C1', '1.0', '1.0', 'U1', ?, '1.0', 2, 1, 0, ?)",
        ("旧🙂" * 60_000, now),
    )
    state._conn.commit()

    transcript = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=512,
        max_memory_bytes=2048,
        max_db_bytes=4096,
    )
    snapshot = transcript.read_thread("T1", "C1", "1.0")
    assert snapshot.messages[0]["content_truncated"] is True
    assert len(snapshot.messages[0]["text"].encode("utf-8")) < 512
    assert snapshot.authoritative is False
    assert transcript.status_snapshot()["memory_bytes"] <= 2048
    stored = state._conn.execute(
        "SELECT payload_bytes, content_truncated, length(CAST(text AS BLOB)) "
        "AS text_bytes FROM transcript_messages"
    ).fetchone()
    assert stored["payload_bytes"] <= 512
    assert stored["content_truncated"] == 1
    assert stored["text_bytes"] < 512
    state.close()

    reopened = StateStore(path)
    again = TranscriptStore(
        reopened,
        boot_id="boot-a",
        max_record_bytes=512,
        max_memory_bytes=2048,
        max_db_bytes=4096,
    ).read_thread("T1", "C1", "1.0")
    assert again.messages[0]["content_truncated"] is True
    assert again.authoritative is False


def test_old_schema_oversized_row_is_bounded_on_first_load(tmp_path):
    path = str(tmp_path / "legacy-load.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE transcript_threads (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            from_root INTEGER NOT NULL DEFAULT 0,
            truncated_before_ts TEXT NOT NULL DEFAULT '',
            last_message_ts TEXT NOT NULL DEFAULT '',
            hydrated_at REAL NOT NULL DEFAULT 0,
            retry_after REAL NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts)
        );
        CREATE TABLE transcript_messages (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            bot_id TEXT NOT NULL DEFAULT '',
            subtype TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            blocks_json TEXT NOT NULL DEFAULT '[]',
            attachments_json TEXT NOT NULL DEFAULT '[]',
            edited_ts TEXT NOT NULL DEFAULT '',
            revision_ts TEXT NOT NULL DEFAULT '',
            tombstone INTEGER NOT NULL DEFAULT 0,
            source_rank INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts, message_ts)
        );
        """
    )
    now = time.time()
    connection.execute(
        "INSERT INTO transcript_threads "
        "(team_id, channel_id, thread_ts, complete, from_root, "
        "last_message_ts, updated_at) "
        "VALUES ('T1', 'C1', '1.0', 1, 1, '1.0', ?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO transcript_messages "
        "(team_id, channel_id, thread_ts, message_ts, user_id, text, "
        "revision_ts, source_rank, updated_at) "
        "VALUES ('T1', 'C1', '1.0', '1.0', 'U1', ?, '1.0', 2, ?)",
        ("旧" * 150_000, now),
    )
    connection.commit()
    connection.close()

    state = StateStore(path)
    transcript = TranscriptStore(
        state,
        boot_id="boot-a",
        max_record_bytes=512,
        max_memory_bytes=2048,
        max_db_bytes=1024 * 1024,
    )
    loaded = transcript.read_thread("T1", "C1", "1.0")
    assert loaded.messages[0]["content_truncated"] is True
    assert len(loaded.messages[0]["text"].encode("utf-8")) < 512
    row = state._conn.execute(
        "SELECT payload_bytes, content_truncated, "
        "length(CAST(text AS BLOB)) FROM transcript_messages"
    ).fetchone()
    assert row[0] <= 512
    assert row[1] == 1
    assert row[2] < 512


def test_byte_capacity_rejects_tiny_records_and_inverted_caps(tmp_path):
    state = StateStore(str(tmp_path / "state.db"))
    for kwargs, expected in (
        ({"max_record_bytes": 1}, "max_record_bytes"),
        (
            {"max_record_bytes": 1024, "max_memory_bytes": 512},
            "max_memory_bytes",
        ),
        (
            {"max_record_bytes": 1024, "max_db_bytes": 512},
            "max_db_bytes",
        ),
    ):
        try:
            TranscriptStore(state, **kwargs)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"{expected} should reject invalid cap")

    try:
        TranscriptStore.from_environment(
            state, {"TRANSCRIPT_MAX_RECORD_BYTES": "1"}
        )
    except ValueError as exc:
        assert "TRANSCRIPT_MAX_RECORD_BYTES" in str(exc)
    else:
        raise AssertionError("environment should reject tiny record cap")
