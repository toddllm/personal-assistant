from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
import httpx

from email_service.config import Settings
from email_service.schemas import (
    AssistantModelsResponse,
    AssistantQueryRequest,
    AssistantQueryResponse,
    AssistantStatusResponse,
    EmailMessage,
    EmailThread,
    HealthResponse,
    InboxRefreshRequest,
    InboxSnapshotResponse,
    SenderCount,
)

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"<([^>]+)>")
REPLY_HINT_TERMS = (
    "please",
    "can you",
    "could you",
    "let me know",
    "need you",
    "action required",
    "follow up",
    "follow-up",
)

_INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Email Service</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&display=swap');
    :root {
      --bg: #f5f7f2;
      --ink: #1f2a1f;
      --muted: #516154;
      --surface: #ffffff;
      --accent: #0b7a5a;
      --accent-2: #d2f5dd;
      --line: #d7e1d8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Space Grotesk', sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 10%, #dff8ea 0, transparent 45%),
        radial-gradient(circle at 90% 0, #f3ead3 0, transparent 38%),
        var(--bg);
    }
    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 20px 48px;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 1.7rem;
      letter-spacing: -0.02em;
    }
    .sub { color: var(--muted); margin-bottom: 18px; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
    button, input, textarea, select {
      border: 1px solid var(--line);
      border-radius: 11px;
      font: inherit;
    }
    button {
      background: var(--accent);
      color: white;
      border: 0;
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary {
      background: var(--surface);
      color: var(--ink);
      border: 1px solid var(--line);
    }
    input {
      padding: 10px 12px;
      min-width: 140px;
      background: var(--surface);
    }
    select {
      padding: 10px 12px;
      min-width: 180px;
      background: var(--surface);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
    }
    .k { color: var(--muted); font-size: 0.88rem; }
    .v { font-size: 1.35rem; font-weight: 700; }
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      margin-bottom: 14px;
    }
    .panel h2 { margin: 0 0 10px; font-size: 1.1rem; }
    .focus-item {
      border-top: 1px solid var(--line);
      padding: 10px 0;
    }
    .focus-item:first-child { border-top: 0; padding-top: 0; }
    .meta {
      font-size: 0.8rem;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .subject { font-weight: 700; margin-bottom: 4px; }
    .snippet { color: var(--muted); font-size: 0.95rem; }
    textarea {
      width: 100%;
      min-height: 96px;
      resize: vertical;
      padding: 10px 12px;
      margin-bottom: 8px;
      background: #fbfdf9;
    }
    .answer {
      white-space: pre-wrap;
      background: var(--accent-2);
      border-radius: 10px;
      padding: 10px;
      min-height: 58px;
    }
    .tiny { color: var(--muted); font-size: 0.84rem; }
    .status {
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 0.8rem;
      background: #eef3ed;
      border: 1px solid var(--line);
      display: inline-block;
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>Email Service</h1>
    <div class=\"sub\">Local inbox triage + Ollama Q&A over your synced Gmail snapshot.</div>

    <div class=\"row\">
      <button id=\"refreshBtn\">Refresh Inbox</button>
      <button id=\"reloadBtn\" class=\"secondary\">Reload Snapshot</button>
      <span id=\"healthBadge\" class=\"status\">Loading…</span>
    </div>

    <div class=\"grid\">
      <div class=\"card\"><div class=\"k\">Messages</div><div id=\"mCount\" class=\"v\">-</div></div>
      <div class=\"card\"><div class=\"k\">Unread</div><div id=\"uCount\" class=\"v\">-</div></div>
      <div class=\"card\"><div class=\"k\">Threads</div><div id=\"tCount\" class=\"v\">-</div></div>
      <div class=\"card\"><div class=\"k\">Focus Queue</div><div id=\"fCount\" class=\"v\">-</div></div>
    </div>

    <div class=\"panel\">
      <h2>Ask About Inbox (Ollama)</h2>
      <textarea id=\"question\" placeholder=\"What needs my response tonight?\"></textarea>
      <div class=\"row\">
        <select id=\"assistantModel\"></select>
        <button id=\"refreshModelsBtn\" class=\"secondary\">Refresh Models</button>
        <button id=\"setDefaultModelBtn\" class=\"secondary\">Set Default Model</button>
        <input id=\"maxMessages\" type=\"number\" min=\"1\" max=\"200\" value=\"30\" />
        <label class=\"tiny\"><input id=\"focusOnly\" type=\"checkbox\" /> focus only</label>
        <button id=\"askBtn\">Ask</button>
      </div>
      <div id=\"modelStatus\" class=\"tiny\"></div>
      <div id=\"answer\" class=\"answer\">No answer yet.</div>
      <div id=\"answerMeta\" class=\"tiny\"></div>
    </div>

    <div class=\"panel\">
      <h2>Focus Items</h2>
      <div id=\"focusList\" class=\"tiny\">No data yet.</div>
    </div>
  </div>

  <script>
    const DEFAULT_MODEL_STORAGE_KEY = 'email_service_default_model';
    const state = { snapshot: null };

    const healthBadge = document.getElementById('healthBadge');
    const focusList = document.getElementById('focusList');
    const assistantModel = document.getElementById('assistantModel');
    const modelStatus = document.getElementById('modelStatus');

    function getStoredModel() {
      try {
        const value = localStorage.getItem(DEFAULT_MODEL_STORAGE_KEY);
        return value ? value.trim() : null;
      } catch (_) {
        return null;
      }
    }

    function setStoredModel(model) {
      if (!model) return;
      try {
        localStorage.setItem(DEFAULT_MODEL_STORAGE_KEY, model);
      } catch (_) {}
    }

    function selectedModel() {
      const value = (assistantModel.value || '').trim();
      return value || null;
    }

    function fmtTs(ts) {
      try { return new Date(ts).toLocaleString(); }
      catch (_) { return ts || ''; }
    }

    function setSummary(snapshot) {
      document.getElementById('mCount').textContent = snapshot.message_count ?? '-';
      document.getElementById('uCount').textContent = snapshot.unread_count ?? '-';
      document.getElementById('tCount').textContent = snapshot.thread_count ?? '-';
      document.getElementById('fCount').textContent = snapshot.focus_count ?? '-';
    }

    function renderFocus(items) {
      if (!items || items.length === 0) {
        focusList.textContent = 'No focus items.';
        return;
      }
      focusList.innerHTML = items.map((m) => {
        const sender = m.sender_email || m.from_header || 'unknown';
        const subj = m.subject || '(no subject)';
        return `
          <div class=\"focus-item\">
            <div class=\"meta\">${sender} • ${fmtTs(m.received_at)} • p${m.priority_score}</div>
            <div class=\"subject\">${subj}</div>
            <div class=\"snippet\">${m.snippet || ''}</div>
          </div>
        `;
      }).join('');
    }

    async function loadHealth() {
      try {
        const r = await fetch('/health');
        const h = await r.json();
        const ollama = h.ollama_enabled
          ? (h.ollama_reachable ? 'ollama:ok' : 'ollama:down')
          : 'ollama:off';
        healthBadge.textContent = `${h.status} • google:${h.google_sync_reachable ? 'ok' : 'down'} • ${ollama}`;
      } catch (e) {
        healthBadge.textContent = 'health unavailable';
      }
    }

    async function loadOverview(refresh = false) {
      const url = refresh ? '/v1/inbox/overview?refresh=true' : '/v1/inbox/overview';
      const r = await fetch(url);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `overview failed (${r.status})`);
      }
      const data = await r.json();
      state.snapshot = data;
      setSummary(data);
      renderFocus(data.focus || []);
    }

    async function refreshInbox() {
      const r = await fetch('/v1/inbox/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force_sync: true, max_results: 50, label_ids: ['INBOX'] }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `refresh failed (${r.status})`);
      }
      const data = await r.json();
      state.snapshot = data;
      setSummary(data);
      renderFocus(data.focus || []);
    }

    async function loadAssistantModels() {
      const current = selectedModel();
      const stored = getStoredModel();
      const r = await fetch('/v1/assistant/models');
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `models failed (${r.status})`);
      }
      const data = await r.json();
      const models = Array.isArray(data.models) && data.models.length
        ? data.models
        : [data.default_model || 'llama3.1:8b'];
      let selected = models[0];
      if (current && models.includes(current)) {
        selected = current;
      } else if (stored && models.includes(stored)) {
        selected = stored;
      } else if (data.default_model && models.includes(data.default_model)) {
        selected = data.default_model;
      }
      assistantModel.innerHTML = '';
      for (const model of models) {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        assistantModel.appendChild(option);
      }
      assistantModel.value = selected;

      const saved = getStoredModel();
      if (data.reachable === false) {
        modelStatus.textContent = `Ollama unreachable. Using fallback model list. Saved default: ${saved || 'none'}.`;
      } else {
        modelStatus.textContent = `Loaded ${models.length} model(s). Saved default: ${saved || 'none'}.`;
      }
    }

    async function askAssistant() {
      const question = document.getElementById('question').value.trim();
      if (!question) return;
      const answer = document.getElementById('answer');
      const meta = document.getElementById('answerMeta');
      answer.textContent = 'Thinking…';
      meta.textContent = '';

      const payload = {
        question,
        refresh: false,
        focus_only: document.getElementById('focusOnly').checked,
        max_messages: Number(document.getElementById('maxMessages').value || 30),
        model: selectedModel(),
      };

      const r = await fetch('/v1/assistant/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        answer.textContent = err.detail || `ask failed (${r.status})`;
        return;
      }
      const data = await r.json();
      answer.textContent = data.answer || '(empty answer)';
      meta.textContent = `model=${data.model} • messages=${data.messages_used}`;
    }

    document.getElementById('refreshBtn').addEventListener('click', async () => {
      try { await refreshInbox(); await loadHealth(); }
      catch (e) { alert(e.message); }
    });

    document.getElementById('reloadBtn').addEventListener('click', async () => {
      try { await loadOverview(false); await loadHealth(); }
      catch (e) { alert(e.message); }
    });

    document.getElementById('askBtn').addEventListener('click', async () => {
      try { await askAssistant(); }
      catch (e) { alert(e.message); }
    });

    document.getElementById('refreshModelsBtn').addEventListener('click', async () => {
      try { await loadAssistantModels(); }
      catch (e) { alert(e.message); }
    });

    document.getElementById('setDefaultModelBtn').addEventListener('click', () => {
      const model = selectedModel();
      if (!model) {
        modelStatus.textContent = 'Select a model first.';
        return;
      }
      setStoredModel(model);
      modelStatus.textContent = `Saved default model: ${model}`;
    });

    (async () => {
      await loadHealth();
      try { await loadAssistantModels(); }
      catch (_) {}
      try { await loadOverview(false); }
      catch (_) {}
    })();
  </script>
