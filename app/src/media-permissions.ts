import { invoke } from "@tauri-apps/api/core";

export interface MediaPermissionStatus {
  service: "microphone";
  status: "authorized" | "denied" | "restricted" | "not-determined" | "unsupported";
  granted: boolean;
  canPrompt: boolean;
}

let microphonePermissionGranted = false;

function mediaDevicesAvailable(): boolean {
  return typeof navigator !== "undefined"
    && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === "function";
}

function messageForPermissionStatus(status: MediaPermissionStatus): string {
  if (status.status === "denied") {
    return "Microphone access was denied. Enable Personal Assistant in System Settings > Privacy & Security > Microphone and try again.";
  }
  if (status.status === "restricted") {
    return "Microphone access is restricted by macOS and cannot be granted from this app.";
  }
  if (status.status === "unsupported") {
    return "This build cannot request microphone access natively.";
  }
  return "Microphone access could not be granted.";
}

function normalizeBrowserPermissionError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Microphone access was denied. Allow microphone access for Personal Assistant and try again.";
    }
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
      return "No microphone input device is available.";
    }
    if (error.name === "NotReadableError" || error.name === "TrackStartError") {
      return "Microphone access is blocked by another app or the device could not be opened.";
    }
    return error.message || error.name;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Microphone access could not be requested.";
}

export async function getMicrophonePermissionStatus(): Promise<MediaPermissionStatus | null> {
  try {
    const status = await invoke<MediaPermissionStatus>("get_microphone_permission_status");
    if (status.granted) {
      microphonePermissionGranted = true;
    }
    return status;
  } catch {
    return null;
  }
}

async function ensureBrowserMicrophonePermission(): Promise<void> {
  if (!mediaDevicesAvailable()) {
    throw new Error("This app runtime does not expose navigator.mediaDevices.getUserMedia().");
  }

  let stream: MediaStream | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
      video: false,
    });
    microphonePermissionGranted = true;
  } catch (error) {
    throw new Error(normalizeBrowserPermissionError(error));
  } finally {
    stream?.getTracks().forEach((track) => track.stop());
  }
}

export async function ensureMicrophonePermission(): Promise<void> {
  if (microphonePermissionGranted) {
    return;
  }

  try {
    const status = await invoke<MediaPermissionStatus>("request_microphone_permission");
    if (status.granted) {
      microphonePermissionGranted = true;
      return;
    }
    throw new Error(messageForPermissionStatus(status));
  } catch (error) {
    if (mediaDevicesAvailable()) {
      await ensureBrowserMicrophonePermission();
      return;
    }
    if (error instanceof Error) {
      throw error;
    }
    throw new Error(String(error));
  }
}

// --- Screen Recording Permission ---

export interface ScreenRecordingPermissionStatus {
  available: boolean;
  granted: boolean;
  error: string | null;
}

export async function checkScreenRecordingPermission(): Promise<ScreenRecordingPermissionStatus> {
  try {
    return await invoke<ScreenRecordingPermissionStatus>("get_screen_recording_permission_status");
  } catch {
    return { available: false, granted: false, error: "failed to check permission" };
  }
}

export async function primeMicrophonePermission(): Promise<void> {
  const status = await getMicrophonePermissionStatus();
  if (!status || status.granted || !status.canPrompt) {
    return;
  }

  try {
    const requested = await invoke<MediaPermissionStatus>("request_microphone_permission");
    if (requested.granted) {
      microphonePermissionGranted = true;
      return;
    }
  } catch {
    // The explicit recorder action still surfaces errors if startup prompting fails.
  }
}
