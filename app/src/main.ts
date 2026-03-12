import { invoke } from "@tauri-apps/api/core";
import { disposeAppState, initAppState, isAppActive, onAppActivityChange } from "./app-state";
import type { ServiceDef } from "./services";
import { renderServiceGrid, pollHealth, setupEventDelegation } from "./ui";
import { bindAudioAssistService, renderAudioPanel, setupAudioPanelEvents, pollAudioPanel, stopAudioPanelPolling } from "./audio-panel";
import { pauseLogPanelPolling, resumeLogPanelPolling } from "./logs";
import { primeMicrophonePermission } from "./media-permissions";
import { renderVoiceChatPanel, setupVoiceChatEvents, pollVoiceChatPanel, stopVoiceChatPolling } from "./voice-chat-panel";
import { renderVoiceProfilePanel, setupVoiceProfileEvents, pollVoiceProfilePanel, stopVoiceProfilePolling } from "./voice-profile-panel";

let services: ServiceDef[] = [];
let healthTimer: ReturnType<typeof setInterval> | null = null;
let clockTimer: ReturnType<typeof setInterval> | null = null;
let dashboardPollingActive = false;
let stopAppActivityListener: (() => void) | null = null;

async function init() {
  renderAudioPanel();
  setupAudioPanelEvents();
  renderVoiceChatPanel();
  setupVoiceChatEvents();
  renderVoiceProfilePanel();
  setupVoiceProfileEvents();

  try {
    services = await invoke<ServiceDef[]>("get_services");
  } catch (e) {
    document.getElementById("service-grid")!.textContent = `Failed to load services: ${e}`;
  }

  bindAudioAssistService(services);
  if (services.length > 0) {
    renderServiceGrid(services);
    setupEventDelegation(services);
  }

  await initAppState();
  stopAppActivityListener = onAppActivityChange((active) => {
    if (active) {
      void startDashboardPolling();
    } else {
      stopDashboardPolling();
    }
  });

  if (isAppActive()) {
    await startDashboardPolling();
  }

  void primeMicrophonePermission();

  window.addEventListener("beforeunload", handleBeforeUnload, { once: true });
}

async function startDashboardPolling() {
  if (dashboardPollingActive) {
    return;
  }
  dashboardPollingActive = true;

  pollAudioPanel();
  pollVoiceChatPanel();
  pollVoiceProfilePanel();
  resumeLogPanelPolling();

  if (services.length > 0) {
    await pollHealth(services);
    healthTimer = setInterval(() => {
      void pollHealth(services);
    }, 5000);
  }

  updateClock();
  clockTimer = setInterval(updateClock, 1000);
}

function stopDashboardPolling() {
  if (!dashboardPollingActive) {
    return;
  }
  dashboardPollingActive = false;

  stopAudioPanelPolling();
  stopVoiceChatPolling();
  stopVoiceProfilePolling();
  pauseLogPanelPolling();

  if (healthTimer) {
    clearInterval(healthTimer);
    healthTimer = null;
  }

  if (clockTimer) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function handleBeforeUnload() {
  stopAppActivityListener?.();
  stopAppActivityListener = null;
  stopDashboardPolling();
  disposeAppState();
}

function updateClock() {
  const el = document.getElementById("clock");
  if (el) {
    el.textContent = new Date().toLocaleTimeString();
  }
}

window.addEventListener("DOMContentLoaded", init);
