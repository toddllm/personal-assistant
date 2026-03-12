import { invoke } from "@tauri-apps/api/core";
import {
  fetchVolumeDevices,
  fetchSpeakerSettings,
  setVolume,
  fetchSourceLevels,
  fetchSources,
  ensureCaptureSources,
  stopCaptureSource,
  resumeCaptureSource,
  fetchRecentTranscripts,
  fetchCaptureReadiness,
  fetchMicDevices,
  fetchMicLevels,
  setMicVolume,
  startMicTest,
  fetchAudioRouteStatus,
  restoreCaptureAudioDefaults,
  type VolumeDevice,
  type SpeakerSetting,
  type SourceLevel,
  type SourceStatus,
  type TranscriptItem,
  type CaptureReadiness,
  type MicDevice,
  type MicLevel,
  type AudioRouteStatus,
} from "./api";
import { checkHealth } from "./health";
import { showLogPanel } from "./logs";
import { ensureMicrophonePermission } from "./media-permissions";
import type { HealthResult, HealthStatus, ServiceControlResult, ServiceDef } from "./services";

let pollTimers: ReturnType<typeof setInterval>[] = [];

// Interaction guard: suppress volume poll while user is dragging or just committed
let volumeLocked = false;
let volumeLockTimer: ReturnType<typeof setTimeout> | null = null;

// Pre-mute volume memory per slug (for unmute restore)
const preMuteVolume = new Map<string, number>();

// Debounce timer for slider API calls
let sliderDebounce: ReturnType<typeof setTimeout> | null = null;

// Mic panel state
let micLocked = false;
let micLockTimer: ReturnType<typeof setTimeout> | null = null;
const preMuteMicVolume = new Map<string, number>();
let micSliderDebounce: ReturnType<typeof setTimeout> | null = null;

const AUDIO_ASSIST_SERVICE_ID = "audio-assist";
const CAPTURE_STATUS_DEFAULT = "Loading recorder status...";
const CAPTURE_OUTPUT_DEVICE = "CaptureAudio 2ch";
const BOSE_OUTPUT_DEVICE = "Bose QC45";
const BOSE_SLUG = "bose-qc45";
let audioAssistService: ServiceDef | null = null;
let audioAssistStatus: HealthStatus = "unknown";
let audioAssistBusy = false;
const captureSourceBusy = new Set<string>();
let ensureCaptureBusy = false;
let audioRouteBusy = false;
let latestVolumeDevices: Record<string, VolumeDevice> = {};
let latestSpeakerSettings: Record<string, SpeakerSetting> = {};
let latestMicDevices: Record<string, MicDevice> = {};
let latestAudioRouteStatus: AudioRouteStatus | null = null;
let audioRouteError: string | null = null;
let lastAudioAssistHealth: HealthResult | null = null;

type AudioRouteAction = "restore-defaults" | "disable-bose-mic";

interface AudioRouteBanner {
  severity: "ok" | "warn" | "error";
  badge: string;
  title: string;
  detail: string;
  action?: AudioRouteAction;
  actionLabel?: string;
}

function lockMicPoll(ms = 3000) {
  micLocked = true;
  if (micLockTimer) clearTimeout(micLockTimer);
  micLockTimer = setTimeout(() => { micLocked = false; }, ms);
}

function lockVolumePoll(ms = 3000) {
  volumeLocked = true;
  if (volumeLockTimer) clearTimeout(volumeLockTimer);
  volumeLockTimer = setTimeout(() => { volumeLocked = false; }, ms);
}

// --- Render ---

export function renderAudioPanel() {
  const container = document.getElementById("audio-panel");
  if (!container) return;

  container.innerHTML = `
    <h2 class="group-heading">Audio</h2>
    <div class="audio-grid-3">
      <div class="audio-card" id="volume-panel">
        <h3 class="audio-card-title">Volume</h3>
        <div id="audio-route-status">
          <span class="audio-unavailable">checking route...</span>
        </div>
        <div id="volume-devices">
          <span class="audio-unavailable">loading...</span>
        </div>
      </div>
      <div class="audio-card" id="mic-panel">
        <h3 class="audio-card-title">Microphones</h3>
        <div id="mic-devices">
          <span class="audio-unavailable">loading...</span>
        </div>
        <div class="mic-test-row">
          <button class="mic-test-btn" id="mic-test-btn">Test Mic</button>
          <span class="mic-test-status" id="mic-test-status"></span>
        </div>
      </div>
      <div class="audio-card" id="sources-panel">
        <h3 class="audio-card-title">Sources &amp; Capture</h3>
        <div class="capture-service-row">
          <div class="capture-service-status">
            <span class="status-dot unknown" id="audio-assist-dot"></span>
            <div class="capture-service-text">
              <div class="capture-service-label">Recorder</div>
              <div class="capture-service-detail" id="audio-assist-detail">${CAPTURE_STATUS_DEFAULT}</div>
            </div>
          </div>
          <div class="capture-service-actions">
            <button class="capture-service-btn" id="audio-assist-toggle">Loading…</button>
            <button class="capture-service-btn secondary" id="audio-assist-logs">Logs</button>
          </div>
        </div>
        <div id="capture-readiness"></div>
        <div class="capture-subsection">
          <div class="capture-subsection-header">
            <span>Capture sources</span>
            <button class="capture-service-btn secondary" id="audio-assist-ensure">Ensure defaults</button>
          </div>
          <div id="capture-sources">
            <span class="audio-unavailable">loading...</span>
          </div>
        </div>
        <div class="capture-subsection">
          <div class="capture-subsection-header">
            <span>Live levels</span>
          </div>
        <div id="source-levels">
          <span class="audio-unavailable">loading...</span>
        </div>
        </div>
      </div>
    </div>
  `;
}

