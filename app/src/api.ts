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

export async function setVolume(slug: string, volume: number): Promise<void> {
  await fetchLocal(
    `${VOL_BASE}/api/volume/${slug}`,
    "POST",
    JSON.stringify({ volume }),
  );
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

// --- Audio Source Levels (port 8790) ---

const AUDIO_BASE = "http://127.0.0.1:8790";

export async function fetchSourceLevels(): Promise<SourceLevel[]> {
  return getJson<SourceLevel[]>(`${AUDIO_BASE}/v1/sources/levels`);
}

// --- Capture Readiness (port 8790) ---

export async function fetchCaptureReadiness(): Promise<CaptureReadiness> {
  return getJson<CaptureReadiness>(`${AUDIO_BASE}/v1/capture/readiness`);
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
