import {
  cloneVoice,
  deleteSpeakerProfile,
  deleteVoiceProfile,
  enrollSpeaker,
  fetchSpeakerProfiles,
  fetchVoiceProfiles,
  testVoice,
  uploadProfilePhoto,
  type SpeakerProfile,
  type VoiceProfile,
} from "./api";

let pollTimers: ReturnType<typeof setInterval>[] = [];
let voiceProfiles: VoiceProfile[] = [];
let speakerProfiles: SpeakerProfile[] = [];
let playingAudio: HTMLAudioElement | null = null;

export function renderVoiceProfilePanel() {
  const container = document.getElementById("voice-profile-panel");
  if (!container) return;

  container.innerHTML = `
    <h2 class="group-heading">Voice & Speaker Profiles</h2>
    <div class="vp-layout">
      <div class="audio-card vp-list-card">
        <h3 class="audio-card-title">Text-to-Speech Voices</h3>
        <div class="vp-profiles" id="vp-profiles">
          <span class="audio-unavailable">loading...</span>
        </div>
        <div class="vp-section-divider"></div>
        <h3 class="audio-card-title">Enrolled Speakers</h3>
        <div class="vp-speaker-profiles" id="vp-speaker-profiles">
          <span class="audio-unavailable">loading...</span>
        </div>
      </div>
      <div class="vp-sidebar">
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

        <div class="audio-card vp-speaker-card">
          <h3 class="audio-card-title">Enroll New Speaker</h3>
          <div class="vp-form">
            <div class="vp-form-row">
              <label class="vp-label">Display Name</label>
              <input type="text" class="vp-input" id="vp-speaker-name"
                     placeholder="e.g. Chelsea Deshane" maxlength="64" />
            </div>
            <div class="vp-form-row">
              <label class="vp-label">Enrollment Audio</label>
              <div class="vp-audio-input">
                <button class="vp-file-btn" id="vp-speaker-choose-file">Choose WAV File</button>
                <span class="vp-file-name" id="vp-speaker-file-name">no file selected</span>
                <input type="file" id="vp-speaker-file-input" accept=".wav,audio/wav" style="display:none" />
              </div>
              <span class="vp-hint">Use 5-20 seconds of mostly clean speech from one person.</span>
            </div>
            <div class="vp-form-row vp-checkbox-row">
              <label><input type="checkbox" id="vp-speaker-owner" /> Mark as owner speaker</label>
            </div>
            <button class="vp-clone-btn" id="vp-speaker-enroll-btn" disabled>Enroll Speaker</button>
            <div class="vp-clone-status" id="vp-speaker-status"></div>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function refreshVoiceProfiles() {
  try {
    voiceProfiles = await fetchVoiceProfiles();
    renderVoiceProfileList();
  } catch {
    const el = document.getElementById("vp-profiles");
    if (el) el.innerHTML = `<span class="audio-unavailable">service unavailable</span>`;
  }
}

async function refreshSpeakerProfiles() {
  try {
    speakerProfiles = await fetchSpeakerProfiles();
    renderSpeakerProfileList();
  } catch {
    const el = document.getElementById("vp-speaker-profiles");
    if (el) el.innerHTML = `<span class="audio-unavailable">speaker-service unavailable</span>`;
  }
}

async function refreshProfiles() {
  await Promise.allSettled([refreshVoiceProfiles(), refreshSpeakerProfiles()]);
}

function renderVoiceProfileList() {
  const el = document.getElementById("vp-profiles");
  if (!el) return;

  if (voiceProfiles.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no voices available</span>`;
    return;
  }

  const cloned = voiceProfiles.filter((p) => p.voice_type === "cloned");
  const builtin = voiceProfiles.filter((p) => p.voice_type === "builtin");

  let html = "";
  const owner = cloned.find((p) => p.is_owner);
  if (owner) {
    html += `
      <div class="vp-owner-card">
        <div class="vp-owner-photo" id="vp-owner-photo-${escapeAttr(owner.name)}">
          ${owner.has_photo ? `<img src="" class="vp-photo-img" data-name="${escapeAttr(owner.name)}" />` : `<span class="vp-photo-placeholder">?</span>`}
        </div>
        <div class="vp-owner-info">
          <span class="vp-owner-name">${escapeHtml(owner.display_name)}</span>
          <span class="vp-owner-tag">owner · cloned</span>
        </div>
        <div class="vp-owner-actions">
          <button class="vp-test-btn" data-vp-test="${escapeAttr(owner.name)}">Test</button>
          <button class="vp-photo-btn" data-vp-photo="${escapeAttr(owner.name)}">Photo</button>
        </div>
      </div>
    `;
  }

  if (cloned.length > 0) {
    html += `<div class="vp-section-label">Cloned Voices</div>`;
    for (const profile of cloned) {
      html += renderVoiceRow(profile, true);
    }
  }

  if (builtin.length > 0) {
    html += `<div class="vp-section-label">Builtin Voices</div>`;
    for (const profile of builtin) {
      html += renderVoiceRow(profile, false);
    }
  }

  el.innerHTML = html;
}

