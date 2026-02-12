from __future__ import annotations

from dataclasses import dataclass
import json
import re

import httpx

from audio_assist.storage import TranscriptRecord


@dataclass(slots=True)
class QAResult:
    answer: str
    provider: str


@dataclass(slots=True)
class TranslationResult:
    id: int
    translated_text: str | None = None
    error: str | None = None


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
        force_translation: bool = False,
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
                force_translation=force_translation,
            )
            provider_name = "ollama" if used_ollama else "extractive-fallback"
            return QAResult(answer=answer, provider=provider_name)
        return QAResult(answer=self._answer_extractive(question, evidence), provider="extractive")

    def generate_title(
        self,
        evidence: list[TranscriptRecord],
        *,
        model: str | None = None,
        max_words: int = 8,
    ) -> QAResult:
        if not evidence:
            return QAResult(answer="Untitled Session", provider="extractive-fallback")
        title, used_ollama = self._generate_title_ollama(
            evidence=evidence,
            model=model or self._ollama_model,
            max_words=max_words,
        )
        if title:
            return QAResult(answer=title, provider="ollama" if used_ollama else "extractive-fallback")
        return QAResult(answer=self._generate_title_fallback(evidence, max_words=max_words), provider="extractive-fallback")

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
        force_translation: bool = False,
    ) -> tuple[str, bool]:
        context_lines = self._build_context_lines(evidence, max_items=24, max_chars=6000)
        is_translation_task = bool(force_translation)
        translation_guidance = ""
        if is_translation_task:
            translation_guidance = (
                "If the question asks for translation, find the non-English transcript lines in Evidence and "
                "return only their English translations.\n"
                "Output format: one translated line per line, no headers, no analysis, no labels.\n"
            )
        context_block = "\n".join(context_lines)
        prompt = (
            "You answer questions using only transcript evidence.\n"
            "If evidence is insufficient, say exactly: Insufficient evidence.\n"
            f"{translation_guidance}"
            "Return only the final answer.\n"
            "Do not output internal reasoning, planning, or <think> tags.\n"
            "Do not include source citations like [1] or 【1】.\n\n"
            f"Question: {question}\n\n"
            "Evidence:\n"
            f"{context_block}"
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
                if is_translation_task:
                    answer = self._sanitize_translation_answer(answer, evidence=evidence)
                if answer:
                    return answer, True
        except Exception:  # noqa: BLE001
            pass
        return self._answer_extractive(question, evidence), False

    def _generate_title_ollama(
        self,
        *,
        evidence: list[TranscriptRecord],
        model: str,
        max_words: int,
    ) -> tuple[str, bool]:
        max_words = int(max(3, min(max_words, 16)))
        context_lines = self._build_context_lines(evidence, max_items=28, max_chars=7000)
        context_block = "\n".join(context_lines)
        prompt = (
            "You create concise meeting titles from transcript evidence.\n"
            f"Return exactly one title, at most {max_words} words.\n"
            "Use plain title case text only. No quotes, no punctuation at the ends, no emojis.\n"
            "If evidence is unclear, return exactly: General Discussion.\n\n"
            "Transcript Evidence:\n"
            f"{context_block}"
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
            raw = self._sanitize_ollama_answer(str(data.get("response", "")))
            title = self._normalize_title(raw, max_words=max_words)
            if title:
                return title, True
        except Exception:  # noqa: BLE001
            pass
        return self._generate_title_fallback(evidence, max_words=max_words), False

    def _generate_title_fallback(self, evidence: list[TranscriptRecord], *, max_words: int) -> str:
        ordered = sorted(evidence, key=lambda item: item.started_at)
        for item in ordered:
            text = str(item.text or "").strip()
            if not text:
                continue
            cleaned = re.sub(r"\s+", " ", text).strip()
            title = self._normalize_title(cleaned, max_words=max_words)
            if title:
                return title
        return "General Discussion"

    @staticmethod
    def _normalize_title(text: str, *, max_words: int) -> str:
        candidate = str(text or "").strip()
        if not candidate:
            return ""
        # Keep only the first non-empty line and remove common wrappers.
        candidate = candidate.splitlines()[0].strip().strip("\"'`")
        candidate = re.sub(r"^\s*title\s*:\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -–—:;,.")
        if not candidate:
            return ""
        words = candidate.split()
        if len(words) > max_words:
            words = words[:max_words]
        normalized = " ".join(words).strip(" -–—:;,.")
        if not normalized:
            return ""
        return normalized[:96]

    def translate_records(
        self,
        records: list[TranscriptRecord],
        *,
        source_language: str | None = None,
        target_language: str = "English",
        model: str | None = None,
    ) -> list[TranslationResult]:
        if not records:
            return []
        model_name = (model or self._ollama_model).strip() or self._ollama_model
        target = str(target_language or "English").strip() or "English"
        source_hint = str(source_language or "").strip() or None

        payload_items = [
            {"id": int(item.id), "text": str(item.text or "").strip()}
            for item in records
            if str(item.text or "").strip()
        ]
        if not payload_items:
            return [TranslationResult(id=item.id, translated_text=None) for item in records]

        prompt = (
            "You are a translation engine.\n"
            f"Translate each input line into {target}.\n"
            "Preserve names and numbers. Do not summarize.\n"
            "Return ONLY strict JSON with this exact shape:\n"
            '{"translations":[{"id":123,"translation":"..."}]}\n'
            "If a line is already in the target language, keep it as-is.\n"
        )
        if source_hint:
            prompt += f"Source language hint: {source_hint}\n"
        prompt += "\nInput:\n" + json.dumps(payload_items, ensure_ascii=False)

        request_payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        translations: dict[int, str] = {}
        error_message: str | None = None
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(f"{self._ollama_url}/api/generate", json=request_payload)
                response.raise_for_status()
                data = response.json()
                raw = self._sanitize_ollama_answer(str(data.get("response", "")))
                translations = self._parse_translation_response(raw)
                if not translations:
                    error_message = "Translation output could not be parsed."
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)

        results: list[TranslationResult] = []
        for item in records:
            translated = translations.get(int(item.id))
            if translated is not None:
                results.append(TranslationResult(id=item.id, translated_text=translated))
                continue
            results.append(
                TranslationResult(
                    id=item.id,
                    translated_text=None,
                    error=error_message,
                )
            )
        return results

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
    def _parse_translation_response(raw: str) -> dict[int, str]:
        text = str(raw or "").strip()
        if not text:
            return {}

        candidates: list[str] = [text]
        if "```" in text:
            blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
            candidates.extend(block.strip() for block in blocks if block.strip())
        first_obj = text.find("{")
        last_obj = text.rfind("}")
        if first_obj != -1 and last_obj > first_obj:
            candidates.append(text[first_obj : last_obj + 1].strip())
        first_arr = text.find("[")
        last_arr = text.rfind("]")
        if first_arr != -1 and last_arr > first_arr:
            candidates.append(text[first_arr : last_arr + 1].strip())

        parsed: object | None = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                break
            except Exception:  # noqa: BLE001
                continue
        if parsed is None:
            return {}

        rows: list[object]
        if isinstance(parsed, dict):
            rows_raw = parsed.get("translations")
            rows = rows_raw if isinstance(rows_raw, list) else []
        elif isinstance(parsed, list):
            rows = parsed
        else:
            return {}

        translations: dict[int, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = row.get("id")
            raw_text = row.get("translation")
            if raw_text is None:
                raw_text = row.get("text")
            try:
                item_id = int(raw_id)
            except Exception:  # noqa: BLE001
                continue
            text_value = str(raw_text or "").strip()
            if not text_value:
                continue
            translations[item_id] = text_value
        return translations

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

    @staticmethod
    def _is_translation_task(question: str) -> bool:
        normalized = question.strip().lower()
        if not normalized:
            return False
        hints = (
            "translate",
            "translation",
            "traduc",
            "spanish",
            "espanol",
            "español",
            "to english",
            "in english",
        )
        return any(hint in normalized for hint in hints)

    @classmethod
    def _sanitize_translation_answer(cls, answer: str, evidence: list[TranscriptRecord]) -> str:
        text = answer.strip()
        if not text:
            return text

        expected = cls._estimated_translation_count(evidence)
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned_lines: list[str] = []
        seen: set[str] = set()
        meta_patterns = (
            "we need",
            "identify",
            "output format",
            "final answer",
            "translation",
            "translate",
            "evidence",
            "question",
            "should",
            "must",
            "probably",
            "maybe",
            "thus",
            "lines:",
        )

        for raw in raw_lines:
            candidate = raw
            if "=>" in candidate:
                candidate = candidate.split("=>", 1)[1].strip()
            candidate = re.sub(r"^\s*[-*•]\s*", "", candidate)
            candidate = re.sub(r"^\s*\d+[\).\:-]\s*", "", candidate)
            candidate = candidate.strip().strip("\"'`")
            normalized = candidate.lower()
            if not candidate:
                continue
            if any(token in normalized for token in meta_patterns):
                continue
            if len(candidate) < 3:
                continue
            if len(candidate) > 220:
                continue
            if re.search(r"[A-Za-z]", candidate) is None:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned_lines.append(candidate)

        if cleaned_lines and expected > 0:
            if len(cleaned_lines) > expected:
                cleaned_lines = cleaned_lines[-expected:]
            return "\n".join(cleaned_lines).strip()
        if cleaned_lines:
            return "\n".join(cleaned_lines).strip()
        return text

    @staticmethod
    def _estimated_translation_count(evidence: list[TranscriptRecord]) -> int:
        if not evidence:
            return 0
        common_spanish = {
            "de",
            "la",
            "el",
            "y",
            "que",
            "los",
            "las",
            "por",
            "para",
            "con",
            "como",
            "esta",
            "está",
            "ella",
            "mujer",
            "quiero",
            "puerto",
            "rico",
        }

        def looks_non_english(text: str) -> bool:
            lowered = text.strip().lower()
            if not lowered:
                return False
            if re.search(r"[áéíóúñü¿¡àèìòùâêîôûãõç]", lowered):
                return True
            tokens = re.findall(r"[a-záéíóúñü]+", lowered, flags=re.IGNORECASE)
            if len(tokens) < 2:
                return False
            score = sum(1 for token in tokens if token in common_spanish)
            return score >= 2

        count = sum(1 for item in evidence if looks_non_english(item.text))
        return count if count > 0 else len(evidence)
