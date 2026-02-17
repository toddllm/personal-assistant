import { invoke } from "@tauri-apps/api/core";
import type { ServiceDef } from "./services";
import { renderServiceGrid, pollHealth, setupEventDelegation } from "./ui";
import { renderAudioPanel, setupAudioPanelEvents, pollAudioPanel } from "./audio-panel";
import { renderVoiceChatPanel, setupVoiceChatEvents, pollVoiceChatPanel } from "./voice-chat-panel";
import { renderVoiceProfilePanel, setupVoiceProfileEvents, pollVoiceProfilePanel } from "./voice-profile-panel";

let services: ServiceDef[] = [];

async function init() {
  // Audio panel (independent of services.json)
  renderAudioPanel();
  setupAudioPanelEvents();
  pollAudioPanel();

  // Voice Chat panel
  renderVoiceChatPanel();
  setupVoiceChatEvents();
  pollVoiceChatPanel();

  // Voice Profile panel
  renderVoiceProfilePanel();
  setupVoiceProfileEvents();
  pollVoiceProfilePanel();

  try {
    services = await invoke<ServiceDef[]>("get_services");
  } catch (e) {
    document.getElementById("service-grid")!.textContent = `Failed to load services: ${e}`;
    return;
  }

  renderServiceGrid(services);
  setupEventDelegation(services);

  // Initial health check
  await pollHealth(services);

  // Poll every 5 seconds
  setInterval(() => pollHealth(services), 5000);

  // Clock
  updateClock();
  setInterval(updateClock, 1000);
}

function updateClock() {
  const el = document.getElementById("clock");
  if (el) {
    el.textContent = new Date().toLocaleTimeString();
  }
}

window.addEventListener("DOMContentLoaded", init);
