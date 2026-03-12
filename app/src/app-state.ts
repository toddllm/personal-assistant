import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";

type Teardown = () => void;
type AppActivityListener = (active: boolean) => void;

const WINDOW_VISIBILITY_EVENT = "personal-assistant://window-visibility";

const listeners = new Set<AppActivityListener>();
const teardowns: Teardown[] = [];

let initialized = false;
let isVisible = document.visibilityState !== "hidden";
let isFocused = document.hasFocus();
let isActive = isVisible && isFocused;

function notifyIfChanged() {
  const nextActive = isVisible && isFocused;
  if (nextActive === isActive) {
    return;
  }

  isActive = nextActive;
  for (const listener of listeners) {
    listener(isActive);
  }
}

function setVisible(nextVisible: boolean) {
  isVisible = nextVisible;
  notifyIfChanged();
}

function setFocused(nextFocused: boolean) {
  isFocused = nextFocused;
  notifyIfChanged();
}

export function isAppActive(): boolean {
  return isActive;
}

export function onAppActivityChange(listener: AppActivityListener): Teardown {
  listeners.add(listener);
  listener(isActive);
  return () => {
    listeners.delete(listener);
  };
}

export async function initAppState(): Promise<void> {
  if (initialized) {
    return;
  }
  initialized = true;

  const handleVisibility = () => setVisible(document.visibilityState !== "hidden");
  const handleFocus = () => setFocused(true);
  const handleBlur = () => setFocused(false);

  document.addEventListener("visibilitychange", handleVisibility);
  window.addEventListener("focus", handleFocus);
  window.addEventListener("blur", handleBlur);

  teardowns.push(() => document.removeEventListener("visibilitychange", handleVisibility));
  teardowns.push(() => window.removeEventListener("focus", handleFocus));
  teardowns.push(() => window.removeEventListener("blur", handleBlur));

  const appWindow = getCurrentWindow();

  try {
    setVisible(await appWindow.isVisible());
  } catch (error) {
    console.warn("Failed to read initial window visibility:", error);
  }

  try {
    setFocused(await appWindow.isFocused());
  } catch (error) {
    console.warn("Failed to read initial window focus:", error);
  }

  try {
    const unlistenFocus = await appWindow.onFocusChanged(({ payload }) => {
      setFocused(payload);
    });
    teardowns.push(unlistenFocus);
  } catch (error) {
    console.warn("Failed to observe window focus changes:", error);
  }

  try {
    const unlistenVisibility = await listen<boolean>(WINDOW_VISIBILITY_EVENT, (event) => {
      setVisible(Boolean(event.payload));
    });
    teardowns.push(unlistenVisibility);
  } catch (error) {
    console.warn("Failed to observe window visibility changes:", error);
  }
}

export function disposeAppState() {
  while (teardowns.length > 0) {
    const teardown = teardowns.pop();
    teardown?.();
  }
  listeners.clear();
  initialized = false;
}