// --- Volume Panel ---

function formatNameList(names: string[]): string {
  if (names.length === 0) return "none";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

function audibleOutputDevices(): VolumeDevice[] {
  return Object.values(latestVolumeDevices).filter((d) => !d.error && !d.muted && d.volume > 0);
}

function speakerRoutingEnabled(slug: string): boolean {
  return latestSpeakerSettings[slug]?.enabled !== false;
}

function computeAudioRouteBanner(): AudioRouteBanner | null {
  if (audioRouteError) {
    return {
      severity: "warn",
      badge: "check",
      title: "Routing status unavailable",
      detail: "The dashboard could not read macOS output selection. Speaker controls still work, but default-output mismatches will not be flagged until this recovers.",
    };
  }

  if (!latestAudioRouteStatus) {
    return null;
  }

  const route = latestAudioRouteStatus;
  const target = route.recommendedOutput || CAPTURE_OUTPUT_DEVICE;
  const currentOutput = route.currentOutput ?? "unknown";
  const currentSystemOutput = route.currentSystemOutput ?? "unknown";
  if (currentOutput !== target || currentSystemOutput !== target) {
    return {
      severity: "error",
      badge: "fix",
      title: "Mac output is bypassing CaptureAudio",
      detail: `Output is ${currentOutput}; system output is ${currentSystemOutput}. Set both back to ${target} so audio-forward can fan out to Bose and speakers.`,
      action: "restore-defaults",
      actionLabel: "Fix defaults",
    };
  }

  const boseMic = latestMicDevices[BOSE_SLUG];
  if (boseMic?.enabled) {
    return {
      severity: "error",
      badge: "risk",
      title: "Bose headset mic is enabled",
      detail: "That can force the headset into the low-quality Bluetooth profile and kill playback. Disable the Bose mic in this panel.",
      action: "disable-bose-mic",
      actionLabel: "Disable Bose mic",
    };
  }

  const audible = audibleOutputDevices();
  const boseExpected = speakerRoutingEnabled(BOSE_SLUG);
  const boseAvailable = route.availableOutputs.includes(BOSE_OUTPUT_DEVICE) || Boolean(latestVolumeDevices[BOSE_SLUG]);
  if (boseExpected && !boseAvailable) {
    if (audible.length === 0) {
      return {
        severity: "error",
        badge: "down",
        title: "No active speaker path",
        detail: "Bose QC45 is missing and no connected speaker currently has audible volume. Reconnect or power-cycle Bose, or raise another output volume.",
      };
    }
    return {
      severity: "warn",
      badge: "bose",
      title: "Bose QC45 is unavailable",
      detail: `Audio is currently only available on ${formatNameList(audible.map((d) => d.display))}. Reconnect or power-cycle Bose if you expected headset audio.`,
    };
  }

  if (audible.length === 0) {
    return {
      severity: "error",
      badge: "mute",
      title: "All connected outputs are muted",
      detail: "CaptureAudio is selected correctly, but every connected speaker is muted or set to zero volume.",
    };
  }

  return {
    severity: "ok",
    badge: "ok",
    title: "Output routing healthy",
    detail: `Defaults point to ${target}. Audible outputs: ${formatNameList(audible.map((d) => d.display))}.`,
  };
}

function renderAudioRouteBanner() {
  const el = document.getElementById("audio-route-status");
  if (!el) return;

  const banner = computeAudioRouteBanner();
  if (!banner) {
    el.innerHTML = `<span class="audio-unavailable">checking route...</span>`;
    return;
  }

  const actionLabel = audioRouteBusy
    ? banner.action === "disable-bose-mic" ? "Disabling…" : "Fixing…"
    : banner.actionLabel;

  el.innerHTML = `
    <div class="audio-route-banner ${banner.severity}">
      <span class="audio-route-badge">${banner.badge}</span>
      <div class="audio-route-body">
        <div class="audio-route-title">${banner.title}</div>
        <div class="audio-route-detail">${banner.detail}</div>
      </div>
      ${banner.action && actionLabel
        ? `<button class="audio-route-action" data-route-action="${banner.action}" ${audioRouteBusy ? "disabled" : ""}>${actionLabel}</button>`
        : ""}
    </div>
  `;
}

// Update existing slider/value DOM in place instead of replacing innerHTML.
// Only do a full rebuild when the set of device slugs changes.
function updateVolumeDevices(devices: Record<string, VolumeDevice>) {
  const el = document.getElementById("volume-devices");
  if (!el) return;

  const slugs = Object.keys(devices);
  if (slugs.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no devices</span>`;
    return;
  }

  // Check if we need a full rebuild (different slugs than what's rendered)
  const existingSlugs = Array.from(el.querySelectorAll(".vol-row"))
    .map((r) => (r as HTMLElement).dataset.slug);
  const needsRebuild = slugs.length !== existingSlugs.length
    || slugs.some((s, i) => s !== existingSlugs[i]);

  if (needsRebuild) {
    el.innerHTML = slugs.map((slug) => buildVolumeRow(slug, devices[slug])).join("");
    return;
  }

  // Incremental update — patch each row in place
  for (const slug of slugs) {
    const d = devices[slug];
    const row = el.querySelector(`.vol-row[data-slug="${slug}"]`) as HTMLElement | null;
    if (!row) continue;

    const disconnected = !!d.error;

    // Handle disconnected ↔ connected transitions with a rebuild
    const wasDisconnected = row.classList.contains("disconnected");
    if (disconnected !== wasDisconnected) {
      row.outerHTML = buildVolumeRow(slug, d);
      continue;
    }

    if (disconnected) continue; // nothing to patch

    // Update slider value (only if user isn't interacting)
    const slider = row.querySelector(".vol-slider") as HTMLInputElement | null;
    if (slider && document.activeElement !== slider) {
      slider.value = String(d.volume);
    }

    // Update displayed number
    const valEl = row.querySelector(".vol-value");
    if (valEl && document.activeElement !== slider) {
      valEl.textContent = String(d.volume);
    }

    // Update mute button label + slider disabled state
    const isMuted = d.muted || d.volume === 0;
    const muteBtn = row.querySelector(".vol-mute") as HTMLButtonElement | null;
    if (muteBtn) muteBtn.textContent = isMuted ? "Unmute" : "Mute";
    if (slider) slider.disabled = isMuted;
  }
}

function buildVolumeRow(slug: string, d: VolumeDevice): string {
  const disconnected = !!d.error;
  const rowClass = disconnected ? "vol-row disconnected" : "vol-row";
  const vol = disconnected ? 0 : d.volume;
  const isMuted = d.muted || d.volume === 0;
  const muteLabel = isMuted ? "Unmute" : "Mute";

  return `
    <div class="${rowClass}" data-slug="${slug}">
      <span class="vol-icon">${d.icon}</span>
      <span class="vol-name">${d.display}</span>
      ${disconnected
        ? `<span class="vol-badge-disc">disconnected</span>`
        : `<input type="range" class="vol-slider" min="0" max="100" value="${vol}"
                 data-slug="${slug}" ${isMuted ? 'disabled' : ''} />
           <span class="vol-value">${vol}</span>
           <button class="vol-mute" data-slug="${slug}">${muteLabel}</button>`
      }
    </div>
  `;
}

// --- Mic Panel ---

function updateMicDevices(devices: Record<string, MicDevice>) {
  const el = document.getElementById("mic-devices");
  if (!el) return;

  const slugs = Object.keys(devices);
  if (slugs.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no mics</span>`;
    return;
  }

  const existingSlugs = Array.from(el.querySelectorAll(".mic-row"))
    .map((r) => (r as HTMLElement).dataset.slug);
  const needsRebuild = slugs.length !== existingSlugs.length
    || slugs.some((s, i) => s !== existingSlugs[i]);

  if (needsRebuild) {
    el.innerHTML = slugs.map((slug) => buildMicRow(slug, devices[slug])).join("");
    return;
  }

  for (const slug of slugs) {
    const d = devices[slug];
    const row = el.querySelector(`.mic-row[data-slug="${slug}"]`) as HTMLElement | null;
    if (!row) continue;

    const slider = row.querySelector(".mic-slider") as HTMLInputElement | null;
    if (slider && document.activeElement !== slider) {
      slider.value = String(d.volume);
    }

    const valEl = row.querySelector(".mic-value");
    if (valEl && document.activeElement !== slider) {
      valEl.textContent = String(d.volume);
      valEl.classList.toggle("mic-boost", d.volume > 100);
    }

    const muteBtn = row.querySelector(".mic-mute") as HTMLButtonElement | null;
    if (muteBtn) muteBtn.textContent = d.enabled ? "Mute" : "Enable";
    if (slider) slider.disabled = !d.enabled;
  }
}

function buildMicRow(slug: string, d: MicDevice): string {
  const muteLabel = d.enabled ? "Mute" : "Enable";
  const boostClass = d.volume > 100 ? " mic-boost" : "";
  return `
    <div class="mic-row" data-slug="${slug}">
      <span class="mic-icon">${d.icon}</span>
      <span class="mic-name">${d.display}</span>
      <input type="range" class="mic-slider" min="0" max="500" value="${d.volume}"
             data-slug="${slug}" ${!d.enabled ? 'disabled' : ''} />
      <span class="mic-value${boostClass}">${d.volume}</span>
      <button class="mic-mute" data-slug="${slug}">${muteLabel}</button>
    </div>
    <div class="mic-level-row" data-slug="${slug}">
      <span class="mic-level-label">Level</span>
      <div class="mic-level-meter">
        <div class="mic-level-bar" data-slug="${slug}" style="width: 0%"></div>
      </div>
      <span class="mic-level-db" data-slug="${slug}">&mdash;</span>
    </div>
  `;
}

// --- Mic Level Meters ---

function updateMicLevels(levels: Record<string, MicLevel>) {
  for (const [slug, lv] of Object.entries(levels)) {
    const bar = document.querySelector(`.mic-level-bar[data-slug="${slug}"]`) as HTMLElement | null;
    const dbEl = document.querySelector(`.mic-level-db[data-slug="${slug}"]`);
    if (!bar || !dbEl) continue;

    if (!lv.enabled) {
      bar.style.width = "0%";
      bar.className = "mic-level-bar silent";
      dbEl.textContent = "off";
      continue;
    }

    // Map dBFS to 0-100%. -60dB = 0%, 0dB = 100%
    const pct = Math.max(0, Math.min(100, ((lv.level_dbfs + 60) / 60) * 100));
    bar.style.width = `${pct}%`;

    // Color: green < -6dB, yellow -6...-3dB, red > -3dB (clipping)
    const stateClass = lv.peak_dbfs > -3 ? "clipping"
      : lv.peak_dbfs > -6 ? "hot"
      : lv.level_dbfs > -100 ? "active"
      : "silent";
    bar.className = `mic-level-bar ${stateClass}`;

    dbEl.textContent = lv.level_dbfs > -100
      ? `${lv.level_dbfs.toFixed(0)} dB`
      : "\u2014";
  }
}

// --- Source Levels ---

function renderSourceLevels(levels: SourceLevel[]) {
  const el = document.getElementById("source-levels");
  if (!el) return;

  if (levels.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no active sources</span>`;
    return;
  }

  el.innerHTML = levels.map((src) => {
    // Map dBFS to a 0-100 meter width. -60 dB = 0%, 0 dB = 100%
    const pct = Math.max(0, Math.min(100, ((src.level_dbfs + 60) / 60) * 100));
    const stateClass = src.clipped ? "clipped" : src.silent ? "silent" : "active";
    const dbLabel = src.level_dbfs > -100 ? `${src.level_dbfs.toFixed(0)} dB` : "\u2014";

    return `
      <div class="source-row">
        <span class="source-name">${src.source_id}</span>
        <span class="source-db">${dbLabel}</span>
        <div class="level-meter">
          <div class="level-bar ${stateClass}" style="width: ${pct}%"></div>
        </div>
      </div>
    `;
  }).join("");
}

function renderCaptureSources(statuses: SourceStatus[], levels: SourceLevel[], transcripts: TranscriptItem[]) {
  const el = document.getElementById("capture-sources");
  if (!el) return;

  if (statuses.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no capture sources</span>`;
    return;
  }

  const levelBySource = new Map(levels.map((item) => [item.source_id, item]));
  const transcriptCounts = new Map<string, number>();
  const lastTranscript = new Map<string, TranscriptItem>();
  for (const item of transcripts) {
    transcriptCounts.set(item.source_id, (transcriptCounts.get(item.source_id) ?? 0) + 1);
    if (!lastTranscript.has(item.source_id)) {
      lastTranscript.set(item.source_id, item);
    }
  }

  el.innerHTML = statuses.map((src) => {
    const level = levelBySource.get(src.source_id);
    const transcriptCount = transcriptCounts.get(src.source_id) ?? 0;
    const latest = lastTranscript.get(src.source_id);
    const suppressed = src.details?.suppressed === "true";
    const levelText = level
      ? `${level.level_dbfs.toFixed(0)} dB`
      : "no level";
    const transcriptText = transcriptCount > 0
      ? `${transcriptCount} transcript chunk${transcriptCount === 1 ? "" : "s"} in recent window`
      : "no recent transcript";
    const latestText = latest
      ? `latest ${new Date(latest.started_at).toLocaleTimeString()}`
      : "no recent rows";
    const runningClass = src.running ? "running" : "stopped";
    const runningLabel = suppressed ? "suppressed" : src.running ? "running" : "stopped";
    const warn = sourceWarning(src);
    const sourceBusy = captureSourceBusy.has(src.source_id);

    return `
      <div class="capture-source-row ${runningClass}">
        <div class="capture-source-main">
          <div class="capture-source-title-row">
            <span class="capture-source-id">${src.source_id}</span>
            <span class="capture-source-badge ${suppressed ? "suppressed" : runningClass}">${runningLabel}</span>
            <span class="capture-source-role">${src.source_role || src.source_type}</span>
          </div>
          <div class="capture-source-detail">${levelText} · ${transcriptText} · ${latestText}</div>
          ${warn ? `<div class="capture-source-warning">${warn}</div>` : ""}
        </div>
        <div class="capture-source-actions">
          ${src.running
            ? `<button class="capture-source-btn" data-source-stop="${src.source_id}" ${sourceBusy ? "disabled" : ""}>${sourceBusy ? "Stopping…" : "Stop"}</button>`
            : `<button class="capture-source-btn secondary" data-source-start="${src.source_id}" ${sourceBusy ? "disabled" : ""}>${sourceBusy ? "Starting…" : (suppressed ? "Start" : "Restart")}</button>`
          }
        </div>
      </div>
    `;
  }).join("");

  const ensureBtn = document.getElementById("audio-assist-ensure") as HTMLButtonElement | null;
  if (ensureBtn) {
    ensureBtn.disabled = ensureCaptureBusy;
    ensureBtn.textContent = ensureCaptureBusy ? "Ensuring…" : "Ensure defaults";
  }
}

function sourceWarning(src: SourceStatus): string {
  const lowered = `${src.source_id} ${src.source_role}`.toLowerCase();
  if (src.details?.suppressed === "true") {
    return "Manually stopped. This source will stay off until you explicitly ensure defaults or restart it.";
  }
  if (lowered.includes("app-audio")) {
    return "Captures app and desktop playback. Stop this source if background media should not be transcribed.";
  }
  if (lowered.includes("system-audio")) {
    return "Captures routed system output. Verify loopback routing if remote audio should be transcribed.";
  }
  return "";
}

// --- Capture Readiness ---

function renderCaptureReadiness(cr: CaptureReadiness) {
  const el = document.getElementById("capture-readiness");
  if (!el) return;

  const badgeClass = cr.status === "ready" ? "badge-ready"
    : cr.status === "degraded" ? "badge-degraded"
    : "badge-down";

  el.innerHTML = `
    <div class="capture-banner">
      <span class="capture-badge ${badgeClass}">${cr.status}</span>
      <span class="capture-summary">${cr.summary}</span>
    </div>
  `;
}

function renderCaptureUnavailable() {
  const el = document.getElementById("capture-readiness");
  if (!el) return;
  el.innerHTML = `
    <div class="capture-banner">
      <span class="capture-badge badge-down">down</span>
      <span class="capture-summary">audio-assist is not reachable.</span>
    </div>
  `;
}

function setAudioAssistStatus(health: HealthResult | null, detailOverride?: string) {
  const dot = document.getElementById("audio-assist-dot");
  const detail = document.getElementById("audio-assist-detail");
  const toggle = document.getElementById("audio-assist-toggle") as HTMLButtonElement | null;
  const logsBtn = document.getElementById("audio-assist-logs") as HTMLButtonElement | null;

  if (health) {
    lastAudioAssistHealth = health;
  }

  if (logsBtn) {
    logsBtn.disabled = !audioAssistService;
  }

  audioAssistStatus = health?.status ?? lastAudioAssistHealth?.status ?? "unknown";
  const detailText = detailOverride ?? health?.detail ?? lastAudioAssistHealth?.detail ?? CAPTURE_STATUS_DEFAULT;

  if (dot) {
    dot.className = `status-dot ${audioAssistStatus}`;
  }

  if (detail) {
    detail.textContent = detailText;
  }

  if (!toggle) return;

  if (!audioAssistService) {
    toggle.textContent = "Unavailable";
    toggle.disabled = true;
    return;
  }

  if (audioAssistBusy) {
    toggle.textContent = "Working…";
    toggle.disabled = true;
    return;
  }

  const running = audioAssistStatus === "green" || audioAssistStatus === "yellow";
  toggle.textContent = running ? "Stop recording" : "Start recording";
  toggle.disabled = false;
}

function nextAudioAssistAction(): "start" | "stop" {
  return audioAssistStatus === "green" || audioAssistStatus === "yellow"
    ? "stop"
    : "start";
}

async function refreshAudioAssistService() {
  if (!audioAssistService) {
    lastAudioAssistHealth = null;
    setAudioAssistStatus(null, "Recorder service definition not loaded.");
    return;
  }

  const health = await checkHealth(audioAssistService);
  setAudioAssistStatus(health);
}

async function toggleAudioAssistService() {
  if (!audioAssistService || audioAssistBusy) return;

  const action = nextAudioAssistAction();
  audioAssistBusy = true;
  let stickyDetail: string | null = null;
  let shouldRefresh = false;
  setAudioAssistStatus(lastAudioAssistHealth, `${action === "start" ? "Starting" : "Stopping"} recorder…`);

  try {
    if (action === "start") {
      setAudioAssistStatus(lastAudioAssistHealth, "Checking microphone access…");
      try {
        await ensureMicrophonePermission();
      } catch (error) {
        stickyDetail = error instanceof Error ? error.message : String(error);
        return;
      }
      setAudioAssistStatus(lastAudioAssistHealth, "Starting recorder…");
    }

    const result = await invoke<ServiceControlResult>("service_control", {
      serviceId: audioAssistService.id,
      script: audioAssistService.script ?? null,
      entrypoint: audioAssistService.entrypoint ?? null,
      action,
    });

    if (!result.success) {
      stickyDetail = result.output?.trim() || `${action} failed`;
      return;
    }

    shouldRefresh = true;
    await new Promise((resolve) => setTimeout(resolve, action === "start" ? 1800 : 800));
    await Promise.allSettled([
      refreshAudioAssistService(),
      refreshLevels(),
      refreshCapture(),
      refreshCaptureSources(),
    ]);
  } finally {
    audioAssistBusy = false;
    if (stickyDetail) {
      setAudioAssistStatus(lastAudioAssistHealth, stickyDetail);
      return;
    }
    if (shouldRefresh) {
      await refreshAudioAssistService();
    } else {
      setAudioAssistStatus(lastAudioAssistHealth);
    }
  }
}

export function bindAudioAssistService(services: ServiceDef[]) {
  audioAssistService = services.find((svc) => svc.id === AUDIO_ASSIST_SERVICE_ID) ?? null;
  void refreshAudioAssistService();
}

async function refreshCaptureSources() {
  try {
    const [statuses, levels, transcripts] = await Promise.all([
      fetchSources(),
      fetchSourceLevels(),
      fetchRecentTranscripts(180, 30),
    ]);
    renderCaptureSources(statuses, levels, transcripts);
  } catch {
    const el = document.getElementById("capture-sources");
    if (el) el.innerHTML = `<span class="audio-unavailable">capture sources unavailable</span>`;
  }
}

async function stopCaptureSourceFromPanel(sourceId: string) {
  if (captureSourceBusy.has(sourceId)) return;
  captureSourceBusy.add(sourceId);
  try {
    await stopCaptureSource(sourceId);
    await Promise.allSettled([
      refreshCaptureSources(),
      refreshLevels(),
      refreshCapture(),
    ]);
  } finally {
    captureSourceBusy.delete(sourceId);
    await refreshCaptureSources();
  }
}

async function resumeCaptureSourceFromPanel(sourceId: string) {
  if (captureSourceBusy.has(sourceId)) return;
  captureSourceBusy.add(sourceId);
  try {
    await resumeCaptureSource(sourceId);
    await Promise.allSettled([
      refreshCaptureSources(),
      refreshLevels(),
      refreshCapture(),
    ]);
  } finally {
    captureSourceBusy.delete(sourceId);
    await refreshCaptureSources();
  }
}

async function ensureDefaultCaptureSources() {
  if (ensureCaptureBusy) return;
  ensureCaptureBusy = true;
  try {
    await ensureCaptureSources();
    await Promise.allSettled([
      refreshCaptureSources(),
      refreshLevels(),
      refreshCapture(),
    ]);
  } finally {
    ensureCaptureBusy = false;
    await refreshCaptureSources();
  }
}

async function refreshAudioRouteStatus() {
  try {
    latestAudioRouteStatus = await fetchAudioRouteStatus();
    audioRouteError = null;
  } catch (err) {
    audioRouteError = err instanceof Error ? err.message : String(err);
  }
  renderAudioRouteBanner();
}

// --- Events ---

export function setupAudioPanelEvents() {
  const container = document.getElementById("audio-panel");
  if (!container) return;

  const recorderToggle = document.getElementById("audio-assist-toggle") as HTMLButtonElement | null;
  recorderToggle?.addEventListener("click", async (event) => {
    event.stopPropagation();
    await toggleAudioAssistService();
  });

  const recorderEnsure = document.getElementById("audio-assist-ensure") as HTMLButtonElement | null;
  recorderEnsure?.addEventListener("click", async (event) => {
    event.stopPropagation();
    await ensureDefaultCaptureSources();
  });

  const recorderLogs = document.getElementById("audio-assist-logs") as HTMLButtonElement | null;
  recorderLogs?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (audioAssistService) {
      showLogPanel(audioAssistService.id, audioAssistService.logFile);
    }
  });

  const micTestBtn = document.getElementById("mic-test-btn") as HTMLButtonElement | null;
  micTestBtn?.addEventListener("click", async (event) => {
    event.stopPropagation();
    const statusEl = document.getElementById("mic-test-status");
    const seconds = 3;
    micTestBtn.disabled = true;
    micTestBtn.textContent = `Recording ${seconds}s...`;
    if (statusEl) statusEl.textContent = "speak now";
    try {
      await startMicTest(seconds);
      await new Promise((resolve) => setTimeout(resolve, seconds * 1000 + 200));
      micTestBtn.textContent = "Playing back...";
      if (statusEl) statusEl.textContent = "listen";
      await new Promise((resolve) => setTimeout(resolve, seconds * 1000 + 500));
    } catch (err) {
      console.error("mic test failed:", err);
      if (statusEl) statusEl.textContent = "error";
    }
    micTestBtn.disabled = false;
    micTestBtn.textContent = "Test Mic";
    if (statusEl) statusEl.textContent = "";
  });

  // Lock poll while the user is dragging a volume slider
  container.addEventListener("mousedown", (e) => {
    const cls = (e.target as HTMLElement).classList;
    if (cls.contains("vol-slider")) lockVolumePoll(5000);
    if (cls.contains("mic-slider")) lockMicPoll(5000);
  });
  container.addEventListener("touchstart", (e) => {
    const cls = (e.target as HTMLElement).classList;
    if (cls.contains("vol-slider")) lockVolumePoll(5000);
    if (cls.contains("mic-slider")) lockMicPoll(5000);
  }, { passive: true });

  // Live update: show value while dragging, debounce API calls
  container.addEventListener("input", (e) => {
    const target = e.target as HTMLInputElement;

    if (target.classList.contains("vol-slider")) {
      lockVolumePoll(3000);
      const row = target.closest(".vol-row");
      const valEl = row?.querySelector(".vol-value");
      if (valEl) valEl.textContent = target.value;
      const slug = target.dataset.slug!;
      const vol = parseInt(target.value, 10);
      if (sliderDebounce) clearTimeout(sliderDebounce);
      sliderDebounce = setTimeout(() => {
        setVolume(slug, vol).catch((err) => console.error("setVolume failed:", err));
      }, 150);
    }

    if (target.classList.contains("mic-slider")) {
      lockMicPoll(3000);
      const row = target.closest(".mic-row");
      const valEl = row?.querySelector(".mic-value");
      if (valEl) valEl.textContent = target.value;
      const slug = target.dataset.slug!;
      const vol = parseInt(target.value, 10);
      if (micSliderDebounce) clearTimeout(micSliderDebounce);
      micSliderDebounce = setTimeout(() => {
        setMicVolume(slug, vol).catch((err) => console.error("setMicVolume failed:", err));
      }, 150);
    }
  });

  // Final commit on slider release
  container.addEventListener("change", async (e) => {
    const target = e.target as HTMLInputElement;

    if (target.classList.contains("vol-slider")) {
      if (sliderDebounce) { clearTimeout(sliderDebounce); sliderDebounce = null; }
      const slug = target.dataset.slug!;
      const vol = parseInt(target.value, 10);
      lockVolumePoll(3000);
      try {
        await setVolume(slug, vol);
      } catch (err) {
        console.error("setVolume failed:", err);
      }
    }

    if (target.classList.contains("mic-slider")) {
      if (micSliderDebounce) { clearTimeout(micSliderDebounce); micSliderDebounce = null; }
      const slug = target.dataset.slug!;
      const vol = parseInt(target.value, 10);
      lockMicPoll(3000);
      try {
        await setMicVolume(slug, vol);
      } catch (err) {
        console.error("setMicVolume failed:", err);
      }
    }
  });

  // Mute toggle: optimistic UI update, then fire API call
  container.addEventListener("click", async (e) => {
    const routeBtn = (e.target as HTMLElement).closest("[data-route-action]") as HTMLButtonElement | null;
    if (routeBtn && !audioRouteBusy) {
      const action = routeBtn.dataset.routeAction as AudioRouteAction | undefined;
      if (!action) return;
      audioRouteBusy = true;
      renderAudioRouteBanner();
      try {
        if (action === "restore-defaults") {
          latestAudioRouteStatus = await restoreCaptureAudioDefaults();
          audioRouteError = null;
        } else if (action === "disable-bose-mic") {
          await setMicVolume(BOSE_SLUG, 0);
        }
        await Promise.allSettled([
          refreshAudioRouteStatus(),
          refreshVolume(),
          refreshMics(),
        ]);
      } catch (err) {
        audioRouteError = err instanceof Error ? err.message : String(err);
      } finally {
        audioRouteBusy = false;
        renderAudioRouteBanner();
      }
      return;
    }

    const stopSourceBtn = (e.target as HTMLElement).closest("[data-source-stop]") as HTMLButtonElement | null;
    if (stopSourceBtn) {
      const sourceId = stopSourceBtn.dataset.sourceStop;
      if (sourceId) {
        await stopCaptureSourceFromPanel(sourceId);
      }
      return;
    }

    const startSourceBtn = (e.target as HTMLElement).closest("[data-source-start]") as HTMLButtonElement | null;
    if (startSourceBtn) {
      const sourceId = startSourceBtn.dataset.sourceStart;
      if (sourceId) {
        await resumeCaptureSourceFromPanel(sourceId);
      }
      return;
    }

    // Volume mute
    const volBtn = (e.target as HTMLElement).closest(".vol-mute") as HTMLButtonElement | null;
    if (volBtn) {
      const slug = volBtn.dataset.slug!;
      const row = volBtn.closest(".vol-row") as HTMLElement | null;
      if (!row) return;

      const slider = row.querySelector(".vol-slider") as HTMLInputElement | null;
      const valEl = row.querySelector(".vol-value");
      const wantUnmute = volBtn.textContent?.trim() === "Unmute";

      lockVolumePoll(5000);

      let targetVol: number;
      if (wantUnmute) {
        targetVol = preMuteVolume.get(slug) ?? 50;
      } else {
        const currentVol = slider ? parseInt(slider.value, 10) : 50;
        if (currentVol > 0) preMuteVolume.set(slug, currentVol);
        targetVol = 0;
      }

      // Optimistic UI update
      if (slider) { slider.value = String(targetVol); slider.disabled = targetVol === 0; }
      if (valEl) valEl.textContent = String(targetVol);
      volBtn.textContent = targetVol === 0 ? "Unmute" : "Mute";

      try {
        await setVolume(slug, targetVol);
      } catch (err) {
        console.warn(`[audio] setVolume(${slug}, ${targetVol}) failed:`, err);
        // Revert optimistic update on failure
        if (valEl) valEl.textContent = "err";
      }
      try {
        await refreshVolume();
      } catch {
        // poll will pick it up later
      }
      return;
    }

    // Mic test
    // Mic mute
    const micBtn = (e.target as HTMLElement).closest(".mic-mute") as HTMLButtonElement | null;
    if (micBtn) {
      const slug = micBtn.dataset.slug!;
      const row = micBtn.closest(".mic-row") as HTMLElement | null;
      if (!row) return;

      const slider = row.querySelector(".mic-slider") as HTMLInputElement | null;
      const valEl = row.querySelector(".mic-value");
      const wantEnable = micBtn.textContent?.trim() === "Enable";

      lockMicPoll(5000);

      let targetVol: number;
      if (wantEnable) {
        targetVol = preMuteMicVolume.get(slug) ?? 100;
      } else {
        const currentVol = slider ? parseInt(slider.value, 10) : 100;
        if (currentVol > 0) preMuteMicVolume.set(slug, currentVol);
        targetVol = 0;
      }

      if (slider) { slider.value = String(targetVol); slider.disabled = targetVol === 0; }
      if (valEl) valEl.textContent = String(targetVol);
      micBtn.textContent = targetVol === 0 ? "Enable" : "Mute";

      try {
        await setMicVolume(slug, targetVol);
      } catch (err) {
        console.error("mic mute toggle failed:", err);
      }
      refreshMics().catch(() => {});
    }
  });
}