function renderSpeakerProfileList() {
  const el = document.getElementById("vp-speaker-profiles");
  if (!el) return;

  if (speakerProfiles.length === 0) {
    el.innerHTML = `<span class="audio-unavailable">no enrolled speakers yet</span>`;
    return;
  }

  el.innerHTML = speakerProfiles
    .map((profile) => {
      const tags: string[] = [];
      if (profile.is_owner) tags.push("owner");
      tags.push(`${profile.sample_count} sample${profile.sample_count === 1 ? "" : "s"}`);
      return `
        <div class="vp-row">
          <div class="vp-speaker-meta">
            <span class="vp-row-name">${escapeHtml(profile.name)}</span>
            <span class="vp-row-subtitle">${tags.join(" · ")}</span>
          </div>
          <div class="vp-row-actions">
            <button class="vp-delete-btn" data-vp-speaker-delete="${profile.id}">Delete</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderVoiceRow(profile: VoiceProfile, canDelete: boolean): string {
  const tags: string[] = [];
  if (profile.voice_type === "cloned") tags.push("cloned");
  if (profile.is_owner) tags.push("owner");
  const tagStr = tags.length > 0 ? ` <span class="vp-tag">${tags.join(", ")}</span>` : "";

  return `
    <div class="vp-row">
      <span class="vp-row-name">${escapeHtml(profile.display_name)}${tagStr}</span>
      <div class="vp-row-actions">
        <button class="vp-test-btn" data-vp-test="${escapeAttr(profile.name)}">Test</button>
        ${canDelete ? `<button class="vp-delete-btn" data-vp-delete="${escapeAttr(profile.name)}">Delete</button>` : ""}
      </div>
    </div>
  `;
}

export function setupVoiceProfileEvents() {
  const container = document.getElementById("voice-profile-panel");
  if (!container) return;

  container.addEventListener("click", async (event) => {
    const target = event.target as HTMLElement;

    if (target.id === "vp-choose-file" || target.closest("#vp-choose-file")) {
      document.getElementById("vp-file-input")?.click();
      return;
    }

    if (target.id === "vp-speaker-choose-file" || target.closest("#vp-speaker-choose-file")) {
      document.getElementById("vp-speaker-file-input")?.click();
      return;
    }

    const testAttr = target.getAttribute("data-vp-test") || target.closest("[data-vp-test]")?.getAttribute("data-vp-test");
    if (testAttr) {
      await handleTestVoice(testAttr, target);
      return;
    }

    const deleteAttr = target.getAttribute("data-vp-delete") || target.closest("[data-vp-delete]")?.getAttribute("data-vp-delete");
    if (deleteAttr) {
      await handleDeleteVoice(deleteAttr);
      return;
    }

    const speakerDeleteAttr = target.getAttribute("data-vp-speaker-delete") || target.closest("[data-vp-speaker-delete]")?.getAttribute("data-vp-speaker-delete");
    if (speakerDeleteAttr) {
      await handleDeleteSpeaker(Number(speakerDeleteAttr));
      return;
    }

    const photoAttr = target.getAttribute("data-vp-photo") || target.closest("[data-vp-photo]")?.getAttribute("data-vp-photo");
    if (photoAttr) {
      handlePhotoUpload(photoAttr);
      return;
    }

    if (target.id === "vp-clone-btn" || target.closest("#vp-clone-btn")) {
      await handleCloneVoice();
      return;
    }

    if (target.id === "vp-speaker-enroll-btn" || target.closest("#vp-speaker-enroll-btn")) {
      await handleEnrollSpeaker();
    }
  });

  container.addEventListener("change", (event) => {
    const target = event.target as HTMLElement;
    if (target.id === "vp-file-input") {
      syncSelectedFile("vp-file-input", "vp-file-name", "vp-clone-btn");
      return;
    }
    if (target.id === "vp-speaker-file-input") {
      syncSelectedFile("vp-speaker-file-input", "vp-speaker-file-name", "vp-speaker-enroll-btn");
    }
  });
}

async function handleTestVoice(name: string, btn: HTMLElement) {
  btn.textContent = "...";
  try {
    if (playingAudio) {
      playingAudio.pause();
      playingAudio = null;
    }
    const audioBase64 = await testVoice(name);
    const audio = new Audio(`data:audio/wav;base64,${audioBase64}`);
    playingAudio = audio;
    void audio.play();
    audio.addEventListener("ended", () => {
      playingAudio = null;
    });
  } catch (error) {
    console.error("Test voice error:", error);
  }
  btn.textContent = "Test";
}

async function handleDeleteVoice(name: string) {
  try {
    await deleteVoiceProfile(name);
    await refreshVoiceProfiles();
  } catch (error) {
    console.error("Delete voice error:", error);
  }
}

async function handleDeleteSpeaker(profileId: number) {
  try {
    await deleteSpeakerProfile(profileId);
    await refreshSpeakerProfiles();
  } catch (error) {
    console.error("Delete speaker error:", error);
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
      await uploadProfilePhoto(name, bytesToBase64(new Uint8Array(buffer)));
      await refreshVoiceProfiles();
    } catch (error) {
      console.error("Photo upload error:", error);
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

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Cloning...";
  }
  if (statusEl) statusEl.textContent = "Uploading and cloning voice...";

  try {
    const buffer = await file.arrayBuffer();
    await cloneVoice(name, displayName, refText, bytesToBase64(new Uint8Array(buffer)), isOwner);
    if (statusEl) statusEl.textContent = "Voice cloned successfully.";
    resetCloneForm();
    await refreshVoiceProfiles();
  } catch (error) {
    if (statusEl) statusEl.textContent = `Error: ${formatError(error)}`;
  }

  if (btn) {
    btn.disabled = false;
    btn.textContent = "Clone Voice";
  }
}

async function handleEnrollSpeaker() {
  const nameEl = document.getElementById("vp-speaker-name") as HTMLInputElement | null;
  const fileInput = document.getElementById("vp-speaker-file-input") as HTMLInputElement | null;
  const ownerEl = document.getElementById("vp-speaker-owner") as HTMLInputElement | null;
  const statusEl = document.getElementById("vp-speaker-status");
  const btn = document.getElementById("vp-speaker-enroll-btn") as HTMLButtonElement | null;

  if (!nameEl || !fileInput) return;

  const name = nameEl.value.trim();
  const file = fileInput.files?.[0];
  const isOwner = ownerEl?.checked ?? false;

  if (!name || !file) {
    if (statusEl) statusEl.textContent = "Choose a name and WAV file first.";
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Enrolling...";
  }
  if (statusEl) statusEl.textContent = "Converting audio and enrolling speaker...";

  try {
    const audio = await decodeAudioFileToMonoPcm(file);
    await enrollSpeaker(name, audio.sampleRate, bytesToBase64(audio.pcmBytes), isOwner);
    if (statusEl) statusEl.textContent = "Speaker enrolled successfully.";
    resetSpeakerForm();
    await refreshSpeakerProfiles();
  } catch (error) {
    if (statusEl) statusEl.textContent = `Error: ${formatError(error)}`;
  }

  if (btn) {
    btn.disabled = false;
    btn.textContent = "Enroll Speaker";
  }
}

export function pollVoiceProfilePanel() {
  if (pollTimers.length > 0) {
    return;
  }

  void refreshProfiles();
  pollTimers.push(setInterval(() => {
    void refreshProfiles();
  }, 15000));
}

export function stopVoiceProfilePolling() {
  for (const timer of pollTimers) clearInterval(timer);
  pollTimers = [];
}

function syncSelectedFile(inputId: string, nameId: string, buttonId: string) {
  const input = document.getElementById(inputId) as HTMLInputElement | null;
  const nameEl = document.getElementById(nameId);
  const button = document.getElementById(buttonId) as HTMLButtonElement | null;
  const file = input?.files?.[0];
  if (nameEl) nameEl.textContent = file?.name ?? "no file selected";
  if (button) button.disabled = !file;
}

function resetCloneForm() {
  const nameEl = document.getElementById("vp-clone-name") as HTMLInputElement | null;
  const displayEl = document.getElementById("vp-clone-display") as HTMLInputElement | null;
  const refTextEl = document.getElementById("vp-clone-reftext") as HTMLTextAreaElement | null;
  const fileInput = document.getElementById("vp-file-input") as HTMLInputElement | null;
  const ownerEl = document.getElementById("vp-clone-owner") as HTMLInputElement | null;
  const fileNameEl = document.getElementById("vp-file-name");
  const btn = document.getElementById("vp-clone-btn") as HTMLButtonElement | null;

  if (nameEl) nameEl.value = "";
  if (displayEl) displayEl.value = "";
  if (refTextEl) refTextEl.value = "";
  if (fileInput) fileInput.value = "";
  if (ownerEl) ownerEl.checked = false;
  if (fileNameEl) fileNameEl.textContent = "no file selected";
  if (btn) btn.disabled = true;
}

function resetSpeakerForm() {
  const nameEl = document.getElementById("vp-speaker-name") as HTMLInputElement | null;
  const fileInput = document.getElementById("vp-speaker-file-input") as HTMLInputElement | null;
  const ownerEl = document.getElementById("vp-speaker-owner") as HTMLInputElement | null;
  const fileNameEl = document.getElementById("vp-speaker-file-name");
  const btn = document.getElementById("vp-speaker-enroll-btn") as HTMLButtonElement | null;

  if (nameEl) nameEl.value = "";
  if (fileInput) fileInput.value = "";
  if (ownerEl) ownerEl.checked = false;
  if (fileNameEl) fileNameEl.textContent = "no file selected";
  if (btn) btn.disabled = true;
}

async function decodeAudioFileToMonoPcm(file: File): Promise<{ sampleRate: number; pcmBytes: Uint8Array }> {
  const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) {
    throw new Error("This build cannot decode WAV files in the dashboard.");
  }
  const audioContext = new AudioContextCtor();
  try {
    const encoded = await file.arrayBuffer();
    const decoded = await audioContext.decodeAudioData(encoded.slice(0));
    const mono = downmixAudioBuffer(decoded);
    return {
      sampleRate: decoded.sampleRate,
      pcmBytes: float32ToInt16Bytes(mono),
    };
  } finally {
    void audioContext.close();
  }
}

function downmixAudioBuffer(buffer: AudioBuffer): Float32Array {
  const channelCount = Math.max(1, buffer.numberOfChannels);
  const mono = new Float32Array(buffer.length);
  for (let channel = 0; channel < channelCount; channel += 1) {
    const data = buffer.getChannelData(channel);
    for (let index = 0; index < buffer.length; index += 1) {
      mono[index] += data[index] / channelCount;
    }
  }
  return mono;
}

function float32ToInt16Bytes(samples: Float32Array): Uint8Array {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    const value = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    view.setInt16(index * 2, Math.round(value), true);
  }
  return bytes;
}

function bytesToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function formatError(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return String(error);
}

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
