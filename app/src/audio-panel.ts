import {
  fetchVolumeDevices,
  setVolume,
  fetchSourceLevels,
  fetchCaptureReadiness,
  fetchMicDevices,
  fetchMicLevels,
  setMicVolume,
  startMicTest,
  type VolumeDevice,
  type SourceLevel,
  type CaptureReadiness,
  type MicDevice,
  type MicLevel,
} from "./api";

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
        <div id="capture-readiness"></div>
        <div id="source-levels">
          <span class="audio-unavailable">loading...</span>
        </div>
      </div>
    </div>
  `;
}

// --- Volume Panel ---

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
  if (el) el.innerHTML = "";
}

// --- Events ---

export function setupAudioPanelEvents() {
  const container = document.getElementById("audio-panel");
  if (!container) return;

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
    const testBtn = (e.target as HTMLElement).closest(".mic-test-btn") as HTMLButtonElement | null;
    if (testBtn) {
      const statusEl = document.getElementById("mic-test-status");
      const seconds = 3;
      testBtn.disabled = true;
      testBtn.textContent = `Recording ${seconds}s...`;
      if (statusEl) statusEl.textContent = "speak now";
      try {
        await startMicTest(seconds);
        // Wait for recording phase
        await new Promise((r) => setTimeout(r, seconds * 1000 + 200));
        testBtn.textContent = "Playing back...";
        if (statusEl) statusEl.textContent = "listen";
        // Wait for playback phase
        await new Promise((r) => setTimeout(r, seconds * 1000 + 500));
      } catch (err) {
        console.error("mic test failed:", err);
        if (statusEl) statusEl.textContent = "error";
      }
      testBtn.disabled = false;
      testBtn.textContent = "Test Mic";
      if (statusEl) statusEl.textContent = "";
      return;
    }

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
    const devices = await fetchVolumeDevices();
    // Always track latest non-zero volume for unmute restore
    for (const [slug, d] of Object.entries(devices)) {
      if (d.volume > 0) {
        preMuteVolume.set(slug, d.volume);
      }
    }
    updateVolumeDevices(devices);
  } catch {
    const el = document.getElementById("volume-devices");
    if (el) el.innerHTML = `<span class="audio-unavailable">volume service unavailable</span>`;
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
    for (const [slug, d] of Object.entries(devices)) {
      if (d.volume > 0) {
        preMuteMicVolume.set(slug, d.volume);
      }
    }
    updateMicDevices(devices);
  } catch {
    const el = document.getElementById("mic-devices");
    if (el) el.innerHTML = `<span class="audio-unavailable">mic service unavailable</span>`;
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
  // Initial fetch
  refreshVolume();
  refreshMics();
  refreshMicLevels();
  refreshLevels();
  refreshCapture();

  // Staggered intervals — volume/mic poll skips when locked
  pollTimers.push(setInterval(() => { if (!volumeLocked) refreshVolume(); }, 1000));
  pollTimers.push(setInterval(() => { if (!micLocked) refreshMics(); }, 10000));
  pollTimers.push(setInterval(refreshMicLevels, 500));  // fast poll for live meters
  pollTimers.push(setInterval(refreshLevels, 2000));
  pollTimers.push(setInterval(refreshCapture, 15000));
}

export function stopAudioPanelPolling() {
  for (const t of pollTimers) clearInterval(t);
  pollTimers = [];
}
