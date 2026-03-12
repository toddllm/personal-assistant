import { invoke } from "@tauri-apps/api/core";
import { fetchCaptureReadiness, fetchRecentTranscripts, type CaptureReadiness, type TranscriptItem } from "./api";

let activeLogService: string | null = null;
let activeLogFile: string | null = null;
let logInterval: ReturnType<typeof setInterval> | null = null;
const AUDIO_ASSIST_SERVICE_ID = "audio-assist";

export function showLogPanel(serviceId: string, logFile: string) {
  const panel = document.getElementById("log-panel")!;
  const title = document.getElementById("log-title")!;
  const content = document.getElementById("log-content")!;

  activeLogService = serviceId;
  activeLogFile = logFile;
  title.textContent = serviceId === AUDIO_ASSIST_SERVICE_ID
    ? `Logs + Raw Transcript: ${serviceId}`
    : `Logs: ${serviceId}`;
  panel.classList.add("open");

  // Clear and fetch immediately
  content.textContent = "Loading...";
  void fetchLogs(logFile, content);
  startLogPolling(logFile, content);
}

export function hideLogPanel() {
  const panel = document.getElementById("log-panel")!;
  panel.classList.remove("open");
  activeLogService = null;
  activeLogFile = null;
  stopLogPolling();
}

export function getActiveLogService(): string | null {
  return activeLogService;
}

export function pauseLogPanelPolling() {
  stopLogPolling();
}

export function resumeLogPanelPolling() {
  if (!activeLogService || !activeLogFile) {
    return;
  }

  const panel = document.getElementById("log-panel");
  const content = document.getElementById("log-content");
  if (!panel || !content || !panel.classList.contains("open")) {
    return;
  }

  content.textContent = "Loading...";
  void fetchLogs(activeLogFile, content);
  startLogPolling(activeLogFile, content);
}

function startLogPolling(logFile: string, content: HTMLElement) {
  stopLogPolling();
  logInterval = setInterval(() => {
    void fetchLogs(logFile, content);
  }, 3000);
}

function stopLogPolling() {
  if (!logInterval) {
    return;
  }
  clearInterval(logInterval);
  logInterval = null;
}

async function fetchLogs(logFile: string, el: HTMLElement) {
  try {
    const logPromise = invoke<string>("read_log_tail", {
      logFile,
      lines: 80,
    });
    const audioAssistView = activeLogService === AUDIO_ASSIST_SERVICE_ID;
    const transcriptPromise = audioAssistView
      ? fetchRecentTranscripts(300, 24)
      : Promise.resolve<TranscriptItem[]>([]);
    const readinessPromise = audioAssistView
      ? fetchCaptureReadiness()
      : Promise.resolve<CaptureReadiness | null>(null);

    const [logText, transcripts, readiness] = await Promise.all([logPromise, transcriptPromise, readinessPromise]);
    el.textContent = audioAssistView
      ? buildAudioAssistLogView(logText, transcripts, readiness)
      : logText;
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = `Error reading logs: ${e}`;
  }
}

function buildAudioAssistLogView(
  logText: string,
  transcripts: TranscriptItem[],
  readiness: CaptureReadiness | null,
): string {
  const transcriptSection = formatTranscriptSection(transcripts);
  const captureSection = formatCaptureSnapshot(readiness, transcripts.length);
  return [
    "=== Service Log Tail ===",
    logText.trimEnd(),
    "",
    "=== Raw Transcript (Recent) ===",
    transcriptSection,
    "",
    "=== Capture Snapshot ===",
    captureSection,
  ].join("\n");
}

function formatTranscriptSection(transcripts: TranscriptItem[]): string {
  if (transcripts.length === 0) {
    return "No recent transcript rows.";
  }

  const chronological = [...transcripts].reverse();
  return chronological
    .map((item) => {
      const timestamp = new Date(item.started_at).toLocaleTimeString();
      const speaker = item.speaker ? ` (${item.speaker})` : "";
      return `[${timestamp}] [${item.source_id}]${speaker} ${item.text}`;
    })
    .join("\n");
}

function formatCaptureSnapshot(readiness: CaptureReadiness | null, transcriptCount: number): string {
  if (!readiness) {
    return "Capture readiness unavailable.";
  }

  const lines = [readiness.summary];
  const levels = readiness.signals?.levels ?? [];
  const transcriber = readiness.signals?.transcriber;

  if (transcriptCount === 0 && levels.length > 0) {
    const levelSummary = levels
      .filter((level) => level.level_dbfs !== null)
      .map((level) => {
        const state = level.silent ? "silent" : "active";
        return `${level.source_id}: ${level.level_dbfs?.toFixed(0)} dBFS (${state})`;
      });
    if (levelSummary.length > 0) {
      lines.push(`Levels: ${levelSummary.join(" | ")}`);
    }
  }

  if (transcriber) {
    lines.push(
      `Transcriber: processed=${transcriber.processed ?? 0} text=${transcriber.with_text ?? 0} empty=${transcriber.empty_text ?? 0} queue=${transcriber.queue_size ?? 0}/${transcriber.queue_capacity ?? 0}`,
    );
  }

  if (transcriptCount === 0) {
    lines.push("No recent transcript rows have been written yet.");
  }

  return lines.join("\n");
}
