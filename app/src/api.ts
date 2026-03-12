import { invoke } from "@tauri-apps/api/core";

// --- Types ---

export interface VolumeDevice {
  slug: string;
  display: string;
  icon: string;
  volume: number;
  muted: boolean;
  error?: string | null;
}

export interface SpeakerSetting {
  enabled?: boolean;
  volume?: number;
  pre_mute_volume?: number;
}

export interface SourceLevel {
  source_id: string;
  level_dbfs: number;
  peak_dbfs: number;
  age_seconds: number;
  silent: boolean;
  clipped: boolean;
}

export interface CaptureReadiness {
  status: "ready" | "degraded" | "down";
  summary: string;
  issues: { code: string; severity: string; message: string }[];
  recommendations?: string[];
  signals?: {
    queue_size?: number;
    queue_capacity?: number;
    queue_ratio?: number;
    processed?: number;
    with_text?: number;
    empty_text?: number;
    transcriber?: {
      queue_size?: number;
      queue_capacity?: number;
      queue_ratio?: number;
      processed?: number;
      with_text?: number;
      empty_text?: number;
    };
    levels?: Array<{
      source_id: string;
      level_dbfs: number | null;
      peak_dbfs: number | null;
      age_seconds: number | null;
      silent: boolean | null;
      clipped: boolean | null;
      updated_at: string | null;
    }>;
  };
}

export interface SourceStatus {
  source_id: string;
  source_type: string;
  source_role: string;
  running: boolean;
  started_at: string | null;
  details: Record<string, string>;
}

export interface TranscriptItem {
  id: number;
  source_id: string;
  session_id: string | null;
  started_at: string;
  ended_at: string;
  text: string;
  speaker: string | null;
}

// --- Helpers ---

async function fetchLocal(url: string, method?: string, body?: string): Promise<string> {
  return invoke<string>("fetch_local_api", { url, method: method ?? null, body: body ?? null });
}

async function getJson<T>(url: string): Promise<T> {
  const text = await fetchLocal(url);
  return JSON.parse(text) as T;
}

// --- Volume Control (port 8788) ---

const VOL_BASE = "http://127.0.0.1:8788";

export async function fetchVolumeDevices(): Promise<Record<string, VolumeDevice>> {
  return getJson<Record<string, VolumeDevice>>(`${VOL_BASE}/api/devices`);
}

export async function fetchSpeakerSettings(): Promise<Record<string, SpeakerSetting>> {
  return getJson<Record<string, SpeakerSetting>>(`${VOL_BASE}/api/speaker-settings`);
}

export async function setVolume(slug: string, volume: number): Promise<void> {
  await fetchLocal(
    `${VOL_BASE}/api/volume/${slug}`,
    "POST",
    JSON.stringify({ volume }),
  );
}

export interface AudioRouteStatus {
  currentOutput: string | null;
  currentSystemOutput: string | null;
  availableOutputs: string[];
  recommendedOutput: string;
}

export async function fetchAudioRouteStatus(): Promise<AudioRouteStatus> {
  return invoke<AudioRouteStatus>("get_audio_route_status");
}

export async function restoreCaptureAudioDefaults(): Promise<AudioRouteStatus> {
  return invoke<AudioRouteStatus>("restore_capture_audio_defaults");
}

// --- Mic Devices (port 8788 — digital gain) ---

export interface MicDevice {
  slug: string;
  display: string;
  icon: string;
  volume: number;    // 0-500 (digital gain, >100 = boost)
  enabled: boolean;
}

export interface MicLevel {
  level_dbfs: number;
  peak_dbfs: number;
  volume: number;
  enabled: boolean;
}

export async function fetchMicDevices(): Promise<Record<string, MicDevice>> {
  return getJson<Record<string, MicDevice>>(`${VOL_BASE}/api/mics`);
}

export async function fetchMicLevels(): Promise<Record<string, MicLevel>> {
  return getJson<Record<string, MicLevel>>(`${VOL_BASE}/api/mic-levels`);
}

export async function setMicVolume(slug: string, volume: number): Promise<void> {
  await fetchLocal(`${VOL_BASE}/api/mic/${slug}`, "POST", JSON.stringify({ volume }));
}

export async function startMicTest(seconds = 3): Promise<{ ok: boolean; seconds: number }> {
  const text = await fetchLocal(`${VOL_BASE}/api/mic-test`, "POST", JSON.stringify({ seconds }));
  return JSON.parse(text);
}

// --- Audio Assist (port 8787) ---

const AUDIO_BASE = "http://127.0.0.1:8787";

export async function fetchSourceLevels(): Promise<SourceLevel[]> {
  return getJson<SourceLevel[]>(`${AUDIO_BASE}/v1/sources/levels`);
}

// --- Capture Readiness (port 8787) ---

export async function fetchCaptureReadiness(): Promise<CaptureReadiness> {
  return getJson<CaptureReadiness>(`${AUDIO_BASE}/v1/capture/readiness`);
}

export async function fetchSources(): Promise<SourceStatus[]> {
  return getJson<SourceStatus[]>(`${AUDIO_BASE}/v1/sources`);
}

export async function ensureCaptureSources(): Promise<Record<string, unknown>> {
  const text = await fetchLocal(`${AUDIO_BASE}/v1/sources/ensure`, "POST", "{}");
  return JSON.parse(text) as Record<string, unknown>;
}

export async function stopCaptureSource(sourceId: string): Promise<{ stopped: boolean }> {
  const text = await fetchLocal(`${AUDIO_BASE}/v1/sources/stop/${encodeURIComponent(sourceId)}`, "POST");
  return JSON.parse(text) as { stopped: boolean };
}

