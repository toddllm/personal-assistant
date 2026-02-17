import {
  fetchVoiceProfiles,
  cloneVoice,
  deleteVoiceProfile,
  testVoice,
  uploadProfilePhoto,
  type VoiceProfile,
} from "./api";

let pollTimers: ReturnType<typeof setInterval>[] = [];
let profiles: VoiceProfile[] = [];
let playingAudio: HTMLAudioElement | null = null;

// --- Render ---

export function renderVoiceProfilePanel() {
  const container = document.getElementById("voice-profile-panel");
  if (!container) return;

  container.innerHTML = `
    <h2 class="group-heading">Voice Profiles</h2>
    <div class="vp-layout">
      <div class="audio-card vp-list-card">
        <h3 class="audio-card-title">All Voices</h3>
        <div class="vp-profiles" id="vp-profiles">
          <span class="audio-unavailable">loading...</span>
        </div>
      </div>
      <div class="audio-card vp-clone-card">
        <h3 class="audio-card-title">Clone New Voice</h3>
        <div class="vp-form">
          <div class="vp-form-row">
            <label class="vp-label">Name (lowercase)</label>
            <input type="text" class="vp-input" id="vp-clone-name"
                   placeholder="e.g. todd" pattern="[a-z0-9_]+" maxlength="50" />
          </div>
          <div class="vp-form-row">
            <label class="vp-label">Display Name</label>
            <input type="text" class="vp-input" id="vp-clone-display"
                   placeholder="e.g. Todd" maxlength="100" />
          </div>
          <div class="vp-form-row">
            <label class="vp-label">Reference Text</label>
            <textarea class="vp-textarea" id="vp-clone-reftext" rows="2"
                      placeholder="Transcript of the reference audio"></textarea>
          </div>
          <div class="vp-form-row">
            <label class="vp-label">Reference Audio</label>
            <div class="vp-audio-input">
              <button class="vp-file-btn" id="vp-choose-file">Choose WAV File</button>
              <span class="vp-file-name" id="vp-file-name">no file selected</span>
              <input type="file" id="vp-file-input" accept=".wav,audio/wav" style="display:none" />
            </div>
          </div>
          <div class="vp-form-row vp-checkbox-row">
            <label><input type="checkbox" id="vp-clone-owner" /> Mark as owner voice</label>
          </div>
          <button class="vp-clone-btn" id="vp-clone-btn" disabled>Clone Voice</button>
          <div class="vp-clone-status" id="vp-clone-status"></div>
        </div>
      </div>
    </div>
  `;
}

// --- Profile List ---

async function refreshProfiles() {
  try {
    profiles = await fetchVoiceProfiles();
    renderProfileList();
  } catch {
    const el = document.getElementById("vp-profiles");
    if (el) el.innerHTML = `<span class="audio-unavailable">service unavailable</span>`;
  }
}

