from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock

from fastapi import FastAPI, HTTPException
import numpy as np

from speaker_service.config import Settings
from speaker_service.schemas import DiarizeChunkRequest, DiarizeChunkResponse, DiarizedSegment


@dataclass(slots=True)
class ClusterState:
    label: str
    centroid: np.ndarray
    count: int
    last_seen: datetime


@dataclass(slots=True)
class SourceState:
    clusters: list[ClusterState] = field(default_factory=list)
    next_cluster_index: int = 1
    last_seen: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class ClusteringSpeakerEngine:
    """Lightweight per-source speaker clustering for short PCM chunks."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = Lock()
        self._states: dict[str, SourceState] = {}

    def diarize_chunk(self, req: DiarizeChunkRequest) -> DiarizeChunkResponse:
        try:
            pcm = base64.b64decode(req.pcm_s16le_base64)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid pcm_s16le_base64 payload.") from exc

        if not pcm:
            return DiarizeChunkResponse()

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return DiarizeChunkResponse()

        windows = self._windows(samples.size, req.sample_rate)
        segments: list[DiarizedSegment] = []
        for start_idx, end_idx in windows:
            window_audio = samples[start_idx:end_idx]
            embedding = self._embedding(window_audio, req.sample_rate)
            if embedding is None:
                continue
            speaker, confidence = self._assign_cluster(
                source_id=req.source_id,
                session_id=req.session_id,
                embedding=embedding,
            )
            if not speaker:
                continue
            start_sec = start_idx / req.sample_rate
            end_sec = end_idx / req.sample_rate
            segments.append(
                DiarizedSegment(
                    speaker=speaker,
                    start=start_sec,
                    end=end_sec,
                    duration=max(0.0, end_sec - start_sec),
                    confidence=confidence,
                )
            )

        if not segments:
            return DiarizeChunkResponse()

        merged = self._merge_segments(segments)
        speaker, confidence = self._dominant_speaker(merged)
        return DiarizeChunkResponse(
            speaker=speaker,
            confidence=confidence,
            segments=merged,
        )

    def status(self) -> dict[str, object]:
        now = datetime.now(tz=UTC)
        with self._lock:
            self._prune_locked(now)
            state_count = len(self._states)
            cluster_count = sum(len(item.clusters) for item in self._states.values())
        return {
            "status": "ok",
            "active_sources": state_count,
            "active_clusters": cluster_count,
            "window_seconds": self._settings.window_seconds,
            "similarity_threshold": self._settings.similarity_threshold,
            "create_threshold": self._settings.create_threshold,
        }

    def _assign_cluster(
        self,
        source_id: str,
        session_id: str | None,
        embedding: np.ndarray,
    ) -> tuple[str, float]:
        now = datetime.now(tz=UTC)
        key = self._source_key(source_id, session_id)
        with self._lock:
            self._prune_locked(now)
            state = self._states.get(key)
            if state is None:
                state = SourceState(last_seen=now)
                self._states[key] = state
            state.last_seen = now

            if not state.clusters:
                return self._create_cluster_locked(state, embedding, now)

            best_cluster: ClusterState | None = None
            best_similarity = -1.0
            for cluster in state.clusters:
                similarity = float(np.dot(cluster.centroid, embedding))
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_cluster = cluster

            if (
                best_similarity < self._settings.create_threshold
                and len(state.clusters) < self._settings.max_clusters_per_source
            ):
                return self._create_cluster_locked(state, embedding, now)

            if best_cluster is None:
                return self._create_cluster_locked(state, embedding, now)

            if best_similarity >= self._settings.similarity_threshold:
                alpha = 1.0 / float(min(best_cluster.count + 1, 12))
                updated = self._normalize_vector((1.0 - alpha) * best_cluster.centroid + alpha * embedding)
                best_cluster.centroid = updated
            best_cluster.count += 1
            best_cluster.last_seen = now
            confidence = self._similarity_to_confidence(best_similarity)
            return best_cluster.label, confidence

    def _create_cluster_locked(
        self,
        state: SourceState,
        embedding: np.ndarray,
        now: datetime,
    ) -> tuple[str, float]:
        label = f"SPK_{state.next_cluster_index:02d}"
        state.next_cluster_index += 1
        state.clusters.append(
            ClusterState(
                label=label,
                centroid=embedding.copy(),
                count=1,
                last_seen=now,
            )
        )
        return label, max(0.55, self._similarity_to_confidence(self._settings.create_threshold))

    def _windows(self, sample_count: int, sample_rate: int) -> list[tuple[int, int]]:
        min_samples = max(1, int(sample_rate * self._settings.min_window_seconds))
        target_samples = max(min_samples, int(sample_rate * self._settings.window_seconds))
        if sample_count <= target_samples:
            return [(0, sample_count)]

        step = max(1, target_samples // 2)
        windows: list[tuple[int, int]] = []
        idx = 0
        while idx < sample_count:
            end_idx = min(sample_count, idx + target_samples)
            if end_idx - idx >= min_samples:
                windows.append((idx, end_idx))
            if end_idx >= sample_count:
                break
            idx += step
        if not windows:
            windows = [(0, sample_count)]
        return windows

    def _embedding(self, samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
        frame_size = max(256, int(sample_rate * 0.025))
        hop_size = max(128, int(sample_rate * 0.01))
        if samples.size < frame_size:
            return None

        n_fft = 1
        while n_fft < frame_size:
            n_fft *= 2

        window = np.hanning(frame_size).astype(np.float32)
        feature_rows: list[np.ndarray] = []

        for start in range(0, samples.size - frame_size + 1, hop_size):
            frame = samples[start : start + frame_size]
            rms = float(np.sqrt(np.mean(frame * frame)))
            dbfs = -96.0 if rms <= 1e-9 else float(20.0 * np.log10(rms))
            if dbfs < self._settings.min_voice_dbfs:
                continue
            spectrum = np.abs(np.fft.rfft(frame * window, n=n_fft)).astype(np.float32)
            if spectrum.size <= 1:
                continue
            spectrum = spectrum[1:]
            # 32 coarse spectral bands.
            band_values = np.array(
                [float(np.mean(chunk)) for chunk in np.array_split(spectrum, 32)],
                dtype=np.float32,
            )
            feature_rows.append(np.log1p(band_values))

        if len(feature_rows) < 3:
            return None

        embedding = np.mean(np.stack(feature_rows, axis=0), axis=0)
        return self._normalize_vector(embedding)

    @staticmethod
    def _normalize_vector(vector: np.ndarray) -> np.ndarray:
        centered = vector.astype(np.float32) - float(np.mean(vector))
        norm = float(np.linalg.norm(centered))
        if norm <= 1e-8:
            return np.zeros_like(centered)
        return centered / norm

    @staticmethod
    def _source_key(source_id: str, session_id: str | None) -> str:
        source = str(source_id or "").strip() or "source"
        session = str(session_id or "").strip()
        if session:
            return f"{source}::{session}"
        return source

    @staticmethod
    def _similarity_to_confidence(similarity: float) -> float:
        clipped = max(-1.0, min(1.0, similarity))
        return max(0.0, min(1.0, (clipped + 1.0) * 0.5))

    @staticmethod
    def _merge_segments(segments: list[DiarizedSegment], max_gap_seconds: float = 0.08) -> list[DiarizedSegment]:
        if not segments:
            return []
        ordered = sorted(segments, key=lambda item: (item.start, item.end))
        merged: list[DiarizedSegment] = [ordered[0]]
        for seg in ordered[1:]:
            prev = merged[-1]
            if seg.speaker == prev.speaker and seg.start <= (prev.end + max_gap_seconds):
                new_end = max(prev.end, seg.end)
                merged_duration = max(0.0, new_end - prev.start)
                prev_conf = prev.confidence if prev.confidence is not None else 0.5
                seg_conf = seg.confidence if seg.confidence is not None else 0.5
                prev_weight = max(prev.duration, 1e-6)
                seg_weight = max(seg.duration, 1e-6)
                weighted_conf = ((prev_conf * prev_weight) + (seg_conf * seg_weight)) / (prev_weight + seg_weight)
                prev.end = new_end
                prev.duration = merged_duration
                prev.confidence = weighted_conf
                continue
            merged.append(seg)
        return merged

    @staticmethod
    def _dominant_speaker(segments: list[DiarizedSegment]) -> tuple[str | None, float | None]:
        if not segments:
            return None, None
        durations: dict[str, float] = {}
        weighted_confidences: dict[str, float] = {}
        for seg in segments:
            duration = max(0.0, float(seg.duration))
            conf = float(seg.confidence) if seg.confidence is not None else 0.5
            durations[seg.speaker] = durations.get(seg.speaker, 0.0) + duration
            weighted_confidences[seg.speaker] = weighted_confidences.get(seg.speaker, 0.0) + (conf * duration)
        winner = max(durations.keys(), key=lambda key: durations[key])
        total_duration = max(1e-6, durations[winner])
        confidence = weighted_confidences[winner] / total_duration
        return winner, confidence

    def _prune_locked(self, now: datetime) -> None:
        if not self._states:
            return
        cutoff = now - timedelta(seconds=max(60, int(self._settings.stale_source_seconds)))
        stale_keys = [key for key, state in self._states.items() if state.last_seen < cutoff]
        for key in stale_keys:
            self._states.pop(key, None)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="speaker-service", version="0.1.0")
    engine = ClusteringSpeakerEngine(settings)

    @app.get("/health")
    def health() -> dict[str, object]:
        return engine.status()

    @app.post("/v1/diarize/chunk", response_model=DiarizeChunkResponse)
    def diarize_chunk(req: DiarizeChunkRequest) -> DiarizeChunkResponse:
        try:
            return engine.diarize_chunk(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app