export async function resumeCaptureSource(sourceId: string): Promise<SourceStatus> {
  const text = await fetchLocal(`${AUDIO_BASE}/v1/sources/resume/${encodeURIComponent(sourceId)}`, "POST");
  return JSON.parse(text) as SourceStatus;
}

export async function fetchRecentTranscripts(
  sinceSeconds = 180,
  limit = 40,
): Promise<TranscriptItem[]> {
  const params = new URLSearchParams({
    since_seconds: String(sinceSeconds),
    limit: String(limit),
    sessionized: "false",
  });
  return getJson<TranscriptItem[]>(`${AUDIO_BASE}/v1/transcripts/recent?${params.toString()}`);
}

// --- Voice Chat (port 8797) ---

const VC_BASE = "http://127.0.0.1:8797";

export interface VoiceChatDevice {
  index: number;
  name: string;
  channels_in: number;
  channels_out: number;
  sample_rate: number;
}

export interface VoiceChatDevices {
  inputs: VoiceChatDevice[];
  outputs: VoiceChatDevice[];
}

export interface VoiceChatStatus {
  running: boolean;
  state: string;
  mic_device: string;
  speaker_device: string;
  stt_model: string;
  llm_model: string;
  tts_voice: string;
  conversation_length: number;
}

export interface VoiceChatMessage {
  role: string;
  text: string;
  timestamp: number;
}

export async function fetchVoiceChatDevices(): Promise<VoiceChatDevices> {
  return getJson<VoiceChatDevices>(`${VC_BASE}/api/devices`);
}

export async function fetchVoiceChatStatus(): Promise<VoiceChatStatus> {
  return getJson<VoiceChatStatus>(`${VC_BASE}/api/status`);
}

export async function fetchVoiceChatConversation(): Promise<VoiceChatMessage[]> {
  return getJson<VoiceChatMessage[]>(`${VC_BASE}/api/conversation`);
}

export async function startVoiceChat(micDevice?: string, speakerDevice?: string): Promise<{ ok: boolean; error?: string }> {
  const body: Record<string, string> = {};
  if (micDevice) body.mic_device = micDevice;
  if (speakerDevice) body.speaker_device = speakerDevice;
  const text = await fetchLocal(`${VC_BASE}/api/start`, "POST", JSON.stringify(body));
  return JSON.parse(text);
}

export async function stopVoiceChat(): Promise<{ ok: boolean; error?: string }> {
  const text = await fetchLocal(`${VC_BASE}/api/stop`, "POST", "{}");
  return JSON.parse(text);
}

export async function updateVoiceChatConfig(config: Record<string, string>): Promise<any> {
  const text = await fetchLocal(`${VC_BASE}/api/config`, "POST", JSON.stringify(config));
  return JSON.parse(text);
}

// --- Voice Profiles (via voice-chat proxy, port 8797) ---

export interface VoiceProfile {
  name: string;
  display_name: string;
  voice_type: string;
  is_owner: boolean;
  has_photo: boolean;
  created_at: string | null;
}

export async function fetchVoiceProfiles(): Promise<VoiceProfile[]> {
  const data = await getJson<{ profiles: VoiceProfile[] }>(`${VC_BASE}/api/tts/profiles`);
  return data.profiles;
}

export async function cloneVoice(
  name: string,
  displayName: string,
  refText: string,
  audioBase64: string,
  isOwner: boolean = false,
): Promise<void> {
  await fetchLocal(
    `${VC_BASE}/api/tts/profiles/clone`,
    "POST",
    JSON.stringify({
      name,
      display_name: displayName,
      ref_text: refText,
      ref_audio_base64: audioBase64,
      is_owner: isOwner,
    }),
  );
}

export async function deleteVoiceProfile(name: string): Promise<void> {
  await fetchLocal(`${VC_BASE}/api/tts/profiles/${name}`, "DELETE");
}

export async function testVoice(name: string): Promise<string> {
  const text = await fetchLocal(`${VC_BASE}/api/tts/profiles/${name}/test`, "POST", "{}");
  const data = JSON.parse(text);
  return data.audio_base64;
}

export async function uploadProfilePhoto(name: string, photoBase64: string): Promise<void> {
  await fetchLocal(
    `${VC_BASE}/api/tts/profiles/${name}/photo`,
    "POST",
    JSON.stringify({ photo_base64: photoBase64 }),
  );
}

// --- Speaker Profiles (port 8791) ---

const SPEAKER_BASE = "http://127.0.0.1:8791";

export interface SpeakerProfile {
  id: number;
  name: string;
  embedding_dim: number;
  enrolled_at: string;
  sample_count: number;
  is_owner: boolean;
}

export async function fetchSpeakerProfiles(): Promise<SpeakerProfile[]> {
  return getJson<SpeakerProfile[]>(`${SPEAKER_BASE}/v1/profiles`);
}

export async function enrollSpeaker(
  name: string,
  sampleRate: number,
  pcmBase64: string,
  isOwner: boolean = false,
): Promise<SpeakerProfile> {
  const text = await fetchLocal(
    `${SPEAKER_BASE}/v1/enroll`,
    "POST",
    JSON.stringify({
      name,
      sample_rate: sampleRate,
      channels: 1,
      pcm_s16le_base64: pcmBase64,
      is_owner: isOwner,
    }),
  );
  return JSON.parse(text) as SpeakerProfile;
}

export async function deleteSpeakerProfile(profileId: number): Promise<void> {
  await fetchLocal(`${SPEAKER_BASE}/v1/profiles/${profileId}`, "DELETE");
}
