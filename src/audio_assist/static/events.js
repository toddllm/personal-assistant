const el = {
  lookbackHours: document.getElementById("lookback-hours"),
  lookaheadHours: document.getElementById("lookahead-hours"),
  connectGoogle: document.getElementById("connect-google"),
  syncCalendar: document.getElementById("sync-calendar"),
  ensureCapture: document.getElementById("ensure-capture"),
  refreshEvents: document.getElementById("refresh-events"),
  captureStatus: document.getElementById("capture-status"),
  captureReadiness: document.getElementById("capture-readiness"),
  googleStatus: document.getElementById("google-status"),
  eventsMeta: document.getElementById("events-meta"),
  eventsList: document.getElementById("events-list"),
};

function escapeHtml(input) {
  return String(input)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function lookbackHoursValue() {
  const parsed = Number.parseInt(String(el.lookbackHours?.value || "168"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 168;
  }
  return Math.min(720, Math.max(1, parsed));
}

function lookaheadHoursValue() {
  const parsed = Number.parseInt(String(el.lookaheadHours?.value || "168"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 168;
  }
  return Math.min(720, Math.max(1, parsed));
}

function formatTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value || "");
  }
  return date.toLocaleString();
}

function formatRange(startValue, endValue) {
  return `${formatTimestamp(startValue)} - ${formatTimestamp(endValue)}`;
}

