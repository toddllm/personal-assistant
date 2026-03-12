import {
  fetchVoiceChatDevices,
  fetchVoiceChatStatus,
  fetchVoiceChatConversation,
  startVoiceChat,
  stopVoiceChat,
  type VoiceChatDevice,
  type VoiceChatStatus,
  type VoiceChatMessage,
} from "./api";

let pollTimers: ReturnType<typeof setInterval>[] = [];
let lastConversationLength = 0;
let serviceAvailable = false;

// --- Render ---

export function renderVoiceChatPanel() {
  const container = document.getElementById("voice-chat-panel");
  if (!container) return;

  container.innerHTML = `
    <h2 class="group-heading">Voice Chat</h2>
    <div class="vc-layout">
      <div class="audio-card" id="vc-controls">
        <h3 class="audio-card-title">Controls</h3>
        <div class="vc-status-row">
          <span class="vc-status-dot" id="vc-status-dot"></span>
          <span class="vc-status-label" id="vc-status-label">offline</span>
        </div>
        <div class="vc-device-row">
          <label class="vc-label">Mic</label>
          <select class="vc-select" id="vc-mic-select">
            <option value="">loading...</option>
          </select>
        </div>
        <div class="vc-device-row">
          <label class="vc-label">Speaker</label>
          <select class="vc-select" id="vc-speaker-select">
            <option value="">loading...</option>
          </select>
        </div>
        <div class="vc-btn-row">
          <button class="vc-start-btn" id="vc-start-btn" disabled>Start</button>
          <button class="vc-stop-btn" id="vc-stop-btn" disabled>Stop</button>
        </div>
        <div class="vc-model-info" id="vc-model-info"></div>
      </div>
      <div class="audio-card vc-conversation-card" id="vc-conversation-card">
        <h3 class="audio-card-title">Conversation</h3>
        <div class="vc-conversation" id="vc-conversation">
          <span class="audio-unavailable">no conversation yet</span>
        </div>
      </div>
    </div>
  `;
}

// --- Device lists ---

async function refreshDevices() {
  try {
    const devices = await fetchVoiceChatDevices();
    serviceAvailable = true;
    populateSelect("vc-mic-select", devices.inputs);
    populateSelect("vc-speaker-select", devices.outputs);

    // Try to restore selection from current status
    const status = await fetchVoiceChatStatus();
    selectByName("vc-mic-select", status.mic_device);
    selectByName("vc-speaker-select", status.speaker_device);

    const startBtn = document.getElementById("vc-start-btn") as HTMLButtonElement | null;
    if (startBtn && !status.running) startBtn.disabled = false;
  } catch {
    serviceAvailable = false;
    setSelectUnavailable("vc-mic-select");
    setSelectUnavailable("vc-speaker-select");
  }
}

function populateSelect(id: string, devices: VoiceChatDevice[]) {
  const sel = document.getElementById(id) as HTMLSelectElement | null;
  if (!sel) return;
  sel.innerHTML = devices.map((d) =>
    `<option value="${escapeAttr(d.name)}">${escapeHtml(d.name)}</option>`
  ).join("");
}

function selectByName(id: string, name: string) {
  const sel = document.getElementById(id) as HTMLSelectElement | null;
  if (!sel) return;
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === name) {
      sel.selectedIndex = i;
      return;
    }
  }
  // Partial match
  const lower = name.toLowerCase();
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value.toLowerCase().includes(lower)) {
      sel.selectedIndex = i;
      return;
    }
  }
}

function setSelectUnavailable(id: string) {
  const sel = document.getElementById(id) as HTMLSelectElement | null;
  if (sel) sel.innerHTML = `<option value="">service unavailable</option>`;
}

// --- Status ---

async function refreshStatus() {
  try {
    const status = await fetchVoiceChatStatus();
    serviceAvailable = true;
    updateStatusUI(status);
  } catch {
    serviceAvailable = false;
    updateStatusOffline();
  }
}