function renderProfileList() {
  const el = document.getElementById("vp-profiles");
  if (!el) return;

  if (profiles.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no voices available</span>`;
    return;
  }

  const cloned = profiles.filter(p => p.voice_type === "cloned");
  const builtin = profiles.filter(p => p.voice_type === "builtin");

  let html = "";

  // Owner card at top
  const owner = cloned.find(p => p.is_owner);
  if (owner) {
    html += `
      <div class="vp-owner-card">
        <div class="vp-owner-photo" id="vp-owner-photo-${escapeAttr(owner.name)}">
          ${owner.has_photo ? `<img src="" class="vp-photo-img" data-name="${escapeAttr(owner.name)}" />` : `<span class="vp-photo-placeholder">?</span>`}
        </div>
        <div class="vp-owner-info">
          <span class="vp-owner-name">${escapeHtml(owner.display_name)}</span>
          <span class="vp-owner-tag">owner &middot; cloned</span>
        </div>
        <div class="vp-owner-actions">
          <button class="vp-test-btn" data-vp-test="${escapeAttr(owner.name)}">Test</button>
          <button class="vp-photo-btn" data-vp-photo="${escapeAttr(owner.name)}">Photo</button>
        </div>
      </div>
    `;
  }

  // Cloned voices
  if (cloned.length > 0) {
    html += `<div class="vp-section-label">Cloned Voices</div>`;
    for (const p of cloned) {
      html += renderProfileRow(p, true);
    }
  }

  // Builtin voices
  if (builtin.length > 0) {
    html += `<div class="vp-section-label">Builtin Voices</div>`;
    for (const p of builtin) {
      html += renderProfileRow(p, false);
    }
  }

  el.innerHTML = html;
}

function renderProfileRow(p: VoiceProfile, canDelete: boolean): string {
  const tags: string[] = [];
  if (p.voice_type === "cloned") tags.push("cloned");
  if (p.is_owner) tags.push("owner");
  const tagStr = tags.length > 0 ? ` <span class="vp-tag">${tags.join(", ")}</span>` : "";

  return `
    <div class="vp-row">
      <span class="vp-row-name">${escapeHtml(p.display_name)}${tagStr}</span>
      <div class="vp-row-actions">
        <button class="vp-test-btn" data-vp-test="${escapeAttr(p.name)}">Test</button>
        ${canDelete ? `<button class="vp-delete-btn" data-vp-delete="${escapeAttr(p.name)}">Delete</button>` : ""}
      </div>
    </div>
  `;
}

// --- Events ---

export function setupVoiceProfileEvents() {
  const container = document.getElementById("voice-profile-panel");
  if (!container) return;

  // File input trigger
  container.addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;

    // Choose file button
    if (target.id === "vp-choose-file" || target.closest("#vp-choose-file")) {
      document.getElementById("vp-file-input")?.click();
      return;
    }

    // Test voice button
    const testAttr = target.getAttribute("data-vp-test") || target.closest("[data-vp-test]")?.getAttribute("data-vp-test");
    if (testAttr) {
      await handleTestVoice(testAttr, target);
      return;
    }

    // Delete button
    const deleteAttr = target.getAttribute("data-vp-delete") || target.closest("[data-vp-delete]")?.getAttribute("data-vp-delete");
    if (deleteAttr) {
      await handleDeleteVoice(deleteAttr);
      return;
    }

    // Photo button
    const photoAttr = target.getAttribute("data-vp-photo") || target.closest("[data-vp-photo]")?.getAttribute("data-vp-photo");
    if (photoAttr) {
      handlePhotoUpload(photoAttr);
      return;
    }

    // Clone button
    if (target.id === "vp-clone-btn" || target.closest("#vp-clone-btn")) {
      await handleCloneVoice();
      return;
    }
  });

  // File input change
  container.addEventListener("change", (e) => {
    const target = e.target as HTMLElement;
    if (target.id === "vp-file-input") {
      const input = target as HTMLInputElement;
      const file = input.files?.[0];
      const nameEl = document.getElementById("vp-file-name");
      const cloneBtn = document.getElementById("vp-clone-btn") as HTMLButtonElement | null;
      if (file && nameEl) {
        nameEl.textContent = file.name;
        if (cloneBtn) cloneBtn.disabled = false;
      }
    }
  });
}

async function handleTestVoice(name: string, btn: HTMLElement) {
  btn.textContent = "...";
  try {
    // Stop any currently playing audio
    if (playingAudio) {
      playingAudio.pause();
      playingAudio = null;
    }
    const audioBase64 = await testVoice(name);
    const audio = new Audio(`data:audio/wav;base64,${audioBase64}`);
    playingAudio = audio;
    audio.play();
    audio.addEventListener("ended", () => { playingAudio = null; });
  } catch (err) {
    console.error("Test voice error:", err);
  }
  btn.textContent = "Test";
}

async function handleDeleteVoice(name: string) {
  try {
    await deleteVoiceProfile(name);
    await refreshProfiles();
  } catch (err) {
    console.error("Delete voice error:", err);
  }
}

function handlePhotoUpload(name: string) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/jpeg,image/png,.jpg,.jpeg,.png";
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const buffer = await file.arrayBuffer();
      const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)));
      await uploadProfilePhoto(name, base64);
      await refreshProfiles();
    } catch (err) {
      console.error("Photo upload error:", err);
    }
  };
  input.click();
}

async function handleCloneVoice() {
  const nameEl = document.getElementById("vp-clone-name") as HTMLInputElement | null;
  const displayEl = document.getElementById("vp-clone-display") as HTMLInputElement | null;
  const refTextEl = document.getElementById("vp-clone-reftext") as HTMLTextAreaElement | null;
  const fileInput = document.getElementById("vp-file-input") as HTMLInputElement | null;
  const ownerEl = document.getElementById("vp-clone-owner") as HTMLInputElement | null;
  const statusEl = document.getElementById("vp-clone-status");
  const btn = document.getElementById("vp-clone-btn") as HTMLButtonElement | null;

  if (!nameEl || !displayEl || !refTextEl || !fileInput) return;

  const name = nameEl.value.trim();
  const displayName = displayEl.value.trim();
  const refText = refTextEl.value.trim();
  const file = fileInput.files?.[0];
  const isOwner = ownerEl?.checked ?? false;

  if (!name || !displayName || !refText || !file) {
    if (statusEl) statusEl.textContent = "Please fill all fields and select an audio file.";
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = "Cloning..."; }
  if (statusEl) statusEl.textContent = "Uploading and cloning voice...";

  try {
    const buffer = await file.arrayBuffer();
    const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)));
    await cloneVoice(name, displayName, refText, base64, isOwner);
    if (statusEl) statusEl.textContent = "Voice cloned successfully!";
    // Reset form
    nameEl.value = "";
    displayEl.value = "";
    refTextEl.value = "";
    fileInput.value = "";
    const fileNameEl = document.getElementById("vp-file-name");
    if (fileNameEl) fileNameEl.textContent = "no file selected";
    if (ownerEl) ownerEl.checked = false;
    await refreshProfiles();
  } catch (err) {
    if (statusEl) statusEl.textContent = `Error: ${err}`;
  }
  if (btn) { btn.disabled = false; btn.textContent = "Clone Voice"; }
}

// --- Polling ---

export function pollVoiceProfilePanel() {
  refreshProfiles();
  pollTimers.push(setInterval(refreshProfiles, 15000));
}

export function stopVoiceProfilePolling() {
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
