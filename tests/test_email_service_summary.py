from datetime import UTC, datetime

from email_service.config import Settings
from email_service.service import summarize_gmail_messages


def test_summarize_gmail_messages_builds_focus_and_threads() -> None:
    settings = Settings(
        important_senders="boss@example.com",
        urgent_terms="urgent,asap",
    )
    now = datetime(2026, 2, 12, 3, 0, 0, tzinfo=UTC)

    raw_messages = [
        {
            "message_id": "m-1",
            "thread_id": "t-1",
            "from_header": "The Boss <boss@example.com>",
            "subject": "URGENT: need this now",
            "snippet": "Can you send this tonight?",
            "label_ids": ["INBOX", "UNREAD"],
            "received_at": "2026-02-12T02:30:00Z",
        },
        {
            "message_id": "m-2",
            "thread_id": "t-1",
            "from_header": "Me <me@example.com>",
            "subject": "Re: URGENT: need this now",
            "snippet": "Working on it",
            "label_ids": ["INBOX"],
            "received_at": "2026-02-12T02:40:00Z",
        },
        {
            "message_id": "m-3",
            "thread_id": "t-2",
            "from_header": "newsletter@example.com",
            "subject": "Weekly update",
            "snippet": "new content this week",
            "label_ids": ["INBOX", "UNREAD"],
            "received_at": "2026-02-10T02:00:00Z",
        },
    ]

    snapshot = summarize_gmail_messages(
        raw_messages,
        settings=settings,
        source="unit_test",
        now=now,
        focus_limit=10,
    )

    assert snapshot.message_count == 3
    assert snapshot.unread_count == 2
    assert snapshot.thread_count == 2

    assert snapshot.focus[0].message_id == "m-1"
    assert snapshot.focus[0].priority_score > snapshot.focus[1].priority_score

    top_thread = snapshot.threads[0]
    assert top_thread.thread_id == "t-1"
    assert top_thread.unread_count == 1
    assert "boss@example.com" in top_thread.participants
