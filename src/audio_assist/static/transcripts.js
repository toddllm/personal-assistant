const el = {
  sourceFilter: document.getElementById("source-filter"),
  sinceHours: document.getElementById("since-hours"),
  pageSize: document.getElementById("page-size"),
  compact: document.getElementById("compact"),
  sessionized: document.getElementById("sessionized"),
  loadFirst: document.getElementById("load-first"),
  loadMore: document.getElementById("load-more"),
  loadAll: document.getElementById("load-all"),
  copyLoaded: document.getElementById("copy-loaded"),
  copyPlain: document.getElementById("copy-plain"),
  pastePlain: document.getElementById("paste-plain"),
  downloadTxt: document.getElementById("download-txt"),
  downloadJson: document.getElementById("download-json"),
  clear: document.getElementById("clear"),
  status: document.getElementById("status"),
  meta: document.getElementById("meta"),
  list: document.getElementById("list"),
  plainText: document.getElementById("plain-text"),
};

const state = {
  items: [],
  nextBeforeId: null,
  hasMore: false,
  loading: false,
};

function speakerLabel(item) {
  const value = String(item?.speaker || "").trim();
  return value || null;
}

function escapeHtml(input) {
  return String(input)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function sourceValue() {
  const value = String(el.sourceFilter.value || "").trim();
  return value || null;
}

function sinceSecondsValue() {
  const parsed = Number.parseInt(String(el.sinceHours.value || "24"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 24 * 3600;
  }
  return parsed * 3600;
}

function pageSizeValue() {
  const parsed = Number.parseInt(String(el.pageSize.value || "200"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 200;
  }
  return Math.min(1000, Math.max(20, parsed));
}

function setStatus(message, isError = false) {
  el.status.textContent = message;
  el.status.classList.toggle("muted", !isError);
  el.status.style.color = isError ? "#bf2f39" : "";
}

function buildPlainText(items) {
  const chronological = [...items].sort((a, b) => {
    const startDiff = new Date(a.started_at) - new Date(b.started_at);
    if (startDiff !== 0) {
      return startDiff;
    }
    return Number(a.id) - Number(b.id);
  });
  return chronological
    .map((item) => {
      const start = new Date(item.started_at).toLocaleString();
      const end = new Date(item.ended_at).toLocaleTimeString();
      const speaker = speakerLabel(item);
      const who = speaker ? `${item.source_id}/${speaker}` : item.source_id;
      return `[${start} - ${end}] ${who}: ${item.text}`;
    })
    .join("\n");
}

function uniqueById(items) {
  const map = new Map();
  items.forEach((item) => map.set(Number(item.id), item));
  return [...map.values()].sort((a, b) => Number(b.id) - Number(a.id));
}

function render() {
  if (!state.items.length) {
    el.list.innerHTML = '<div class="entry"><p class="text muted">No transcript entries loaded yet.</p></div>';
    el.meta.textContent = "No entries loaded.";
    el.plainText.value = "";
    return;
  }
  el.list.innerHTML = state.items
    .map((item) => {
      const started = new Date(item.started_at).toLocaleString();
      const ended = new Date(item.ended_at).toLocaleTimeString();
      const speaker = speakerLabel(item);
      const sessionPart = item.session_id ? ` | session ${item.session_id}` : ` | #${item.id}`;
      return `
      <article class="entry">
        <div class="meta">${escapeHtml(
          `${item.source_id}${speaker ? ` | speaker ${speaker}` : ""} | ${started} - ${ended}${sessionPart}`
        )}</div>
        <p class="text">${escapeHtml(item.text)}</p>
      </article>
      `;
    })
    .join("");
  el.meta.textContent = `${state.items.length} entries loaded${state.hasMore ? " • older entries available" : ""}.`;
  el.plainText.value = buildPlainText(state.items);
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

async function loadSources() {
  const [sourceValues, running] = await Promise.all([
    request("/v1/transcripts/sources").catch(() => []),
    request("/v1/sources").catch(() => []),
  ]);
  const merged = new Set();
  if (Array.isArray(sourceValues)) {
    sourceValues.forEach((item) => {
      const value = String(item || "").trim();
      if (value) {
        merged.add(value);
      }
    });
  }
  if (Array.isArray(running)) {
    running.forEach((item) => {
      const value = String(item.source_id || "").trim();
      if (value) {
        merged.add(value);
      }
    });
  }
  const current = sourceValue();
  const options = ['<option value="">All sources</option>'];
  [...merged].sort().forEach((value) => {
    options.push(`<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`);
  });
  el.sourceFilter.innerHTML = options.join("");
  if (current && merged.has(current)) {
    el.sourceFilter.value = current;
  } else {
    el.sourceFilter.value = "";
  }
}

async function loadPage(reset = false) {
  if (state.loading) {
    return;
  }
  state.loading = true;
  try {
    if (reset) {
      state.items = [];
      state.nextBeforeId = null;
      state.hasMore = false;
      render();
      await loadSources();
    }
    const params = new URLSearchParams({
      limit: String(pageSizeValue()),
      since_seconds: String(sinceSecondsValue()),
      compact: String(Boolean(el.compact.checked)),
      sessionized: String(Boolean(el.sessionized.checked)),
    });
    const source = sourceValue();
    if (source) {
      params.set("source_id", source);
    }
    if (!reset && state.nextBeforeId) {
      params.set("before_id", String(state.nextBeforeId));
    }

    const payload = await request(`/v1/transcripts/page?${params.toString()}`);
    const newItems = Array.isArray(payload.items) ? payload.items : [];
    state.items = uniqueById([...state.items, ...newItems]);
    const nextBefore = Number(payload.next_before_id);
    state.nextBeforeId = Number.isFinite(nextBefore) ? nextBefore : null;
    state.hasMore = Boolean(payload.has_more);
    render();
    setStatus(
      `Loaded ${newItems.length} entries${state.hasMore ? " (more available)" : ""} at ${new Date().toLocaleTimeString()}.`
    );
  } catch (error) {
    setStatus(`Load failed: ${error.message}`, true);
  } finally {
    state.loading = false;
  }
}

async function loadAllPages() {
  await loadPage(true);
  let pages = 1;
  let stagnant = 0;
  const maxPages = 400;
  while (state.hasMore && pages < maxPages) {
    const beforeCount = state.items.length;
    await loadPage(false);
    pages += 1;
    if (state.items.length === beforeCount) {
      stagnant += 1;
    } else {
      stagnant = 0;
    }
    if (stagnant >= 2) {
      break;
    }
  }
  if (state.hasMore) {
    setStatus(
      `Loaded ${state.items.length} entries but stopped before end (page cap reached or pagination stalled).`,
      true
    );
    return;
  }
  setStatus(`Loaded all available entries (${state.items.length}).`);
}

async function copyLoaded() {
  const text = el.plainText.value.trim();
  if (!text) {
    setStatus("Nothing loaded to copy.", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setStatus(`Copied ${state.items.length} entries to clipboard.`);
  } catch {
    el.plainText.focus();
    el.plainText.select();
    const ok = document.execCommand("copy");
    if (ok) {
      setStatus(`Copied ${state.items.length} entries to clipboard.`);
      return;
    }
    setStatus("Clipboard copy blocked by browser.", true);
  }
}

async function pastePlainText() {
  if (!navigator.clipboard || typeof navigator.clipboard.readText !== "function") {
    setStatus("Clipboard paste is not available in this browser.", true);
    return;
  }
  try {
    const text = await navigator.clipboard.readText();
    if (!String(text).trim()) {
      setStatus("Clipboard is empty.", true);
      return;
    }
    el.plainText.value = text;
    el.plainText.focus();
    setStatus("Pasted clipboard text into plain text view.");
  } catch (error) {
    const detail = error?.message ? `: ${error.message}` : ".";
    setStatus(`Clipboard paste blocked by browser${detail}`, true);
  }
}

function download(name, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function downloadTxt() {
  const text = el.plainText.value.trim();
  if (!text) {
    setStatus("Nothing loaded to download.", true);
    return;
  }
  download(`audio-assist-transcripts-${Date.now()}.txt`, `${text}\n`, "text/plain;charset=utf-8");
  setStatus("TXT download started.");
}

function downloadJson() {
  if (!state.items.length) {
    setStatus("Nothing loaded to download.", true);
    return;
  }
  download(
    `audio-assist-transcripts-${Date.now()}.json`,
    `${JSON.stringify(state.items, null, 2)}\n`,
    "application/json;charset=utf-8"
  );
  setStatus("JSON download started.");
}

function clearView() {
  state.items = [];
  state.nextBeforeId = null;
  state.hasMore = false;
  render();
  setStatus("Cleared loaded entries.");
}

function wireEvents() {
  el.loadFirst.addEventListener("click", () => loadPage(true));
  el.loadMore.addEventListener("click", () => {
    if (!state.hasMore) {
      setStatus("No older entries available.");
      return;
    }
    loadPage(false);
  });
  el.loadAll.addEventListener("click", loadAllPages);
  el.copyLoaded.addEventListener("click", copyLoaded);
  if (el.copyPlain) {
    el.copyPlain.addEventListener("click", copyLoaded);
  }
  if (el.pastePlain) {
    el.pastePlain.addEventListener("click", pastePlainText);
  }
  el.downloadTxt.addEventListener("click", downloadTxt);
  el.downloadJson.addEventListener("click", downloadJson);
  el.clear.addEventListener("click", clearView);
  el.sessionized.addEventListener("change", () => loadPage(true));
  el.compact.addEventListener("change", () => loadPage(true));
}

async function init() {
  wireEvents();
  render();
  await loadPage(true);
}

init();