</body>
</html>
"""


def summarize_gmail_messages(
    raw_messages: list[dict[str, Any]],
    *,
    settings: Settings,
    source: str,
    focus_limit: int | None = None,
    now: datetime | None = None,
) -> InboxSnapshotResponse:
    current = _utc_now() if now is None else _to_utc(now)
    focus_cap = max(1, int(focus_limit or settings.focus_default_limit))

    sender_counts: Counter[str] = Counter()
    messages: list[EmailMessage] = []
    thread_buckets: dict[str, dict[str, Any]] = {}

    for raw in raw_messages:
        message_id = _clean_string(raw.get("message_id") or raw.get("id"))
        if not message_id:
            continue

        thread_id = _clean_string(raw.get("thread_id") or raw.get("threadId")) or message_id
        from_header = _clean_string(raw.get("from_header") or raw.get("from"))
        sender_email = _canonical_sender(from_header)
        subject = _clean_string(raw.get("subject"))
        snippet = _clean_string(raw.get("snippet")) or ""
        received_at = _parse_datetime(raw.get("received_at"), fallback=current)

        raw_labels = raw.get("label_ids") or raw.get("labelIds") or []
        label_ids = [str(label).strip() for label in raw_labels if str(label).strip()]
        label_set = {label.upper() for label in label_ids}
        unread = "UNREAD" in label_set

        needs_reply = _needs_reply(unread=unread, subject=subject, snippet=snippet)
        priority_score = _priority_score(
            unread=unread,
            sender_email=sender_email,
            subject=subject,
            snippet=snippet,
            label_set=label_set,
            received_at=received_at,
            now=current,
            urgent_terms=settings.urgent_terms_list,
            important_senders=settings.important_senders_list,
            needs_reply=needs_reply,
        )

        message = EmailMessage(
            message_id=message_id,
            thread_id=thread_id,
            from_header=from_header,
            sender_email=sender_email,
            subject=subject,
            snippet=snippet,
            label_ids=label_ids,
            received_at=received_at,
            unread=unread,
            needs_reply=needs_reply,
            priority_score=priority_score,
        )
        messages.append(message)

        if sender_email:
            sender_counts[sender_email] += 1

        bucket = thread_buckets.get(thread_id)
        if bucket is None:
            bucket = {
                "thread_id": thread_id,
                "subject": subject,
                "latest_at": received_at,
                "message_count": 0,
                "unread_count": 0,
                "participants": set(),
                "needs_reply": False,
                "priority_score": 0,
                "message_ids": [],
            }
            thread_buckets[thread_id] = bucket

        bucket["message_count"] += 1
        if unread:
            bucket["unread_count"] += 1
        if sender_email:
            bucket["participants"].add(sender_email)
        if received_at > bucket["latest_at"]:
            bucket["latest_at"] = received_at
        if subject and not bucket["subject"]:
            bucket["subject"] = subject
        bucket["needs_reply"] = bool(bucket["needs_reply"] or needs_reply)
        bucket["priority_score"] = max(int(bucket["priority_score"]), priority_score)
        bucket["message_ids"].append(message_id)

    messages.sort(key=lambda item: (item.priority_score, item.received_at), reverse=True)

    threads: list[EmailThread] = []
    for bucket in thread_buckets.values():
        participants = sorted(bucket["participants"])
        threads.append(
            EmailThread(
                thread_id=bucket["thread_id"],
                subject=bucket["subject"],
                latest_at=bucket["latest_at"],
                message_count=bucket["message_count"],
                unread_count=bucket["unread_count"],
                participants=participants,
                needs_reply=bool(bucket["needs_reply"]),
                priority_score=int(bucket["priority_score"]),
                message_ids=bucket["message_ids"],
            )
        )
    threads.sort(key=lambda item: (item.priority_score, item.latest_at), reverse=True)

    focus = [
        message
        for message in messages
        if message.unread or message.needs_reply or message.priority_score >= 6
    ][:focus_cap]
    if not focus:
        focus = messages[:focus_cap]

    top_senders = [
        SenderCount(sender=sender, count=count)
        for sender, count in sender_counts.most_common(10)
    ]

    return InboxSnapshotResponse(
        generated_at=current,
        source=source,
        message_count=len(messages),
        unread_count=sum(1 for message in messages if message.unread),
        thread_count=len(threads),
        focus_count=len(focus),
        top_senders=top_senders,
        messages=messages,
        threads=threads,
        focus=focus,
        warnings=[],
    )


class EmailInboxManager:
    def __init__(self, settings: Settings):
        self._settings = settings

    def refresh(self, request: InboxRefreshRequest) -> InboxSnapshotResponse:
        try:
            source, payload = self._fetch_google_payload(request)
            raw_messages = payload.get("messages")
            if not isinstance(raw_messages, list):
                raise RuntimeError("google-sync-service returned an invalid payload.")

            snapshot = summarize_gmail_messages(
                raw_messages,
                settings=self._settings,
                source=source,
            )
            self._write_snapshot(snapshot)
            return snapshot
        except Exception as exc:  # noqa: BLE001
            logger.warning("email refresh failed: %s", exc)
            if not request.allow_cache_fallback:
                raise

            cached = self.load_snapshot()
            if cached is None:
                raise

            warnings = [*cached.warnings, f"refresh_failed: {exc}"]
            return cached.model_copy(update={"warnings": warnings, "source": "local_cache"})

    def load_snapshot(self) -> InboxSnapshotResponse | None:
        path = self._settings.local_cache_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return InboxSnapshotResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to read email snapshot %s: %s", path, exc)
            return None

    def google_sync_status(self) -> tuple[bool, bool | None]:
        url = f"{self._settings.google_sync_service_url.rstrip('/')}/v1/auth/status"
        timeout = httpx.Timeout(self._settings.google_sync_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url)
            if response.status_code >= 400:
                return True, None
            payload = response.json()
            if not isinstance(payload, dict):
                return True, None
            configured = payload.get("configured")
            return True, bool(configured) if configured is not None else None
        except Exception:  # noqa: BLE001
            return False, None

    def assistant_status(self) -> AssistantStatusResponse:
        if not self._settings.ollama_enabled:
            return AssistantStatusResponse(
                enabled=False,
                reachable=False,
                model=self._settings.ollama_model,
                url=self._settings.ollama_url,
                detail="disabled by EMAIL_SERVICE_OLLAMA_ENABLED",
            )

        try:
            timeout = httpx.Timeout(self._settings.ollama_timeout_seconds)
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{self._settings.ollama_url.rstrip('/')}/api/tags")
            if response.status_code >= 400:
                detail = _extract_error_detail(response)
                return AssistantStatusResponse(
                    enabled=True,
                    reachable=False,
                    model=self._settings.ollama_model,
                    url=self._settings.ollama_url,
                    detail=detail,
                )
            return AssistantStatusResponse(
                enabled=True,
                reachable=True,
                model=self._settings.ollama_model,
                url=self._settings.ollama_url,
            )
        except Exception as exc:  # noqa: BLE001
            return AssistantStatusResponse(
                enabled=True,
                reachable=False,
                model=self._settings.ollama_model,
                url=self._settings.ollama_url,
                detail=str(exc),
            )

    def assistant_models(self) -> AssistantModelsResponse:
        default_model = self._settings.ollama_model
        if not self._settings.ollama_enabled:
            return AssistantModelsResponse(
                reachable=False,
                models=[default_model],
                default_model=default_model,
                detail="disabled by EMAIL_SERVICE_OLLAMA_ENABLED",
            )

        try:
            timeout = httpx.Timeout(self._settings.ollama_timeout_seconds)
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{self._settings.ollama_url.rstrip('/')}/api/tags")
            if response.status_code >= 400:
                detail = _extract_error_detail(response)
                return AssistantModelsResponse(
                    reachable=False,
                    models=[default_model],
                    default_model=default_model,
                    detail=detail,
                )

            payload = response.json()
            names: list[str] = []
            for item in payload.get("models", []):
                name = str(item.get("name", "")).strip()
                if name:
                    names.append(name)
            if not names:
                names = [default_model]
            if default_model not in names:
                names.append(default_model)
            return AssistantModelsResponse(
                reachable=True,
                models=names,
                default_model=default_model,
            )
        except Exception as exc:  # noqa: BLE001
            return AssistantModelsResponse(
                reachable=False,
                models=[default_model],
                default_model=default_model,
                detail=str(exc),
            )

    def answer_question(self, request: AssistantQueryRequest) -> AssistantQueryResponse:
        if not self._settings.ollama_enabled:
            raise RuntimeError("Ollama is disabled. Set EMAIL_SERVICE_OLLAMA_ENABLED=true.")

        snapshot = self._resolve_snapshot_for_query(request.refresh)
        selected = _select_context_messages(
            snapshot,
            max_messages=min(request.max_messages, self._settings.ollama_max_context_messages),
            focus_only=request.focus_only,
        )
        requested_model = str(request.model or "").strip() or None
        model_name = requested_model or self._settings.ollama_model
        answer = self._query_ollama(request.question, selected, model_name=model_name)
        return AssistantQueryResponse(
            answer=answer,
            model=model_name,
            generated_at=_utc_now(),
            messages_used=len(selected),
            warnings=snapshot.warnings,
        )

    def _resolve_snapshot_for_query(self, refresh: bool) -> InboxSnapshotResponse:
        if refresh:
            return self.refresh(
                InboxRefreshRequest(
                    force_sync=True,
                    max_results=self._settings.google_sync_default_max_results,
                    label_ids=self._settings.default_label_ids_list,
                    allow_cache_fallback=True,
                )
            )

        snapshot = self.load_snapshot()
        if snapshot is not None:
            return snapshot

        return self.refresh(
            InboxRefreshRequest(
                force_sync=True,
                max_results=self._settings.google_sync_default_max_results,
                label_ids=self._settings.default_label_ids_list,
                allow_cache_fallback=True,
            )
        )

    def _query_ollama(
        self,
        question: str,
        messages: list[EmailMessage],
        *,
        model_name: str,
    ) -> str:
        context_lines = _render_context_lines(messages)
        system_prompt = (
            "You are a concise email assistant. Answer using only the provided inbox context. "
            "If the answer is unknown, say that directly. Cite message ids used in your answer."
        )
        user_prompt = (
            f"Question: {question}\n\n"
            f"Current time (UTC): {_utc_now().isoformat()}\n"
            "Inbox context:\n"
            f"{context_lines}"
        )
        body: dict[str, Any] = {
            "model": model_name,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": self._settings.ollama_temperature,
            },
        }

        timeout = httpx.Timeout(self._settings.ollama_timeout_seconds)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{self._settings.ollama_url.rstrip('/')}/api/chat", json=body)

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            raise RuntimeError(f"ollama {response.status_code}: {detail}")

        payload = response.json()
        content = _extract_ollama_content(payload)
        if not content:
            raise RuntimeError("Ollama response missing message content.")
        return content

    def _fetch_google_payload(self, request: InboxRefreshRequest) -> tuple[str, dict[str, Any]]:
        self._maybe_start_google_sync_service()
        base_url = self._settings.google_sync_service_url.rstrip("/")
        timeout = httpx.Timeout(self._settings.google_sync_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                if request.force_sync:
                    sync_body: dict[str, Any] = {}
                    max_results = request.max_results or self._settings.google_sync_default_max_results
                    sync_body["max_results"] = max(1, int(max_results))
                    sync_body["label_ids"] = request.label_ids or self._settings.default_label_ids_list
                    if request.query:
                        sync_body["query"] = request.query
                    response = client.post(f"{base_url}/v1/gmail/sync", json=sync_body)
                    source = "google_sync_live"
                else:
                    response = client.get(f"{base_url}/v1/gmail/latest")
                    source = "google_sync_cache"
        except httpx.ConnectError as exc:
            raise RuntimeError(
                "google-sync-service is not reachable. Start it with "
                "`bash scripts/google-sync-service.sh start` and retry."
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                "google-sync-service timed out. Check `data/logs/google-sync-service-supervisor.log`."
            ) from exc

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            raise RuntimeError(f"google-sync-service {response.status_code}: {detail}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("google-sync-service response is not a JSON object.")
        return source, payload

    def _google_sync_health_reachable(self, timeout_seconds: float = 1.0) -> bool:
        base_url = self._settings.google_sync_service_url.rstrip("/")
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(f"{base_url}/health")
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _maybe_start_google_sync_service(self) -> None:
        if not self._settings.google_sync_autostart:
            return
        if self._google_sync_health_reachable(timeout_seconds=1.2):
            return

        command = str(self._settings.google_sync_autostart_command or "").strip()
        cmd = shlex.split(command) if command else []
        if not cmd:
            cmd = ["google-sync-service"]

        env = os.environ.copy()
        cwd = str(self._settings.google_sync_autostart_cwd) if self._settings.google_sync_autostart_cwd else None
        started_process: subprocess.Popen[bytes] | None = None
        try:
            started_process = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            fallback_cmd = [sys.executable, "-m", "google_sync_service.main"]
            started_process = subprocess.Popen(  # noqa: S603
                fallback_cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to auto-start google-sync-service: %s", exc)
            return

        timeout_seconds = max(2.0, float(self._settings.google_sync_autostart_timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._google_sync_health_reachable(timeout_seconds=1.0):
                logger.info("google-sync-service auto-started successfully.")
                return
            if started_process.poll() is not None:
                logger.warning(
                    "google-sync-service exited before becoming ready (exit_code=%s).",
                    started_process.returncode,
                )
                return
            time.sleep(0.3)

    def _write_snapshot(self, snapshot: InboxSnapshotResponse) -> None:
        path: Path = self._settings.local_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("could not chmod email snapshot %s: %s", path, exc)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="email-service", version="0.1.0")
    manager = EmailInboxManager(settings)

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return _INDEX_HTML

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        google_sync_reachable, google_sync_configured = manager.google_sync_status()
        assistant = manager.assistant_status()
        snapshot = manager.load_snapshot()

        cache_exists = snapshot is not None
        cache_message_count = snapshot.message_count if snapshot else 0

        status = "ok"
        if not google_sync_reachable and not cache_exists:
            status = "degraded"

        return HealthResponse(
            status=status,
            service="email-service",
            google_sync_reachable=google_sync_reachable,
            google_sync_configured=google_sync_configured,
            ollama_enabled=assistant.enabled,
            ollama_reachable=assistant.reachable,
            ollama_model=assistant.model,
            cache_exists=cache_exists,
            cache_message_count=cache_message_count,
            now=_utc_now(),
        )

    @app.get("/v1/assistant/status", response_model=AssistantStatusResponse)
    def assistant_status() -> AssistantStatusResponse:
        return manager.assistant_status()

    @app.get("/v1/assistant/models", response_model=AssistantModelsResponse)
    def assistant_models() -> AssistantModelsResponse:
        return manager.assistant_models()

    @app.post("/v1/assistant/query", response_model=AssistantQueryResponse)
    def assistant_query(request: AssistantQueryRequest) -> AssistantQueryResponse:
        if not request.question.strip():
            raise HTTPException(status_code=400, detail="Question is required.")
        if request.max_messages < 1:
            raise HTTPException(status_code=400, detail="max_messages must be >= 1")
        try:
            return manager.answer_question(request)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/inbox/refresh", response_model=InboxSnapshotResponse)
    def inbox_refresh(request: InboxRefreshRequest) -> InboxSnapshotResponse:
        try:
            return manager.refresh(request)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/inbox/overview", response_model=InboxSnapshotResponse)
    def inbox_overview(
        refresh: bool = Query(default=False),
        max_results: int | None = Query(default=None, ge=1, le=500),
        query: str | None = Query(default=None),
    ) -> InboxSnapshotResponse:
        if refresh:
            request = InboxRefreshRequest(
                force_sync=True,
                max_results=max_results,
                query=query,
                allow_cache_fallback=True,
            )
            try:
                return manager.refresh(request)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot

    @app.get("/v1/inbox/focus", response_model=list[EmailMessage])
    def inbox_focus(limit: int = Query(default=15, ge=1, le=100)) -> list[EmailMessage]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.focus[:limit]

    @app.get("/v1/inbox/threads", response_model=list[EmailThread])
    def inbox_threads(limit: int = Query(default=50, ge=1, le=500)) -> list[EmailThread]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.threads[:limit]

    @app.get("/v1/inbox/messages", response_model=list[EmailMessage])
    def inbox_messages(limit: int = Query(default=100, ge=1, le=1000)) -> list[EmailMessage]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.messages[:limit]

    return app


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
            return _to_utc(parsed)
        except ValueError:
            return fallback
    return fallback


def _canonical_sender(from_header: str | None) -> str | None:
    if not from_header:
        return None
    match = EMAIL_RE.search(from_header)
    if match:
        return match.group(1).strip().lower() or None
    return from_header.strip().lower() or None


def _needs_reply(*, unread: bool, subject: str | None, snippet: str) -> bool:
    if not unread:
        return False
    combined = " ".join(part for part in [subject or "", snippet] if part).lower()
    if "?" in combined:
        return True
    return any(term in combined for term in REPLY_HINT_TERMS)


def _priority_score(
    *,
    unread: bool,
    sender_email: str | None,
    subject: str | None,
    snippet: str,
    label_set: set[str],
    received_at: datetime,
    now: datetime,
    urgent_terms: list[str],
    important_senders: list[str],
    needs_reply: bool,
) -> int:
    score = 0

    if unread:
        score += 4
    if needs_reply:
        score += 2
    if sender_email and sender_email in important_senders:
        score += 4

    text = " ".join(part for part in [subject or "", snippet] if part).lower()
    if any(term in text for term in urgent_terms):
        score += 3

    if "IMPORTANT" in label_set:
        score += 2

    age_seconds = max(0.0, (now - received_at).total_seconds())
    if age_seconds <= 6 * 3600:
        score += 3
    elif age_seconds <= 24 * 3600:
        score += 2
    elif age_seconds <= 72 * 3600:
        score += 1

    return score


def _select_context_messages(
    snapshot: InboxSnapshotResponse,
    *,
    max_messages: int,
    focus_only: bool,
) -> list[EmailMessage]:
    source = snapshot.focus if focus_only else snapshot.messages
    if max_messages <= 0:
        return []
    return source[:max_messages]


def _render_context_lines(messages: list[EmailMessage]) -> str:
    if not messages:
        return "(no messages available)"

    lines: list[str] = []
    for item in messages:
        sender = item.sender_email or item.from_header or "unknown"
        subject = item.subject or "(no subject)"
        snippet = (item.snippet or "").replace("\n", " ").strip()
        if len(snippet) > 260:
            snippet = f"{snippet[:257]}..."
        lines.append(
            " | ".join(
                [
                    f"id={item.message_id}",
                    f"time={item.received_at.isoformat()}",
                    f"unread={item.unread}",
                    f"priority={item.priority_score}",
                    f"sender={sender}",
                    f"subject={subject}",
                    f"snippet={snippet}",
                ]
            )
        )
    return "\n".join(lines)


def _extract_ollama_content(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            content = content.strip()
            if content:
                return content

    response = payload.get("response")
    if isinstance(response, str):
        response = response.strip()
        if response:
            return response

    return None


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail:
            return str(detail)
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        return json.dumps(payload, ensure_ascii=True)

    return str(payload)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["EmailInboxManager", "create_app", "summarize_gmail_messages"]