function formatRelativeAge(value) {
  const parsed = Date.parse(String(value || ""));
  if (!Number.isFinite(parsed)) {
    return "";
  }
  const deltaSeconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
  if (deltaSeconds < 90) {
    return "just now";
  }
  if (deltaSeconds < 3600) {
    return `${Math.round(deltaSeconds / 60)}m ago`;
  }
  if (deltaSeconds < 86400) {
    return `${Math.round(deltaSeconds / 3600)}h ago`;
  }
  return `${Math.round(deltaSeconds / 86400)}d ago`;
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

function renderCaptureReadiness(payload) {
  if (!el.captureReadiness) {
    return;
  }
  const status = String(payload?.status || "unknown").trim().toUpperCase();
  const summary = String(payload?.summary || "No readiness summary.");
  const issues = Array.isArray(payload?.issues) ? payload.issues : [];
  const firstIssue = issues.length > 0 ? String(issues[0]?.message || "").trim() : "";
  const issueSuffix = firstIssue ? ` | ${firstIssue}` : "";
  el.captureReadiness.textContent = `Capture Readiness: ${status} | ${summary}${issueSuffix}`;
  el.captureReadiness.style.color = readinessColor(payload?.status);
}

async function loadCaptureReadiness() {
  try {
    const payload = await request("/v1/capture/readiness");
    renderCaptureReadiness(payload);
  } catch (error) {
    if (el.captureReadiness) {
      el.captureReadiness.textContent = `Capture readiness failed: ${error.message}`;
      el.captureReadiness.style.color = "#bf2f39";
    }
  }
}

function formatSourceList(items) {
  if (!items.length) {
    return "";
  }
  return items.map((item) => String(item.source_id || "").trim()).filter((value) => value).join(", ");
}

async function loadCaptureStatus() {
  try {
    const payload = await request("/v1/sources");
    const sources = Array.isArray(payload) ? payload : [];
    const running = sources.filter((item) => Boolean(item && item.running));
    if (!running.length) {
      el.captureStatus.textContent = "No capture source is running. Click Ensure Capture Running.";
      return;
    }
    const names = formatSourceList(running);
    el.captureStatus.textContent = `Capture running: ${running.length} source${running.length === 1 ? "" : "s"} (${names}).`;
  } catch (error) {
    el.captureStatus.textContent = `Capture status failed: ${error.message}`;
  }
}

async function ensureCaptureRunning() {
  el.captureStatus.textContent = "Ensuring capture sources...";
  try {
    const payload = await request("/v1/sources/ensure", { method: "POST" });
    const started = Number(payload?.started_count || 0);
    const running = Number(payload?.running_count || 0);
    const errors = Array.isArray(payload?.errors) ? payload.errors : [];
    if (errors.length > 0) {
      el.captureStatus.textContent = `Capture ensure completed with warnings (${errors.length}). Running ${running} source${running === 1 ? "" : "s"}.`;
    } else if (started > 0) {
      el.captureStatus.textContent = `Capture started ${started} source${started === 1 ? "" : "s"}. Running ${running}.`;
    } else {
      el.captureStatus.textContent = `Capture already running (${running} source${running === 1 ? "" : "s"}).`;
    }
    await loadCaptureStatus();
    await loadCaptureReadiness();
  } catch (error) {
    el.captureStatus.textContent = `Capture ensure failed: ${error.message}`;
  }
}

async function loadGoogleStatus() {
  try {
    const payload = await request("/v1/google-sync/status");
    const reachable = Boolean(payload.reachable);
    const connected = Boolean(payload.connected);
    const cached = Boolean(payload.calendar_cached);
    const count = Number(payload.calendar_event_count);
    const syncedAt = String(payload.calendar_synced_at || "").trim();

    if (!reachable) {
      el.googleStatus.textContent = "Google sync service offline. Start `google-sync-service` or use app autostart.";
      return payload;
    }
    if (!connected) {
      el.googleStatus.textContent = "Google account not connected. Click Connect Google.";
      return payload;
    }
    if (!cached) {
      el.googleStatus.textContent = "Connected, but calendar cache is empty. Click Sync Calendar.";
      return payload;
    }
    const countLabel = Number.isFinite(count) ? `${count} event${count === 1 ? "" : "s"}` : "events";
    const syncedLabel = syncedAt ? `${formatTimestamp(syncedAt)} (${formatRelativeAge(syncedAt)})` : "unknown";
    el.googleStatus.textContent = `Connected • ${countLabel} cached • last sync ${syncedLabel}.`;
    return payload;
  } catch (error) {
    el.googleStatus.textContent = `Google status failed: ${error.message}`;
    return null;
  }
}

function renderEvents(payload) {
  const events = Array.isArray(payload?.events) ? payload.events : [];
  const syncedAt = payload?.synced_at ? `${formatTimestamp(payload.synced_at)} (${formatRelativeAge(payload.synced_at)})` : "unknown";
  el.eventsMeta.textContent = `${events.length} event${events.length === 1 ? "" : "s"} loaded • synced ${syncedAt}.`;

  if (!events.length) {
    el.eventsList.innerHTML = '<article class="entry"><p class="text muted">No events in this window.</p></article>';
    return;
  }

  el.eventsList.innerHTML = events
    .map((event) => {
      const title = String(event.title || "").trim() || "(Untitled meeting)";
      const overlapCount = Number(event.overlap_count || 0);
      const transcripts = Array.isArray(event.transcripts) ? event.transcripts : [];
      const overlapLabel = `${overlapCount} transcript chunk${overlapCount === 1 ? "" : "s"} overlapping`;
      const overflowLabel =
        overlapCount > transcripts.length
          ? `<p class="text muted">Showing first ${transcripts.length} of ${overlapCount} overlapping chunks.</p>`
          : "";
      const openLink = event.html_link
        ? `<a class="ghost-btn" href="${escapeHtml(event.html_link)}" target="_blank" rel="noreferrer">Open Event</a>`
        : "";
      const joinLink = event.hangout_link
        ? `<a class="ghost-btn" href="${escapeHtml(event.hangout_link)}" target="_blank" rel="noreferrer">Join Link</a>`
        : "";
      const transcriptList = transcripts.length
        ? `<ul class="event-transcript-list">
            ${transcripts
              .map((item) => {
                const speaker = String(item?.speaker || "").trim();
                const source = String(item?.source_id || "").trim() || "source";
                const who = speaker ? `${source}/${speaker}` : source;
                return `<li class="event-transcript">
                    <div class="meta">${escapeHtml(`${who} | ${formatRange(item.started_at, item.ended_at)} | #${item.id}`)}</div>
                    <p class="text">${escapeHtml(String(item.text || ""))}</p>
                  </li>`;
              })
              .join("")}
          </ul>`
        : '<p class="text muted">No overlapping transcripts for this event.</p>';

      return `<article class="entry event-entry">
          <div class="source-row">
            <div class="source-main">
              <div class="meta">${escapeHtml(title)}</div>
            </div>
            <div class="source-row-actions">
              ${openLink}
              ${joinLink}
            </div>
          </div>
          <p class="text muted">${escapeHtml(formatRange(event.started_at, event.ended_at))}</p>
          <p class="text muted">${escapeHtml(overlapLabel)}</p>
          ${overflowLabel}
          ${transcriptList}
        </article>`;
    })
    .join("");
}

async function loadEventsPage() {
  el.eventsMeta.textContent = "Loading events...";
  const params = new URLSearchParams({
    lookback_hours: String(lookbackHoursValue()),
    lookahead_hours: String(lookaheadHoursValue()),
  });
  try {
    const payload = await request(`/v1/events/page?${params.toString()}`);
    renderEvents(payload);
  } catch (error) {
    el.eventsMeta.textContent = `Events load failed: ${error.message}`;
    el.eventsList.innerHTML = "";
  }
}

async function connectGoogle() {
  const popup = window.open("/v1/google-sync/connect", "_blank", "noopener,noreferrer");
  if (!popup) {
    window.location.href = "/v1/google-sync/connect";
    return;
  }
  el.googleStatus.textContent = "Google connect window opened. Complete consent, then refresh events.";
}

async function syncCalendarNow() {
  try {
    el.googleStatus.textContent = "Syncing calendar...";
    const payload = await request("/v1/google-sync/calendar/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lookback_hours: lookbackHoursValue(),
        lookahead_hours: lookaheadHoursValue(),
      }),
    });
    const count = Number(payload?.count);
    const countLabel = Number.isFinite(count) ? `${count} event${count === 1 ? "" : "s"}` : "events";
    el.googleStatus.textContent = `Calendar synced (${countLabel}).`;
    await loadGoogleStatus();
    await loadEventsPage();
  } catch (error) {
    el.googleStatus.textContent = `Calendar sync failed: ${error.message}`;
  }
}

function wireEvents() {
  if (el.connectGoogle) {
    el.connectGoogle.addEventListener("click", () => {
      void connectGoogle();
    });
  }
  if (el.syncCalendar) {
    el.syncCalendar.addEventListener("click", () => {
      void syncCalendarNow();
    });
  }
  if (el.ensureCapture) {
    el.ensureCapture.addEventListener("click", () => {
      void ensureCaptureRunning();
    });
  }
  if (el.refreshEvents) {
    el.refreshEvents.addEventListener("click", () => {
      void (async () => {
        await loadCaptureStatus();
        await loadCaptureReadiness();
        await loadGoogleStatus();
        await loadEventsPage();
      })();
    });
  }
}

async function init() {
  wireEvents();
  await ensureCaptureRunning();
  await loadCaptureStatus();
  await loadCaptureReadiness();
  await loadGoogleStatus();
  await loadEventsPage();
}

init();
