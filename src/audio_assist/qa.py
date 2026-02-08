from __future__ import annotations

from dataclasses import dataclass
import re

import httpx

from audio_assist.storage import TranscriptRecord


@dataclass(slots=True)
class QAResult:
    answer: str
    provider: str


class QAEngine:
    def __init__(
        self,
        provider: str = "extractive",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "llama3.1:8b",
        timeout_seconds: float = 25.0,
    ):
        self._provider = provider
        self._ollama_url = ollama_url.rstrip("/")
        self._ollama_model = ollama_model
        self._timeout_seconds = timeout_seconds

    def answer(
        self,
        question: str,
        evidence: list[TranscriptRecord],
        provider: str | None = None,
        ollama_model: str | None = None,
    ) -> QAResult:
        selected_provider = provider or self._provider
        if not evidence:
            return QAResult(
                answer="No transcript context available yet. Start a source and wait for transcription.",
                provider=selected_provider,
            )
        if selected_provider == "ollama":
            answer, used_ollama = self._answer_ollama(
                question,
                evidence,
                model=ollama_model or self._ollama_model,
            )
            provider_name = "ollama" if used_ollama else "extractive-fallback"
            return QAResult(answer=answer, provider=provider_name)
        return QAResult(answer=self._answer_extractive(question, evidence), provider="extractive")

    def _answer_extractive(self, question: str, evidence: list[TranscriptRecord]) -> str:
        question_terms = {token.lower() for token in re.findall(r"[a-zA-Z0-9']+", question) if len(token) > 2}
        scored: list[tuple[int, TranscriptRecord]] = []
        for item in evidence:
            lowered = item.text.lower()
            score = sum(1 for token in question_terms if token in lowered)
            scored.append((score, item))
        scored.sort(key=lambda it: it[0], reverse=True)
        top = [record.text for _, record in scored[:3] if record.text.strip()]
        if not top:
            return "No strong match found yet. Here is the latest transcript context: " + evidence[0].text
        return " ".join(top)

    def _answer_ollama(
        self,
        question: str,
        evidence: list[TranscriptRecord],
        model: str,
    ) -> tuple[str, bool]:
        context_lines = self._build_context_lines(evidence, max_items=24, max_chars=6000)
        prompt = (
            "You answer questions using only transcript evidence.\n"
            "If evidence is insufficient, say exactly: Insufficient evidence.\n"
            "Return only the final answer.\n"
            "Do not output internal reasoning, planning, or <think> tags.\n"
            "Do not include source citations like [1] or 【1】.\n\n"
            f"Question: {question}\n\n"
            "Evidence:\n"
            + "\n".join(context_lines)
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                resp = client.post(f"{self._ollama_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
                answer = self._sanitize_ollama_answer(str(data.get("response", "")))
                if answer:
                    return answer, True
        except Exception:  # noqa: BLE001
            pass
        return self._answer_extractive(question, evidence), False

    @staticmethod
    def _build_context_lines(
        evidence: list[TranscriptRecord],
        max_items: int = 24,
        max_chars: int = 6000,
    ) -> list[str]:
        lines: list[str] = []
        total_chars = 0
        # Chronological context tends to produce more coherent summaries.
        ordered = sorted(evidence, key=lambda item: item.started_at)
        for idx, item in enumerate(ordered[:max_items], start=1):
            text = item.text.strip()
            if len(text) > 240:
                text = f"{text[:237]}..."
            speaker = f"{item.source_id}/{item.speaker}" if item.speaker else item.source_id
            line = f"{idx}. [{item.started_at.isoformat()} - {item.ended_at.isoformat()}] ({speaker}) {text}"
            total_chars += len(line)
            if total_chars > max_chars:
                break
            lines.append(line)
        return lines

    @staticmethod
    def _sanitize_ollama_answer(answer: str) -> str:
        text = answer.strip()
        if not text:
            return ""
        text = re.sub(r"<think>.*?</think>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"【\d+(?:[-–]\d+)?】", "", text)
        text = re.sub(r"\[\d+(?:[-–]\d+)?\]", "", text)
        text = text.replace("```", " ")
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = text.strip()
        if not text:
            return ""
        return text
