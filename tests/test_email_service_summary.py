from datetime import UTC, datetime

from email_service.config import Settings
from email_service.schemas import AssistantQueryRequest
from email_service.service import (
    EmailInboxManager,
    _extract_ollama_content,
    create_app,
    summarize_gmail_messages,
)
from fastapi.testclient import TestClient


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
            "body_text": "Can you send this tonight? Please respond before 9pm.",
            "label_ids": ["INBOX", "UNREAD", "CATEGORY_PERSONAL"],
            "received_at": "2026-02-12T02:30:00Z",
        },
        {
            "message_id": "m-2",
            "thread_id": "t-1",
            "from_header": "Me <me@example.com>",
            "subject": "Re: URGENT: need this now",
            "snippet": "Working on it",
            "body_text": "Working on it and will send by 8pm.",
            "label_ids": ["INBOX"],
            "received_at": "2026-02-12T02:40:00Z",
        },
        {
            "message_id": "m-3",
            "thread_id": "t-2",
            "from_header": "newsletter@example.com",
            "subject": "Weekly update",
            "snippet": "new content this week",
            "body_text": "This is promotional content.",
            "label_ids": ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"],
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
    assert snapshot.focus[0].body_text == "Can you send this tonight? Please respond before 9pm."

    top_thread = snapshot.threads[0]
    assert top_thread.thread_id == "t-1"
    assert top_thread.unread_count == 1
    assert "boss@example.com" in top_thread.participants


def test_extract_ollama_content_supports_chat_and_generate_shapes() -> None:
    chat_payload = {"message": {"role": "assistant", "content": "Hello from chat"}}
    generate_payload = {"response": "Hello from generate"}
    empty_payload = {"done": True}

    assert _extract_ollama_content(chat_payload) == "Hello from chat"
    assert _extract_ollama_content(generate_payload) == "Hello from generate"
    assert _extract_ollama_content(empty_payload) is None


def test_answer_question_uses_request_model_override(tmp_path, monkeypatch) -> None:
    settings = Settings(
        local_cache_path=tmp_path / "inbox_latest.json",
        ollama_model="llama3.1:8b",
    )
    manager = EmailInboxManager(settings)
    snapshot = summarize_gmail_messages(
        [
            {
                "message_id": "m-1",
                "thread_id": "t-1",
                "from_header": "alice@example.com",
                "subject": "hello",
                "snippet": "need a reply",
                "label_ids": ["INBOX", "UNREAD"],
                "received_at": "2026-02-12T01:00:00Z",
            }
        ],
        settings=settings,
        source="unit_test",
        now=datetime(2026, 2, 12, 3, 0, 0, tzinfo=UTC),
    )
    manager._write_snapshot(snapshot)

    captured: dict[str, object] = {}

    def fake_query(question: str, messages: list[object], *, model_name: str) -> str:
        captured["question"] = question
        captured["messages"] = len(messages)
        captured["model_name"] = model_name
        return "ok"

    monkeypatch.setattr(manager, "_query_ollama", fake_query)

    response = manager.answer_question(
        AssistantQueryRequest(
            question="what needs my reply?",
            model="mistral:latest",
            max_messages=20,
        )
    )

    assert response.model == "mistral:latest"
    assert response.answer == "ok"
    assert captured["model_name"] == "mistral:latest"
    assert captured["messages"] == 1


def test_assistant_models_endpoint_returns_fallback_when_ollama_disabled(tmp_path) -> None:
    settings = Settings(
        ollama_enabled=False,
        ollama_model="llama3.1:8b",
        local_cache_path=tmp_path / "inbox_latest.json",
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/v1/assistant/models")
    assert response.status_code == 200
    payload = response.json()

    assert payload["reachable"] is False
    assert payload["models"] == ["llama3.1:8b"]
    assert payload["default_model"] == "llama3.1:8b"
