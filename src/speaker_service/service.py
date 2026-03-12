from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock

from fastapi import FastAPI, HTTPException, Response, status
import numpy as np

from audio_assist.enroll import ProfileStore, SpeakerProfile, StoredSpeakerProfile
from speaker_service.config import Settings
from speaker_service.embeddings import (
    LightweightSpeakerEmbeddingExtractor,
    decode_pcm_s16le_base64,
)
from speaker_service.schemas import (
    DiarizeChunkRequest,
    DiarizeChunkResponse,
    DiarizedSegment,
    EnrollRequest,
    SpeakerProfileResponse,
    VerifyMatch,
    VerifyRequest,
    VerifyResponse,
)


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


class SpeakerEngine:
    """Named speaker matching plus lightweight per-source clustering."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = Lock()
        self._states: dict[str, SourceState] = {}
        self._extractor = LightweightSpeakerEmbeddingExtractor(
            min_voice_dbfs=settings.min_voice_dbfs,
        )
        self._profile_store = ProfileStore(settings.profiles_db_path)

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
            "profiles": len(self._profile_store.list_all()),
            "window_seconds": self._settings.window_seconds,
            "similarity_threshold": self._settings.similarity_threshold,
            "profile_similarity_threshold": self._settings.profile_similarity_threshold,
            "verification_threshold": self._settings.verification_threshold,
        }

    def diarize_chunk(self, req: DiarizeChunkRequest) -> DiarizeChunkResponse:
        samples = self._decode_audio(req.pcm_s16le_base64, req.channels)
        if samples.size == 0:
            return DiarizeChunkResponse()

        windows = self._windows(samples.size, req.sample_rate)
        profiles = self._profile_store.list_all_with_embeddings()
        segments: list[DiarizedSegment] = []
        for start_idx, end_idx in windows:
            window_audio = samples[start_idx:end_idx]
            embedding = self._extract_embedding(window_audio, req.sample_rate)
            if embedding is None:
                continue
            speaker, confidence = self._assign_label(
                source_id=req.source_id,
                session_id=req.session_id,
                embedding=embedding,
                profiles=profiles,
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

    def enroll(self, req: EnrollRequest) -> tuple[SpeakerProfileResponse, bool]:
        name = str(req.name or "").strip()
        if not name:
            raise ValueError("Speaker name is required.")
        samples = self._decode_audio(req.pcm_s16le_base64, req.channels)
        min_samples = int(req.sample_rate * self._settings.enrollment_min_voice_seconds)
        if samples.size < min_samples:
            raise ValueError(
                f"Enrollment sample must be at least {self._settings.enrollment_min_voice_seconds:.1f}s."
            )
        embedding = self._extract_embedding(samples, req.sample_rate)
        if embedding is None:
            raise ValueError("Enrollment sample does not contain enough voiced audio.")
        existing = self._profile_store.get_by_name(name) is not None
        profile = self._profile_store.upsert(name, embedding, is_owner=req.is_owner)
        return self._profile_response(profile), existing

    def list_profiles(self) -> list[SpeakerProfileResponse]:
        return [self._profile_response(item) for item in self._profile_store.list_all()]

    def delete_profile(self, profile_id: int) -> bool:
        return self._profile_store.delete(profile_id)

    def verify(self, req: VerifyRequest) -> VerifyResponse:
        samples = self._decode_audio(req.pcm_s16le_base64, req.channels)
        embedding = self._extract_embedding(samples, req.sample_rate)
        if embedding is None:
            raise ValueError("Verification sample does not contain enough voiced audio.")

        if req.profile_id is not None:
            profiles = [
                item for item in self._profile_store.list_all_with_embeddings()
                if item.profile.id == req.profile_id
            ]
            if not profiles:
                raise KeyError(req.profile_id)
        else:
            profiles = self._profile_store.list_all_with_embeddings()

        matches: list[VerifyMatch] = []
        threshold = float(self._settings.verification_threshold)
        for item in profiles:
            similarity = self._cosine_similarity(item.embedding, embedding)
            matches.append(
                VerifyMatch(
                    profile_id=item.profile.id,
                    name=item.profile.name,
                    similarity=similarity,
                    is_match=similarity >= threshold,
                )
            )
        matches.sort(key=lambda item: item.similarity, reverse=True)
        return VerifyResponse(matches=matches, threshold=threshold)

    def _assign_label(
        self,
        source_id: str,
        session_id: str | None,
        embedding: np.ndarray,
        profiles: list[StoredSpeakerProfile],
    ) -> tuple[str, float]:
        profile_match = self._match_profile(embedding, profiles)
        if profile_match is not None:
            return profile_match
        return self._assign_cluster(source_id=source_id, session_id=session_id, embedding=embedding)

    def _match_profile(
        self,
        embedding: np.ndarray,
        profiles: list[StoredSpeakerProfile],
    ) -> tuple[str, float] | None:
        if not profiles:
            return None
        best_profile: StoredSpeakerProfile | None = None
        best_similarity = -1.0
        for item in profiles:
            similarity = self._cosine_similarity(item.embedding, embedding)
            if similarity > best_similarity:
                best_similarity = similarity
                best_profile = item
        if (
            best_profile is None
            or best_similarity < float(self._settings.profile_similarity_threshold)
        ):
            return None
        return best_profile.profile.name, best_similarity

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
                similarity = self._cosine_similarity(cluster.centroid, embedding)
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
            return best_cluster.label, best_similarity

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
        return label, max(0.55, float(self._settings.create_threshold))

    @staticmethod
    def _profile_response(profile: SpeakerProfile) -> SpeakerProfileResponse:
        return SpeakerProfileResponse(
            id=profile.id,
            name=profile.name,
            embedding_dim=profile.embedding_dim,
            enrolled_at=profile.enrolled_at,
            sample_count=profile.sample_count,
            is_owner=profile.is_owner,
        )

    def _decode_audio(self, payload: str, channels: int) -> np.ndarray:
        return decode_pcm_s16le_base64(payload, channels=channels)

    def _extract_embedding(self, samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
        embedding = self._extractor.extract(samples, sample_rate)
        if embedding is None:
            return None
        return self._normalize_vector(embedding)

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
    def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
        similarity = float(np.dot(left, right))
        return max(0.0, min(1.0, similarity))

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
    engine = SpeakerEngine(settings)

    @app.get("/health")
    def health() -> dict[str, object]:
        return engine.status()

    @app.post("/v1/diarize/chunk", response_model=DiarizeChunkResponse)
    def diarize_chunk(req: DiarizeChunkRequest) -> DiarizeChunkResponse:
        try:
            return engine.diarize_chunk(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/v1/enroll",
        response_model=SpeakerProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def enroll(req: EnrollRequest, response: Response) -> SpeakerProfileResponse:
        try:
            profile, existed = engine.enroll(req)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if existed:
            response.status_code = status.HTTP_200_OK
        return profile

    @app.get("/v1/profiles", response_model=list[SpeakerProfileResponse])
    def list_profiles() -> list[SpeakerProfileResponse]:
        return engine.list_profiles()

    @app.delete("/v1/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_profile(profile_id: int) -> Response:
        if not engine.delete_profile(profile_id):
            raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/verify", response_model=VerifyResponse)
    def verify(req: VerifyRequest) -> VerifyResponse:
        try:
            return engine.verify(req)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Profile {exc.args[0]} not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app