// --- Polling ---

async function refreshVolume() {
  try {
    const [devices, speakerSettings] = await Promise.all([
      fetchVolumeDevices(),
      fetchSpeakerSettings(),
    ]);
    latestVolumeDevices = devices;
    latestSpeakerSettings = speakerSettings;
    // Always track latest non-zero volume for unmute restore
    for (const [slug, d] of Object.entries(devices)) {
      if (d.volume > 0) {
        preMuteVolume.set(slug, d.volume);
      }
    }
    updateVolumeDevices(devices);
    renderAudioRouteBanner();
  } catch {
    latestVolumeDevices = {};
    const el = document.getElementById("volume-devices");
    if (el) el.innerHTML = `<span class="audio-unavailable">volume service unavailable</span>`;
    renderAudioRouteBanner();
  }
}

async function refreshMicLevels() {
  try {
    const levels = await fetchMicLevels();
    updateMicLevels(levels);
  } catch {
    // silent — levels are best-effort
  }
}

async function refreshMics() {
  try {
    const devices = await fetchMicDevices();
    latestMicDevices = devices;
    for (const [slug, d] of Object.entries(devices)) {
      if (d.volume > 0) {
        preMuteMicVolume.set(slug, d.volume);
      }
    }
    updateMicDevices(devices);
    renderAudioRouteBanner();
  } catch {
    latestMicDevices = {};
    const el = document.getElementById("mic-devices");
    if (el) el.innerHTML = `<span class="audio-unavailable">mic service unavailable</span>`;
    renderAudioRouteBanner();
  }
}

