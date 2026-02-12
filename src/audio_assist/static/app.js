const COMMON_SYSTEM_AUDIO_KEYWORDS = [
  "blackhole",
  "loopback",
  "soundflower",
  "vb-audio",
  "virtual",
  "cable",
  "stereo mix",
  "aggregate",
  "multi-output",
  "zoomaudio",
  "teams",
  "krisp",
  "cluely",
];
const OLLAMA_DEFAULT_MODEL_KEY = "audio_assist_default_ollama_model";

const el = {
  sourcePreset: document.getElementById("source-preset"),
  addSource: document.getElementById("add-source"),
  manualSourceType: document.getElementById("manual-source-type"),
  sourceId: document.getElementById("source-id"),
  manualMicFields: document.getElementById("manual-mic-fields"),
  manualFfmpegInputGroup: document.getElementById("manual-ffmpeg-input"),
  manualFfmpegFormatGroup: document.getElementById("manual-ffmpeg-format"),
  ffmpegInput: document.getElementById("ffmpeg-input"),
  ffmpegFormat: document.getElementById("ffmpeg-format"),
  sourceFilter: document.getElementById("source-filter"),
  sinceSeconds: document.getElementById("since-seconds"),
  micDevice: document.getElementById("mic-device"),
  detectDevices: document.getElementById("detect-devices"),
  cycleDevices: document.getElementById("cycle-devices"),
  micScanStatus: document.getElementById("mic-scan-status"),
  micScanList: document.getElementById("mic-scan-list"),
  asrLanguageHint: document.getElementById("asr-language-hint"),
  autoStartSources: document.getElementById("auto-start-sources"),
  showStoppedSources: document.getElementById("show-stopped-sources"),
  liveTranslate: document.getElementById("live-translate"),
  translateSourceLanguage: document.getElementById("translate-source-language"),
  runningSources: document.getElementById("running-sources"),
  verbose: document.getElementById("verbose"),
  controlStatus: document.getElementById("control-status"),
  captureReadiness: document.getElementById("capture-readiness"),
  transcriptMeta: document.getElementById("transcript-meta"),
  transcriptList: document.getElementById("transcript-list"),
  question: document.getElementById("question"),
  ollamaModel: document.getElementById("ollama-model"),
  ollamaStatus: document.getElementById("ollama-status"),
  ttsVoice: document.getElementById("tts-voice"),
  ttsStatus: document.getElementById("tts-status"),
  speakerStatus: document.getElementById("speaker-status"),
  transcriberStatus: document.getElementById("transcriber-status"),
  speakAnswer: document.getElementById("speak-answer"),
  streamVoice: document.getElementById("stream-voice"),
  queryLimit: document.getElementById("query-limit"),
  queryTranslate: document.getElementById("query-translate"),
  answer: document.getElementById("answer"),
  answerProvider: document.getElementById("answer-provider"),
  answerAudio: document.getElementById("answer-audio"),
  evidenceList: document.getElementById("evidence-list"),
  refreshDevices: document.getElementById("refresh-devices"),
  refreshModels: document.getElementById("refresh-models"),
  setDefaultModel: document.getElementById("set-default-model"),
  refreshTtsVoices: document.getElementById("refresh-tts-voices"),
  refreshSources: document.getElementById("refresh-sources"),
  startManualSource: document.getElementById("start-manual-source"),
  applyLanguageHint: document.getElementById("apply-language-hint"),
  stopSource: document.getElementById("stop-source"),
  refreshNow: document.getElementById("refresh-now"),
  askOllama: document.getElementById("ask-ollama"),
};

const state = {
  lastRenderKey: "",
  pollHandle: null,
  sourcePresets: [],
  runningSources: [],
  sourceLevels: {},
  autoStartAttempted: false,
  answerAudioUrl: null,
  streamQueue: [],
  streamPlaying: false,
  pollTicks: 0,
  liveTranslations: {},
  liveTranslationInFlight: false,
  liveTranslationConfigKey: "",
};

window.addEventListener("error", (event) => {
  const message = event && event.message ? String(event.message) : "Unknown frontend error";
  setStatus(`UI error: ${message}`, true);
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event && event.reason ? String(event.reason) : "Unknown async error";
  setStatus(`UI async error: ${reason}`, true);
});

function sourceFilterValue() {
  const value = (el.sourceFilter.value || "").trim();
  return value.length > 0 ? value : null;
}