function updateStatusUI(status: VoiceChatStatus) {
  const dot = document.getElementById("vc-status-dot");
  const label = document.getElementById("vc-status-label");
  const startBtn = document.getElementById("vc-start-btn") as HTMLButtonElement | null;
  const stopBtn = document.getElementById("vc-stop-btn") as HTMLButtonElement | null;
  const info = document.getElementById("vc-model-info");

  if (dot) {
    dot.className = "vc-status-dot";
    if (!status.running) {
      dot.classList.add("vc-idle");
    } else if (status.state === "listening") {
      dot.classList.add("vc-listening");
    } else if (status.state === "thinking") {
      dot.classList.add("vc-thinking");
    } else if (status.state === "speaking") {
      dot.classList.add("vc-speaking");
    } else {
      dot.classList.add("vc-listening");
    }
  }

  if (label) {
    label.textContent = status.running ? status.state : "idle";
  }

  if (startBtn) startBtn.disabled = status.running;
  if (stopBtn) stopBtn.disabled = !status.running;

  if (info) {
    info.innerHTML = `
      <span class="vc-info-item">STT: ${escapeHtml(status.stt_model)}</span>
      <span class="vc-info-item">LLM: ${escapeHtml(status.llm_model)}</span>
      <span class="vc-info-item">TTS: ${escapeHtml(status.tts_voice)}</span>
    `;
  }
}

function updateStatusOffline() {
  const dot = document.getElementById("vc-status-dot");
  const label = document.getElementById("vc-status-label");
  const startBtn = document.getElementById("vc-start-btn") as HTMLButtonElement | null;
  const stopBtn = document.getElementById("vc-stop-btn") as HTMLButtonElement | null;

  if (dot) { dot.className = "vc-status-dot"; }
  if (label) { label.textContent = "offline"; }
  if (startBtn) startBtn.disabled = true;
  if (stopBtn) stopBtn.disabled = true;
}

// --- Conversation ---

async function refreshConversation() {
  if (!serviceAvailable) return;
  try {
    const messages = await fetchVoiceChatConversation();
    if (messages.length !== lastConversationLength) {
      lastConversationLength = messages.length;
      renderConversation(messages);
    }
  } catch {
    // silent
  }
}

function renderConversation(messages: VoiceChatMessage[]) {
  const el = document.getElementById("vc-conversation");
  if (!el) return;

  if (messages.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no conversation yet</span>`;
    return;
  }

  el.innerHTML = messages.map((m) => {
    const cls = m.role === "user" ? "vc-msg-user" : "vc-msg-assistant";
    const label = m.role === "user" ? "You" : "AI";
    const time = new Date(m.timestamp * 1000).toLocaleTimeString();
    return `
      <div class="vc-msg ${cls}">
        <span class="vc-msg-label">${label}</span>
        <span class="vc-msg-text">${escapeHtml(m.text)}</span>
        <span class="vc-msg-time">${time}</span>
      </div>
    `;
  }).join("");

  // Auto-scroll to bottom
  el.scrollTop = el.scrollHeight;
}

// --- Events ---

export function setupVoiceChatEvents() {
  const container = document.getElementById("voice-chat-panel");
  if (!container) return;

  container.addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;

    // Start button
    if (target.id === "vc-start-btn" || target.closest("#vc-start-btn")) {
      const btn = document.getElementById("vc-start-btn") as HTMLButtonElement;
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "Starting...";

      const micSel = document.getElementById("vc-mic-select") as HTMLSelectElement | null;
      const spkSel = document.getElementById("vc-speaker-select") as HTMLSelectElement | null;
      const mic = micSel?.value || undefined;
      const spk = spkSel?.value || undefined;

      try {
        const result = await startVoiceChat(mic, spk);
        if (!result.ok) {
          console.error("Start failed:", result.error);
          btn.textContent = "Start";
          btn.disabled = false;
        } else {
          btn.textContent = "Start";
          lastConversationLength = 0;
          await refreshStatus();
        }
      } catch (err) {
        console.error("Start error:", err);
        btn.textContent = "Start";
        btn.disabled = false;
      }
      return;
    }

    // Stop button
    if (target.id === "vc-stop-btn" || target.closest("#vc-stop-btn")) {
      const btn = document.getElementById("vc-stop-btn") as HTMLButtonElement;
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "Stopping...";

      try {
        await stopVoiceChat();
      } catch (err) {
        console.error("Stop error:", err);
      }
      btn.textContent = "Stop";
      await refreshStatus();
      return;
    }
  });
}

// --- Polling ---

export function pollVoiceChatPanel() {
  if (pollTimers.length > 0) {
    return;
  }

  refreshDevices();
  refreshStatus();

  // Device list: poll infrequently
  pollTimers.push(setInterval(refreshDevices, 30000));
  // Status: poll every 2s
  pollTimers.push(setInterval(refreshStatus, 2000));
  // Conversation: poll every 1s when running
  pollTimers.push(setInterval(refreshConversation, 1000));
}

export function stopVoiceChatPolling() {
  for (const t of pollTimers) clearInterval(t);
  pollTimers = [];
}

// --- Helpers ---

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(str: string): string {
  return str.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