async function refreshLevels() {
  try {
    const levels = await fetchSourceLevels();
    renderSourceLevels(levels);
  } catch {
    const el = document.getElementById("source-levels");
    if (el) el.innerHTML = `<span class="audio-unavailable">audio service unavailable</span>`;
  }
}

async function refreshCapture() {
  try {
    const cr = await fetchCaptureReadiness();
    renderCaptureReadiness(cr);
  } catch {
    renderCaptureUnavailable();
  }
}

export function pollAudioPanel() {
  if (pollTimers.length > 0) {
    return;
  }

  // Initial fetch
  renderAudioRouteBanner();
  refreshVolume();
  refreshMics();
  refreshAudioRouteStatus();
  refreshMicLevels();
  refreshLevels();
  refreshCapture();
  refreshCaptureSources();

  // Staggered intervals — volume/mic poll skips when locked
  pollTimers.push(setInterval(() => { if (!volumeLocked) refreshVolume(); }, 1000));
  pollTimers.push(setInterval(() => { if (!micLocked) refreshMics(); }, 10000));
  pollTimers.push(setInterval(refreshAudioRouteStatus, 5000));
  pollTimers.push(setInterval(refreshMicLevels, 500));  // fast poll for live meters
  pollTimers.push(setInterval(refreshLevels, 1000));
  pollTimers.push(setInterval(refreshCapture, 15000));
  pollTimers.push(setInterval(refreshCaptureSources, 3000));
  pollTimers.push(setInterval(refreshAudioAssistService, 5000));
}

export function stopAudioPanelPolling() {
  for (const t of pollTimers) clearInterval(t);
  pollTimers = [];
}