function sinceSecondsValue() {
  const parsed = Number.parseInt(el.sinceSeconds.value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 3600;
}

function queryLimitValue() {
  const parsed = Number.parseInt(el.queryLimit.value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 8;
}

function isSummaryQuestion(question) {
  const normalized = String(question || "").trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  if (["summary", "summarize", "recap", "tldr", "tl;dr"].includes(normalized)) {
    return true;
  }
  return (
    normalized.includes("summarize") ||
    normalized.includes("summary") ||
    normalized.includes("recap") ||
    normalized.includes("what happened") ||
    normalized.includes("key points") ||
    normalized.includes("takeaways")
  );
}

function selectedOllamaModel() {
  const value = (el.ollamaModel.value || "").trim();
  return value || null;
}

function storedOllamaDefaultModel() {
  try {
    const value = localStorage.getItem(OLLAMA_DEFAULT_MODEL_KEY);
    if (!value) {
      return null;
    }
    const trimmed = String(value).trim();
    return trimmed || null;
  } catch (_) {
    return null;
  }
}

function saveOllamaDefaultModel(modelName) {
  if (!modelName) {
    return;
  }
  try {
    localStorage.setItem(OLLAMA_DEFAULT_MODEL_KEY, modelName);
  } catch (_) {
    // Ignore storage failures. Selection still works for this session.
  }
}

function selectedTtsVoice() {
  const value = (el.ttsVoice.value || "").trim();
  return value || null;
}

function selectedSourcePreset() {
  const value = (el.sourcePreset.value || "").trim();
  return value || null;
}

function selectedManualSourceType() {
  const value = (el.manualSourceType.value || "").trim().toLowerCase();
  return value === "ffmpeg" ? "ffmpeg" : "mic";
}

function selectedAsrLanguageHint() {
  const value = (el.asrLanguageHint && el.asrLanguageHint.value ? el.asrLanguageHint.value : "").trim();
  return value || null;
}

function liveTranslateEnabled() {
  return Boolean(el.liveTranslate && el.liveTranslate.checked);
}

function selectedTranslateSourceLanguage() {
  const value = (el.translateSourceLanguage && el.translateSourceLanguage.value
    ? el.translateSourceLanguage.value
    : ""
  ).trim();
  return value || null;
}

function resetLiveTranslationCache() {
  state.liveTranslations = {};
  state.liveTranslationInFlight = false;
}

function withAsrLanguageHint(payload) {
  const languageHint = selectedAsrLanguageHint();
  if (!languageHint) {
    return { ...payload, language_hint: null };
  }
  return { ...payload, language_hint: languageHint };
}

function escapeHtml(input) {
  return input
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function levelPercent(dbfs) {
  if (!Number.isFinite(dbfs)) {
    return 0;
  }
  const floor = -90;
  const ceiling = 0;
  return ((clamp(dbfs, floor, ceiling) - floor) / (ceiling - floor)) * 100;
}

function formatDb(dbfs) {
  if (!Number.isFinite(dbfs)) {
    return "n/a";
  }
  return `${dbfs.toFixed(1)} dBFS`;
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "n/a";
  }
  if (seconds < 2) {
    return "just now";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s ago`;
  }
  const mins = Math.floor(seconds / 60);
  return `${mins}m ago`;
}

function levelTag(levelDbfs, silent, error) {
  if (error) {
    return "error";
  }
  if (silent) {
    return "silent";
  }
  if (levelDbfs >= -18) {
    return "hot";
  }
  if (levelDbfs >= -40) {
    return "active";
  }
  return "low";
}

function sourceSlugFromName(name) {
  return name
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-+|-+$/g, "")
    .slice(0, 42);
}

function speakerLabel(item) {
  const value = String(item?.speaker || "").trim();
  return value ? value : null;
}

function systemAudioKeywordScore(name) {
  const normalized = String(name || "").toLowerCase();
  let score = 0;
  COMMON_SYSTEM_AUDIO_KEYWORDS.forEach((keyword) => {
    if (normalized.includes(keyword)) {
      score += 1;
    }
  });
  return score;
}

function parseSseBlock(block) {
  const lines = block.split("\n");
  let event = "message";
  const dataLines = [];
  lines.forEach((line) => {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
      return;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  });
  let data = {};
  if (dataLines.length) {
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch {
      data = { raw: dataLines.join("\n") };
    }
  }
  return { event, data };
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) {
        detail = String(payload.detail);
      }
    } catch {
      // best effort
    }
    throw new Error(detail);
  }
  return response.json();
}

async function requestWithTimeout(url, timeoutMs) {
  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await request(url, { signal: controller.signal });
  } finally {
    clearTimeout(timeoutHandle);
  }
}

function setStatus(message, isError = false) {
  el.controlStatus.textContent = message;
  el.controlStatus.classList.toggle("muted", !isError);
  el.controlStatus.style.color = isError ? "#bf2f39" : "";
}

function readinessColor(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ready") {
    return "#17653a";
  }
  if (normalized === "degraded") {
    return "#9a6b00";
  }
  return "#bf2f39";
}

function formatReadinessLabel(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (!normalized) {
    return "UNKNOWN";
  }
  return normalized.toUpperCase();
}

function renderCaptureReadiness(payload) {
  if (!el.captureReadiness) {
    return;
  }
  const label = formatReadinessLabel(payload && payload.status);
  const summary = String(payload && payload.summary ? payload.summary : "No readiness summary.");
  const issues = Array.isArray(payload && payload.issues) ? payload.issues : [];
  const firstIssue = issues.length > 0 ? String(issues[0].message || "").trim() : "";
  const issueSuffix = firstIssue ? ` | ${firstIssue}` : "";
  el.captureReadiness.textContent = `Capture Readiness: ${label} | ${summary}${issueSuffix}`;
  el.captureReadiness.style.color = readinessColor(payload && payload.status);
}

async function refreshCaptureReadiness() {
  if (!el.captureReadiness) {
    return;
  }
  try {
    const payload = await request("/v1/capture/readiness");
    renderCaptureReadiness(payload);
  } catch (error) {
    el.captureReadiness.textContent = `Capture readiness check failed: ${error.message}`;
    el.captureReadiness.style.color = "#bf2f39";
  }
}

function renderTranscripts(items) {
  const verbose = el.verbose.checked;
  const showLiveTranslation = liveTranslateEnabled();
  if (!items.length) {
    el.transcriptList.innerHTML =
      '<div class="entry"><p class="text muted">No transcript entries yet. Add a source and speak/play audio.</p></div>';
    return;
  }
  const html = items
    .map((item) => {
      const started = new Date(item.started_at).toLocaleTimeString();
      const ended = new Date(item.ended_at).toLocaleTimeString();
      const speaker = speakerLabel(item);
      const speakerPart = speaker ? ` | speaker ${speaker}` : "";
      const sessionPart = item.session_id ? ` | session ${item.session_id}` : ` | chunk #${item.id}`;
      const meta = `${item.source_id}${speakerPart} | ${started} - ${ended}${sessionPart}`;
      const text = escapeHtml(item.text);
      const translated = showLiveTranslation ? state.liveTranslations[item.id] : null;
      const translatedBlock = translated
        ? `<p class="text muted"><strong>EN:</strong> ${escapeHtml(translated)}</p>`
        : "";
      if (verbose) {
        return `
          <article class="entry">
            <div class="meta">${escapeHtml(meta)}</div>
            <p class="text">${text}</p>
            ${translatedBlock}
          </article>
        `;
      }
      return `
        <article class="entry">
          <div class="meta">${escapeHtml(
            `${item.source_id}${speaker ? ` | speaker ${speaker}` : ""} | ${started}`
          )}</div>
          <p class="text">${text}</p>
          ${translatedBlock}
        </article>
      `;
    })
    .join("");
  el.transcriptList.innerHTML = html;
}

function renderEvidence(items) {
  if (!items.length) {
    el.evidenceList.innerHTML = '<div class="entry"><p class="text muted">No evidence returned.</p></div>';
    return;
  }
  el.evidenceList.innerHTML = items
    .map((item) => {
      const started = new Date(item.started_at).toLocaleTimeString();
      const speaker = speakerLabel(item);
      return `
      <article class="entry">
        <div class="meta">${escapeHtml(
          `${item.source_id}${speaker ? ` | speaker ${speaker}` : ""} | ${started} | #${item.id}`
        )}</div>
        <p class="text">${escapeHtml(item.text)}</p>
      </article>
    `;
    })
    .join("");
}

function renderRunningSources(items) {
  if (!items.length) {
    el.runningSources.innerHTML =
      '<div class="entry"><p class="text muted">No matching sources. Add a preset source or enable "Show stopped sources".</p></div>';
    return;
  }
  el.runningSources.innerHTML = items
    .map((item) => {
      const started = item.started_at ? new Date(item.started_at).toLocaleTimeString() : "n/a";
      const status = item.running ? "running" : "stopped";
      const sourceId = String(item.source_id || "");
      const sourceType = sourceId.startsWith("system-audio") ? "system-audio" : String(item.source_type || "unknown");
      const hint = item && item.details ? String(item.details.language_hint || "").trim() : "";
      const hintMeta = hint ? ` | asr:${hint}` : "";
      const level = state.sourceLevels[sourceId] || null;
      const hasLevel = Boolean(level);
      const levelDb = hasLevel ? Number(level.level_dbfs) : Number.NaN;
      const peakDb = hasLevel ? Number(level.peak_dbfs) : Number.NaN;
      const ageSeconds = hasLevel ? Number(level.age_seconds) : Number.NaN;
      const meterWidth = hasLevel ? levelPercent(levelDb).toFixed(1) : "0.0";
      const silent = hasLevel ? Boolean(level.silent) : true;
      const clipped = hasLevel ? Boolean(level.clipped) : false;
      const levelClass = clipped ? "clip" : silent ? "silent" : "active";
      const levelSummary = hasLevel
        ? `${formatDb(levelDb)} | peak ${formatDb(peakDb)} | ${formatAge(ageSeconds)}`
        : "Waiting for first audio segment...";
      return `
        <article class="entry source-row">
        <div class="source-main">
          <div class="meta">${escapeHtml(`${sourceId} | ${sourceType} | ${status}${hintMeta}`)}</div>
          <p class="text">Started: ${escapeHtml(started)}</p>
          <div class="level-meta">${escapeHtml(levelSummary)}</div>
          <div class="level-meter" title="${escapeHtml(levelSummary)}">
            <div class="level-fill ${levelClass}" style="width: ${escapeHtml(meterWidth)}%;"></div>
          </div>
          <div class="level-flags">
            ${silent ? '<span class="level-flag">silent</span>' : ""}
            ${clipped ? '<span class="level-flag clip">clipping</span>' : ""}
          </div>
        </div>
        <div class="source-row-actions">
          <button class="ghost-btn stop-source-item" type="button" data-source-id="${escapeHtml(sourceId)}">Stop</button>
        </div>
      </article>
      `;
    })
    .join("");
}

function sortSources(items) {
  return [...items].sort((a, b) => {
    const runningA = a.running ? 1 : 0;
    const runningB = b.running ? 1 : 0;
    if (runningA !== runningB) {
      return runningB - runningA;
    }
    const startedA = a.started_at ? Date.parse(a.started_at) : 0;
    const startedB = b.started_at ? Date.parse(b.started_at) : 0;
    return startedB - startedA;
  });
}

function visibleSourcesForList() {
  const sorted = sortSources(state.runningSources);
  if (el.showStoppedSources.checked) {
    return sorted;
  }
  return sorted.filter((item) => Boolean(item.running));
}

function updateSourceFilterOptions() {
  const currentRaw = String(el.sourceFilter.value || "");
  const sourceIds = sortSources(state.runningSources)
    .map((item) => String(item.source_id || "").trim())
    .filter((value, idx, arr) => value && arr.indexOf(value) === idx);

  const selectedCandidate = currentRaw.trim();
  const options = ['<option value="">All sources</option>'];
  sourceIds.forEach((sourceId) => {
    options.push(`<option value="${escapeHtml(sourceId)}">${escapeHtml(sourceId)}</option>`);
  });

  if (selectedCandidate && !sourceIds.includes(selectedCandidate)) {
    options.push(
      `<option value="${escapeHtml(selectedCandidate)}">${escapeHtml(selectedCandidate)} (custom)</option>`
    );
  }

  el.sourceFilter.innerHTML = options.join("");
  if (selectedCandidate) {
    el.sourceFilter.value = selectedCandidate;
  } else {
    el.sourceFilter.value = "";
  }
}

function renderSourcePanels() {
  updateSourceFilterOptions();
  renderRunningSources(visibleSourcesForList());
}

function createSourcePresets(devices) {
  const presets = [
    {
      key: "desk-mic",
      label: "Desk Mic (Default Input)",
      autoStart: true,
      payload: {
        source_type: "mic",
        source_id: "desk-mic",
        device: null,
      },
    },
  ];

  const virtualDevices = devices.filter((device) => {
    const name = String(device.name || "").toLowerCase();
    return COMMON_SYSTEM_AUDIO_KEYWORDS.some((keyword) => name.includes(keyword));
  });

  virtualDevices.forEach((device, idx) => {
    const deviceName = String(device.name || `device-${device.index}`);
    const sourceId = idx === 0 ? "system-audio" : `system-audio-${sourceSlugFromName(deviceName)}`;
    presets.push({
      key: `virtual-${device.index}`,
      label: `System Audio (${deviceName})`,
      autoStart: idx === 0,
      payload: {
        source_type: "mic",
        source_id: sourceId,
        device: Number(device.index),
        channels: 2,
      },
    });
  });

  if (navigator.platform.toLowerCase().includes("mac")) {
    presets.push({
      key: "ffmpeg-avfoundation",
      label: "System Audio via ffmpeg (avfoundation)",
      autoStart: false,
      payload: {
        source_type: "ffmpeg",
        source_id: "system-audio-ffmpeg",
        ffmpeg_input: ":0",
        ffmpeg_input_format: "avfoundation",
      },
    });
  }

  return presets;
}

function refreshSourcePresetOptions(devices) {
  const current = selectedSourcePreset();
  state.sourcePresets = createSourcePresets(devices);
  const selected =
    state.sourcePresets.find((preset) => preset.key === current)?.key ||
    state.sourcePresets[0]?.key ||
    "";

  el.sourcePreset.innerHTML = state.sourcePresets
    .map((preset) => `<option value="${escapeHtml(preset.key)}">${escapeHtml(preset.label)}</option>`)
    .join("");
  el.sourcePreset.value = selected;
}

function syncManualSourceFields() {
  const manualType = selectedManualSourceType();
  const micMode = manualType === "mic";
  el.manualMicFields.classList.toggle("hidden", !micMode);
  el.manualFfmpegInputGroup.classList.toggle("hidden", micMode);
  el.manualFfmpegFormatGroup.classList.toggle("hidden", micMode);
  if (micMode) {
    el.ffmpegInput.value = "";
    el.ffmpegFormat.value = "";
  }
}

function renderMicScanResults(items) {
  if (!Array.isArray(items) || items.length === 0) {
    el.micScanList.innerHTML =
      '<div class="entry"><p class="text muted">No mic device scan results yet.</p></div>';
    return;
  }
  const html = items
    .map((item) => {
      const name = String(item.name || `device-${item.index}`);
      const levelDb = Number(item.level_dbfs);
      const peakDb = Number(item.peak_dbfs);
      const silent = Boolean(item.silent);
      const err = String(item.error || "");
      const tag = levelTag(levelDb, silent, err);
      const score = Number(item.system_score || 0);
      const isSystemLike = score > 0;
      const label = err
        ? `error: ${err}`
        : `${formatDb(levelDb)} | peak ${formatDb(peakDb)} | ${silent ? "silent" : "active"}${
            isSystemLike ? " | system-like" : ""
          }`;
      return `
      <article class="entry">
        <div class="meta">${escapeHtml(`${item.index}: ${name}`)}</div>
        <p class="text">${escapeHtml(label)}</p>
        <div class="scan-meter">
          <div class="scan-fill ${escapeHtml(tag)}" style="width: ${escapeHtml(
            Number.isFinite(levelDb) ? levelPercent(levelDb).toFixed(1) : "0.0"
          )}%;"></div>
        </div>
        <div class="level-flags">
          ${isSystemLike ? '<span class="level-flag">system-candidate</span>' : ""}
        </div>
      </article>
      `;
    })
    .join("");
  el.micScanList.innerHTML = html;
}

function activeRunningSourceIds() {
  return new Set(
    state.runningSources
      .filter((item) => Boolean(item.running))
      .map((item) => String(item.source_id || ""))
      .filter((item) => item)
  );
}

function findPresetByKey(key) {
  return state.sourcePresets.find((preset) => preset.key === key) || null;
}

async function refreshDevices() {
  try {
    const devices = await request("/v1/devices/mic");
    const options = ['<option value="">Default device</option>'];
    devices.forEach((device) => {
      options.push(
        `<option value="${String(device.index)}">${escapeHtml(
          `${device.index}: ${String(device.name)}`
        )}</option>`
      );
    });
    el.micDevice.innerHTML = options.join("");
    refreshSourcePresetOptions(devices);
    setStatus("Mic devices refreshed.");
    return devices;
  } catch (error) {
    setStatus(`Device listing failed: ${error.message}`, true);
    refreshSourcePresetOptions([]);
    return [];
  }
}

async function detectActiveDevices() {
  el.micScanStatus.textContent = "Scanning input devices for live signal...";
  try {
    const items = await request("/v1/devices/mic/levels?duration_ms=450");
    const list = Array.isArray(items) ? items : [];
    const sortable = list
      .filter((item) => !item.error)
      .map((item) => ({
        ...item,
        level_dbfs: Number(item.level_dbfs),
        system_score: systemAudioKeywordScore(item.name),
      }))
      .sort((a, b) => b.level_dbfs - a.level_dbfs);
    renderMicScanResults(sortable.length ? sortable : list);
    const active = sortable.filter((item) => !item.silent && Number.isFinite(item.level_dbfs));
    if (active.length > 0) {
      const best =
        active.find((item) => Number(item.system_score || 0) > 0) ||
        active[0];
      el.micDevice.value = String(best.index);
      const msg = `Detected active input: ${best.index}: ${best.name} (${formatDb(best.level_dbfs)}).`;
      el.micScanStatus.textContent = msg;
      setStatus(msg);
      return;
    }
    const silentMsg =
      "No active input detected. Route output to a virtual device (for example BlackHole) or choose another mic source.";
    el.micScanStatus.textContent = silentMsg;
    setStatus(silentMsg, true);
  } catch (error) {
    const msg = `Device scan failed: ${error.message}`;
    el.micScanStatus.textContent = msg;
    setStatus(msg, true);
  }
}

async function cycleAndFindDevice() {
  el.micScanStatus.textContent = "Cycling input devices. Keep browser audio playing...";
  let devices = [];
  try {
    devices = await request("/v1/devices/mic");
  } catch (error) {
    const msg = `Device listing failed: ${error.message}`;
    el.micScanStatus.textContent = msg;
    setStatus(msg, true);
    return;
  }
  if (!Array.isArray(devices) || devices.length === 0) {
    const msg = "No input devices found to cycle.";
    el.micScanStatus.textContent = msg;
    setStatus(msg, true);
    return;
  }

  const results = [];
  for (let idx = 0; idx < devices.length; idx += 1) {
    const device = devices[idx];
    const progress = `Cycling ${idx + 1}/${devices.length}: ${device.name}`;
    el.micScanStatus.textContent = progress;
    try {
      const level = await requestWithTimeout(
        `/v1/devices/mic/level/${encodeURIComponent(String(device.index))}?duration_ms=420`,
        9000
      );
      results.push({
        ...level,
        system_score: systemAudioKeywordScore(level.name),
      });
    } catch (error) {
      results.push({
        index: device.index,
        name: device.name,
        level_dbfs: -96.0,
        peak_dbfs: -96.0,
        silent: true,
        system_score: systemAudioKeywordScore(device.name),
        error: String(error.message || error),
      });
    }
  }

  const ranked = [...results]
    .filter((item) => !item.error)
    .sort((a, b) => {
      const activeA = itemActiveScore(a);
      const activeB = itemActiveScore(b);
      if (activeA !== activeB) {
        return activeB - activeA;
      }
      return Number(b.level_dbfs || -96.0) - Number(a.level_dbfs || -96.0);
    });
  renderMicScanResults(ranked.length ? ranked : results);

  const bestSystem =
    ranked.find((item) => Number(item.system_score || 0) > 0 && !item.silent) ||
    null;
  const bestAny = ranked.find((item) => !item.silent) || ranked[0] || null;
  const best = bestSystem || bestAny;
  if (!best) {
    const msg = "Cycle complete, but no usable input was detected.";
    el.micScanStatus.textContent = msg;
    setStatus(msg, true);
    return;
  }
  el.micDevice.value = String(best.index);
  const msg = `Cycle complete. Best candidate: ${best.index}: ${best.name} (${formatDb(
    Number(best.level_dbfs)
  )}).`;
  el.micScanStatus.textContent = msg;
  setStatus(msg, Number(best.level_dbfs) <= -60.0);
}

function itemActiveScore(item) {
  const level = Number(item.level_dbfs || -96.0);
  const systemScore = Number(item.system_score || 0);
  const silent = Boolean(item.silent);
  let score = level;
  score += systemScore * 6;
  if (!silent) {
    score += 20;
  }
  return score;
}

async function refreshRunningSources() {
  try {
    const [items, levelItems] = await Promise.all([
      request("/v1/sources"),
      request("/v1/sources/levels").catch(() => []),
    ]);
    state.runningSources = Array.isArray(items) ? items : [];
    const levelMap = {};
    if (Array.isArray(levelItems)) {
      levelItems.forEach((item) => {
        const sourceId = String(item.source_id || "").trim();
        if (sourceId) {
          levelMap[sourceId] = item;
        }
      });
    }
    state.sourceLevels = levelMap;
    renderSourcePanels();
  } catch (error) {
    el.runningSources.innerHTML = `<div class="entry"><p class="text muted">Source list failed: ${escapeHtml(
      error.message
    )}</p></div>`;
  }
}

async function startSource(payload, quiet = false) {
  try {
    const result = await request("/v1/sources/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!quiet) {
      setStatus(`Started source ${result.source_id}.`);
    }
    await refreshRunningSources();
    return result;
  } catch (error) {
    const msg = String(error.message || "");
    if (msg.includes("already running")) {
      if (!quiet) {
        setStatus(msg);
      }
      await refreshRunningSources();
      return null;
    }
    if (!quiet) {
      setStatus(`Could not start source: ${msg}`, true);
    }
    return null;
  }
}

async function addSourceFromPreset() {
  const preset = findPresetByKey(selectedSourcePreset());
  if (!preset) {
    setStatus("Choose a source preset first.", true);
    return;
  }
  await startSource(withAsrLanguageHint(preset.payload));
}

async function autoStartCommonSources() {
  if (state.autoStartAttempted || !el.autoStartSources.checked) {
    return;
  }
  state.autoStartAttempted = true;

  const runningSet = activeRunningSourceIds();
  const presets = state.sourcePresets.filter((preset) => preset.autoStart);
  let started = 0;
  for (const preset of presets) {
    const sourceId = String(preset.payload.source_id || "");
    if (sourceId && runningSet.has(sourceId)) {
      continue;
    }
    const result = await startSource(withAsrLanguageHint(preset.payload), true);
    if (result) {
      started += 1;
      runningSet.add(String(result.source_id || ""));
    }
  }
  if (started > 0) {
    setStatus(`Auto-started ${started} common source${started === 1 ? "" : "s"}.`);
  }
}

async function refreshOllamaModels() {
  const current = selectedOllamaModel();
  const savedDefault = storedOllamaDefaultModel();
  try {
    const payload = await request("/v1/ollama/models");
    const models = Array.isArray(payload.models) && payload.models.length
      ? payload.models
      : [payload.default_model || "llama3.1:8b"];
    let selected = models[0];
    if (current && models.includes(current)) {
      selected = current;
    } else if (savedDefault && models.includes(savedDefault)) {
      selected = savedDefault;
    } else if (payload.default_model && models.includes(payload.default_model)) {
      selected = payload.default_model;
    }
    el.ollamaModel.innerHTML = models
      .map((model) => `<option value="${escapeHtml(String(model))}">${escapeHtml(String(model))}</option>`)
      .join("");
    el.ollamaModel.value = selected;
    if (payload.reachable === false) {
      el.ollamaStatus.textContent = `Ollama not reachable. Using fallback model list. Saved default: ${savedDefault || "none"}.`;
    } else {
      el.ollamaStatus.textContent = `Loaded ${models.length} local Ollama model(s). Saved default: ${savedDefault || "none"}.`;
    }
  } catch (error) {
    const fallbackModel = savedDefault || "llama3.1:8b";
    el.ollamaModel.innerHTML = `<option value="${escapeHtml(fallbackModel)}">${escapeHtml(fallbackModel)}</option>`;
    el.ollamaModel.value = fallbackModel;
    el.ollamaStatus.textContent = `Model load failed: ${error.message}. Using ${fallbackModel}.`;
  }
}

function setDefaultOllamaModel() {
  const modelName = selectedOllamaModel();
  if (!modelName) {
    setStatus("Select an Ollama model first.", true);
    return;
  }
  saveOllamaDefaultModel(modelName);
  el.ollamaStatus.textContent = `Loaded local Ollama models. Saved default: ${modelName}.`;
  setStatus(`Default model saved: ${modelName}`);
}

function clearStreamQueue() {
  state.streamQueue.forEach((item) => {
    URL.revokeObjectURL(item.url);
  });
  state.streamQueue = [];
  state.streamPlaying = false;
}

function clearAnswerAudio() {
  clearStreamQueue();
  if (state.answerAudioUrl) {
    URL.revokeObjectURL(state.answerAudioUrl);
    state.answerAudioUrl = null;
  }
  el.answerAudio.pause();
  el.answerAudio.removeAttribute("src");
  el.answerAudio.load();
}

function renderAnswerAudio(base64Audio, mimeType) {
  clearAnswerAudio();
  if (!base64Audio) {
    return;
  }
  const raw = atob(base64Audio);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: mimeType || "audio/wav" });
  const url = URL.createObjectURL(blob);
  state.answerAudioUrl = url;
  el.answerAudio.src = url;
}

function enqueueStreamChunk(base64Audio, mimeType) {
  if (!base64Audio) {
    return;
  }
  const raw = atob(base64Audio);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: mimeType || "audio/wav" });
  const url = URL.createObjectURL(blob);
  state.streamQueue.push({ url });
  if (!state.streamPlaying) {
    playNextStreamChunk();
  }
}

function playNextStreamChunk() {
  if (!state.streamQueue.length) {
    state.streamPlaying = false;
    return;
  }
  state.streamPlaying = true;
  const item = state.streamQueue.shift();
  el.answerAudio.src = item.url;
  const onEnded = () => {
    el.answerAudio.removeEventListener("ended", onEnded);
    URL.revokeObjectURL(item.url);
    playNextStreamChunk();
  };
  el.answerAudio.addEventListener("ended", onEnded);
  el.answerAudio.play().catch(() => {
    el.answerAudio.removeEventListener("ended", onEnded);
    state.streamPlaying = false;
    setStatus("Browser blocked autoplay. Press play on the audio control.");
  });
}

async function streamTtsForAnswer(text) {
  const payload = {
    text,
    tts_voice: selectedTtsVoice(),
  };
  const response = await fetch("/v1/tts/synthesize/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let detail = `TTS stream failed (${response.status})`;
    try {
      const data = await response.json();
      if (data && data.detail) {
        detail = String(data.detail);
      }
    } catch {
      // best effort
    }
    throw new Error(detail);
  }
  if (!response.body) {
    throw new Error("TTS stream response body was empty.");
  }

  clearAnswerAudio();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const handleEventBlock = (block) => {
    const parsed = parseSseBlock(block);
    if (parsed.event === "start") {
      const chunks = Number(parsed.data.chunks || 0);
      el.ttsStatus.textContent = `Streaming voice (${chunks} chunk${chunks === 1 ? "" : "s"})...`;
      return;
    }
    if (parsed.event === "chunk") {
      enqueueStreamChunk(parsed.data.audio_base64, parsed.data.mime_type || "audio/wav");
      const idx = Number(parsed.data.index || 0);
      const total = Number(parsed.data.chunks || 0);
      if (idx > 0 && total > 0) {
        el.ttsStatus.textContent = `Streaming voice chunk ${idx}/${total}...`;
      }
      return;
    }
    if (parsed.event === "error") {
      const msg = parsed.data.error || "Unknown TTS stream error.";
      throw new Error(String(msg));
    }
    if (parsed.event === "done") {
      if (parsed.data.ok === false) {
        throw new Error(String(parsed.data.error || "TTS stream ended with error."));
      }
      el.ttsStatus.textContent = "Voice stream complete.";
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);
      if (block) {
        handleEventBlock(block);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) {
    handleEventBlock(buffer.trim());
  }
}

async function refreshTtsVoices() {
  const current = selectedTtsVoice();
  try {
    const payload = await request("/v1/tts/voices");
    const voices = Array.isArray(payload.voices) && payload.voices.length ? payload.voices : ["Vivian"];
    const selected = voices.includes(current) ? current : voices[0];
    el.ttsVoice.innerHTML = voices
      .map((voice) => `<option value="${escapeHtml(String(voice))}">${escapeHtml(String(voice))}</option>`)
      .join("");
    el.ttsVoice.value = selected;
    if (payload.reachable === false) {
      el.ttsStatus.textContent = "TTS service unavailable. Voice responses will gracefully fallback to text.";
    } else {
      el.ttsStatus.textContent = `Loaded ${voices.length} TTS voice option(s).`;
    }
  } catch (error) {
    el.ttsVoice.innerHTML = '<option value="Vivian">Vivian</option>';
    el.ttsStatus.textContent = `TTS voice load failed: ${error.message}`;
  }
}

async function refreshSpeakerStatus() {
  try {
    const payload = await request("/v1/speaker/status");
    const pipeline = payload && typeof payload.pipeline === "object" ? payload.pipeline : {};
    const modeRaw = String(pipeline.mode || "off").toLowerCase();
    const modeLabel = modeRaw === "async" ? "async post-processing" : modeRaw === "inline" ? "inline" : "off";
    const queueSize = Number(pipeline.queue_size || 0);
    const labeled = Number(pipeline.labeled || 0);
    const processed = Number(pipeline.processed || 0);
    const dropped = Number(pipeline.dropped || 0);
    const errors = Number(pipeline.errors || 0);
    const backfillRuns = Number(pipeline.backfill_runs || 0);
    const backfillScanned = Number(pipeline.backfill_scanned || 0);
    const backfillLabeled = Number(pipeline.backfill_labeled || 0);

    if (!payload.enabled) {
      el.speakerStatus.textContent =
        "Speaker detection disabled (enable AUDIO_ASSIST_SPEAKER_ENABLED=true).";
      return;
    }
    if (payload.cooldown_active) {
      el.speakerStatus.textContent =
        `Speaker service unreachable (mode: ${modeLabel}). Capture/transcription continue without speaker labels.`;
      return;
    }
    if (payload.reachable === false) {
      el.speakerStatus.textContent =
        `Speaker service may be unavailable (mode: ${modeLabel}). Capture/transcription continue without speaker labels.`;
      return;
    }
    if (modeRaw === "async") {
      el.speakerStatus.textContent =
        `Speaker detection active (${modeLabel}). Labeled ${labeled}/${processed} chunks, queue ${queueSize}` +
        `, backfill ${backfillLabeled}/${backfillScanned} (${backfillRuns} runs)` +
        `${dropped > 0 ? `, dropped ${dropped}` : ""}` +
        `${errors > 0 ? `, errors ${errors}` : ""}.`;
      return;
    }
    el.speakerStatus.textContent = `Speaker detection active (${modeLabel}).`;
  } catch (error) {
    el.speakerStatus.textContent = `Speaker status check failed: ${error.message}`;
  }
}

async function refreshTranscriberStatus() {
  if (!el.transcriberStatus) {
    return;
  }
  try {
    const payload = await request("/v1/transcriber/status");
    const queueSize = Number(payload.queue_size || 0);
    const queueCapacity = Number(payload.queue_capacity || 0);
    const queueHigh = Number(payload.queue_high_watermark || 0);
    const processed = Number(payload.processed || 0);
    const withText = Number(payload.with_text || 0);
    const emptyText = Number(payload.empty_text || 0);
    const fallbackAttempted = Number(payload.fallback_attempted || 0);
    const fallbackWithText = Number(payload.fallback_with_text || 0);
    const dropped = Number(payload.dropped || 0);
    const archivedOnDrop = Number(payload.archived_on_drop || 0);
    const vadSystem = Boolean(payload.vad_filter_system_audio);
    const msg =
      `Transcription queue ${queueSize}/${queueCapacity} (high ${queueHigh})` +
      ` • text ${withText}/${processed}` +
      ` • empty ${emptyText}` +
      `${fallbackAttempted > 0 ? ` • fallback ${fallbackWithText}/${fallbackAttempted}` : ""}` +
      `${dropped > 0 ? ` • dropped ${dropped} (archived ${archivedOnDrop})` : ""}` +
      ` • system-audio-vad ${vadSystem ? "on" : "off"}`;
    el.transcriberStatus.textContent = msg;
  } catch (error) {
    el.transcriberStatus.textContent = `Transcriber status check failed: ${error.message}`;
  }
}

async function startManualSource() {
  const sourceId = (el.sourceId.value || "").trim();
  if (!sourceId) {
    setStatus("Source ID is required.", true);
    return;
  }
  const sourceType = selectedManualSourceType();
  if (sourceType === "ffmpeg") {
    const ffmpegInput = (el.ffmpegInput.value || "").trim();
    const ffmpegFormat = (el.ffmpegFormat.value || "").trim();
    if (!ffmpegInput) {
      setStatus("ffmpeg input is required for manual ffmpeg sources.", true);
      return;
    }
    await startSource({
      source_type: "ffmpeg",
      source_id: sourceId,
      ffmpeg_input: ffmpegInput,
      ffmpeg_input_format: ffmpegFormat || null,
      language_hint: selectedAsrLanguageHint(),
    });
    return;
  }

  const selectedDevice = (el.micDevice.value || "").trim();
  let device = null;
  if (selectedDevice) {
    const parsed = Number.parseInt(selectedDevice, 10);
    device = Number.isFinite(parsed) ? parsed : selectedDevice;
  }
  await startSource({
    source_type: "mic",
    source_id: sourceId,
    device,
    language_hint: selectedAsrLanguageHint(),
  });
}

async function stopSourceById(sourceId, quiet = false) {
  if (!sourceId) {
    if (!quiet) {
      setStatus("Source ID is required to stop.", true);
    }
    return false;
  }
  try {
    await request(`/v1/sources/stop/${encodeURIComponent(sourceId)}`, { method: "POST" });
    if (!quiet) {
      setStatus(`Stopped source ${sourceId}.`);
    }
    await refreshRunningSources();
    return true;
  } catch (error) {
    if (!quiet) {
      setStatus(`Could not stop source: ${error.message}`, true);
    }
    return false;
  }
}

async function stopSource() {
  const sourceId = sourceFilterValue();
  if (!sourceId) {
    setStatus("Select a source in Transcript View before stopping.", true);
    return;
  }
  await stopSourceById(sourceId);
}

async function applyLanguageHintToSelectedSource() {
  const sourceId = sourceFilterValue();
  if (!sourceId) {
    setStatus("Select a source in Transcript View before applying an ASR hint.", true);
    return;
  }
  const payload = {
    source_id: sourceId,
    language_hint: selectedAsrLanguageHint(),
  };
  try {
    await request("/v1/sources/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setStatus(
      payload.language_hint
        ? `Applied ASR language hint "${payload.language_hint}" to ${sourceId}.`
        : `Cleared ASR language hint for ${sourceId}.`
    );
    await refreshRunningSources();
  } catch (error) {
    setStatus(`Could not apply ASR language hint: ${error.message}`, true);
  }
}

async function ensureLiveTranslations(items) {
  if (!liveTranslateEnabled()) {
    return false;
  }
  if (state.liveTranslationInFlight) {
    return false;
  }
  const sourceLanguage = selectedTranslateSourceLanguage();
  const configKey = `${sourceLanguage || "auto"}`;
  if (configKey !== state.liveTranslationConfigKey) {
    resetLiveTranslationCache();
    state.liveTranslationConfigKey = configKey;
  }
  const missingIds = items
    .map((item) => Number(item.id))
    .filter((id) => Number.isFinite(id) && !(id in state.liveTranslations))
    .slice(0, 24);
  if (!missingIds.length) {
    return false;
  }

  state.liveTranslationInFlight = true;
  try {
    const payload = {
      transcript_ids: missingIds,
      source_language: sourceLanguage,
      target_language: "English",
      model_name: null,
    };
    const result = await request("/v1/transcripts/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let changed = false;
    const translatedItems = Array.isArray(result.items) ? result.items : [];
    translatedItems.forEach((item) => {
      const id = Number(item.id);
      if (!Number.isFinite(id)) {
        return;
      }
      const translated = String(item.translated_text || "").trim();
      if (translated) {
        state.liveTranslations[id] = translated;
        changed = true;
      } else if (!(id in state.liveTranslations)) {
        state.liveTranslations[id] = "";
      }
    });
    return changed;
  } catch (error) {
    setStatus(`Live translation failed: ${error.message}`, true);
    return false;
  } finally {
    state.liveTranslationInFlight = false;
  }
}

async function refreshTranscripts() {
  const params = new URLSearchParams({
    since_seconds: String(sinceSecondsValue()),
    limit: "40",
    sessionized: "false",
  });
  const source = sourceFilterValue();
  if (source) {
    params.set("source_id", source);
  }
  try {
    const items = await request(`/v1/transcripts/recent?${params.toString()}`);
    const translationChanged = await ensureLiveTranslations(items);
    const key = JSON.stringify(items.map((item) => [item.id, item.text, item.speaker || ""]));
    if (key !== state.lastRenderKey || el.verbose.checked || translationChanged) {
      renderTranscripts(items);
      state.lastRenderKey = key;
    }
    if (items.length === 0 && source) {
      el.transcriptMeta.textContent = `No chunks for source "${source}" in the last ${sinceSecondsValue()}s • updated ${new Date().toLocaleTimeString()} • try "All sources".`;
    } else {
      const translatedNote = liveTranslateEnabled() ? " • live EN translation on" : "";
      el.transcriptMeta.textContent = `Showing ${items.length} chunks • updated ${new Date().toLocaleTimeString()}${translatedNote}`;
    }
  } catch (error) {
    el.transcriptMeta.textContent = `Transcript refresh failed: ${error.message}`;
    setStatus(`Transcript refresh failed: ${error.message}`, true);
  }
}

async function askOllama() {
  const question = (el.question.value || "").trim();
  if (!question) {
    el.answer.textContent = "Type a question first.";
    return;
  }
  const effectiveLimit = isSummaryQuestion(question)
    ? Math.max(queryLimitValue(), 24)
    : queryLimitValue();
  const wantsVoice = Boolean(el.speakAnswer.checked);
  const wantsStreaming = wantsVoice && Boolean(el.streamVoice.checked);

  el.answer.textContent = "Querying Ollama...";
  el.answerProvider.textContent = "";
  el.ttsStatus.textContent = "";
  clearAnswerAudio();

  try {
    const payload = {
      question,
      translate: Boolean(el.queryTranslate && el.queryTranslate.checked),
      source_id: sourceFilterValue(),
      since_seconds: sinceSecondsValue(),
      limit: effectiveLimit,
      provider: "ollama",
      ollama_model: selectedOllamaModel(),
      speak: wantsVoice && !wantsStreaming,
      tts_voice: selectedTtsVoice(),
    };
    const result = await request("/v1/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    el.answer.textContent = result.answer;
    el.answerProvider.textContent = `Provider: ${result.provider}`;
    renderEvidence(result.evidence || []);

    if (!wantsVoice) {
      el.ttsStatus.textContent = "Voice response disabled.";
      return;
    }

    if (wantsStreaming) {
      await streamTtsForAnswer(result.answer);
      return;
    }

    renderAnswerAudio(result.audio_base64, result.audio_mime_type);
    if (result.tts_error) {
      el.ttsStatus.textContent = `TTS fallback: ${result.tts_error}`;
    } else if (result.audio_base64) {
      el.ttsStatus.textContent = `Voice response generated by ${result.tts_provider || "tts-service"}.`;
      el.answerAudio.play().catch(() => {
        // Playback can be blocked by browser autoplay policy.
      });
    } else {
      el.ttsStatus.textContent = "No TTS audio returned.";
    }
  } catch (error) {
    el.answer.textContent = `Question failed: ${error.message}`;
    el.answerProvider.textContent = "";
    el.ttsStatus.textContent = "";
    clearAnswerAudio();
    renderEvidence([]);
  }
}

function startPolling() {
  if (state.pollHandle) {
    clearInterval(state.pollHandle);
  }
  refreshTranscripts();
  refreshRunningSources();
  refreshTranscriberStatus();
  refreshCaptureReadiness();
  state.pollHandle = setInterval(() => {
    state.pollTicks += 1;
    refreshTranscripts();
    refreshRunningSources();
    if (state.pollTicks % 4 === 0) {
      refreshTranscriberStatus();
      refreshCaptureReadiness();
    }
    if (state.pollTicks % 8 === 0) {
      refreshSpeakerStatus();
    }
  }, 2500);
}

function wireEvents() {
  el.refreshDevices.addEventListener("click", refreshDevices);
  el.detectDevices.addEventListener("click", detectActiveDevices);
  el.cycleDevices.addEventListener("click", cycleAndFindDevice);
  el.refreshModels.addEventListener("click", refreshOllamaModels);
  el.setDefaultModel.addEventListener("click", setDefaultOllamaModel);
  el.refreshTtsVoices.addEventListener("click", refreshTtsVoices);
  el.refreshSources.addEventListener("click", () => refreshRunningSources());
  el.startManualSource.addEventListener("click", startManualSource);
  el.stopSource.addEventListener("click", stopSource);
  el.addSource.addEventListener("click", addSourceFromPreset);
  el.refreshNow.addEventListener("click", refreshTranscripts);
  el.applyLanguageHint.addEventListener("click", applyLanguageHintToSelectedSource);
  el.askOllama.addEventListener("click", askOllama);
  el.verbose.addEventListener("change", refreshTranscripts);
  el.sourceFilter.addEventListener("change", () => {
    resetLiveTranslationCache();
    refreshTranscripts();
  });
  el.liveTranslate.addEventListener("change", () => {
    resetLiveTranslationCache();
    refreshTranscripts();
  });
  el.translateSourceLanguage.addEventListener("change", () => {
    resetLiveTranslationCache();
    refreshTranscripts();
  });
  el.showStoppedSources.addEventListener("change", () => renderRunningSources(visibleSourcesForList()));
  el.manualSourceType.addEventListener("change", syncManualSourceFields);

  el.runningSources.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const button = target.closest(".stop-source-item");
    if (!(button instanceof HTMLElement)) {
      return;
    }
    const sourceId = button.getAttribute("data-source-id");
    if (sourceId) {
      stopSourceById(sourceId);
    }
  });
}

async function init() {
  wireEvents();
  syncManualSourceFields();
  updateSourceFilterOptions();
  renderMicScanResults([]);
  await refreshDevices();
  await refreshRunningSources();
  await autoStartCommonSources();
  await refreshOllamaModels();
  await refreshTtsVoices();
  await refreshSpeakerStatus();
  await refreshTranscriberStatus();
  await refreshCaptureReadiness();
  startPolling();
}

init();
