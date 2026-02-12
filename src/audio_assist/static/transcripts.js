const el = {
  sourceFilter: document.getElementById("source-filter"),
  sinceHours: document.getElementById("since-hours"),
  pageSize: document.getElementById("page-size"),
  groupingMode: document.getElementById("grouping-mode"),
  meetingGapSeconds: document.getElementById("meeting-gap-seconds"),
  meetingMaxMinutes: document.getElementById("meeting-max-minutes"),
  tinyMaxSeconds: document.getElementById("tiny-max-seconds"),
  hideTinySegments: document.getElementById("hide-tiny-segments"),
  googleSyncLookbackHours: document.getElementById("google-sync-lookback-hours"),
  googleSyncLookaheadHours: document.getElementById("google-sync-lookahead-hours"),
  googleSyncConnect: document.getElementById("google-sync-connect"),
  googleSyncNow: document.getElementById("google-sync-now"),
  googleSyncRefresh: document.getElementById("google-sync-refresh"),
  googleSyncStatus: document.getElementById("google-sync-status"),
  googleSyncEventsMeta: document.getElementById("google-sync-events-meta"),
  googleSyncEvents: document.getElementById("google-sync-events"),
  loadFirst: document.getElementById("load-first"),
  loadMore: document.getElementById("load-more"),
  loadAll: document.getElementById("load-all"),
  generateTitles: document.getElementById("generate-titles"),
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
  plainIncludeTimestamps: document.getElementById("plain-include-timestamps"),
  plainIncludeSpeakers: document.getElementById("plain-include-speakers"),
};

const GROUPING_MODE = {
  MEETING: "meeting",
  SESSION: "session",
  SMART: "smart",
  RAW: "raw",
};

const state = {
  items: [],
  nextBeforeId: null,
  hasMore: false,
  loading: false,
  titleLoading: false,
  googleSync: null,
  googleCalendarEvents: [],
  googleSyncAutoSyncInFlight: false,
  generatedTitles: {},
  generatedTitleAttempts: {},
  lastView: null,
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

function googleLookbackHoursValue() {
  const parsed = Number.parseInt(String(el.googleSyncLookbackHours?.value || "24"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 24;
  }
  return Math.min(168, Math.max(1, parsed));
}

function googleLookaheadHoursValue() {
  const parsed = Number.parseInt(String(el.googleSyncLookaheadHours?.value || "24"), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 24;
  }
  return Math.min(168, Math.max(1, parsed));
}

function meetingGapSecondsValue() {
  const parsed = Number.parseInt(String(el.meetingGapSeconds?.value || "75"), 10);
  if (!Number.isFinite(parsed) || parsed < 10) {
    return 75;
  }
  return Math.min(600, parsed);
}

function meetingMaxMinutesValue() {
  const parsed = Number.parseInt(String(el.meetingMaxMinutes?.value || "30"), 10);
  if (!Number.isFinite(parsed) || parsed < 5) {
    return 30;
  }
  return Math.min(240, parsed);
}

function groupingModeValue() {
  const value = String(el.groupingMode?.value || GROUPING_MODE.SESSION).trim().toLowerCase();
  if (
    value === GROUPING_MODE.MEETING ||
    value === GROUPING_MODE.SESSION ||
    value === GROUPING_MODE.SMART ||
    value === GROUPING_MODE.RAW
  ) {
    return value;
  }
  return GROUPING_MODE.SESSION;
}

function groupingLabel(mode = groupingModeValue()) {
  if (mode === GROUPING_MODE.MEETING) {
    return "Meeting segments";
  }
  if (mode === GROUPING_MODE.SESSION) {
    return "Source sessions";
  }
  if (mode === GROUPING_MODE.SMART) {
    return "Speaker/source turns";
  }
  return "Raw chunks";
}

function tinySegmentThresholdSecondsValue() {
  const parsed = Number.parseInt(String(el.tinyMaxSeconds?.value || "12"), 10);
  if (!Number.isFinite(parsed) || parsed < 3) {
    return 12;
  }
  return Math.min(120, parsed);
}

function hideTinySegmentsEnabled() {
  return Boolean(el.hideTinySegments?.checked);
}

function plainIncludeTimestampsEnabled() {
  return Boolean(el.plainIncludeTimestamps?.checked);
}

function plainIncludeSpeakersEnabled() {
  return Boolean(el.plainIncludeSpeakers?.checked);
}

function plainTextOptions() {
  return {
    timestamps: plainIncludeTimestampsEnabled(),
    speakers: plainIncludeSpeakersEnabled(),
  };
}

function setStatus(message, isError = false) {
  el.status.textContent = message;
  el.status.classList.toggle("muted", !isError);
  el.status.style.color = isError ? "#bf2f39" : "";
}

function timeValue(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value || "");
  }
  return date.toLocaleString();
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

function formatRange(startValue, endValue) {
  return `${formatTimestamp(startValue)} - ${formatTimestamp(endValue)}`;
}

function formatDuration(startValue, endValue) {
  const startMs = timeValue(startValue);
  const endMs = timeValue(endValue);
  const totalSeconds = Math.max(0, Math.round((endMs - startMs) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

function intervalOverlapMs(leftStart, leftEnd, rightStart, rightEnd) {
  const start = Math.max(leftStart, rightStart);
  const end = Math.min(leftEnd, rightEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return 0;
  }
  return end - start;
}

function eventKey(event, index = 0) {
  const id = String(event?.event_id || "").trim();
  if (id) {
    return id;
  }
  const start = String(event?.start_at || "").trim();
  const end = String(event?.end_at || "").trim();
  return `event-${index}-${start}-${end}`;
}

function groupDomId(groupKey) {
  const encoded = encodeURIComponent(String(groupKey || "").trim() || "group");
  return `transcript-group-${encoded.replaceAll("%", "_")}`;
}

function chunkDomId(chunkId) {
  return `transcript-chunk-${Number(chunkId)}`;
}

function overlapChars(left, right, maxChars = 120) {
  const bound = Math.min(left.length, right.length, maxChars);
  for (let size = bound; size >= 12; size -= 1) {
    if (left.slice(-size) === right.slice(0, size)) {
      return size;
    }
  }
  return 0;
}

function mergeChunkText(existingText, incomingText) {
  const left = String(existingText || "").trim();
  const right = String(incomingText || "").trim();
  if (!right) {
    return left;
  }
  if (!left) {
    return right;
  }
  const leftLower = left.toLowerCase();
  const rightLower = right.toLowerCase();
  if (leftLower === rightLower) {
    return left;
  }
  if (rightLower.includes(leftLower) && right.length > left.length + 6) {
    return right;
  }
  if (leftLower.includes(rightLower) && right.length + 6 < left.length) {
    return left;
  }
  const overlap = overlapChars(leftLower, rightLower, 120);
  if (overlap >= 12) {
    const trimmed = right.slice(overlap).replace(/^[\s,.;:-]+/, "").trim();
    if (!trimmed) {
      return left;
    }
    return `${left} ${trimmed}`.trim();
  }
  return `${left} ${right}`.trim();
}

function tokenizeForSignal(text) {
  return String(text || "")
    .toLowerCase()
    .match(/[\p{L}\p{N}']+/gu) || [];
}

function signalStats(text) {
  const raw = String(text || "");
  const tokens = tokenizeForSignal(raw);
  const tokenCount = tokens.length;
  const frequency = new Map();
  tokens.forEach((token) => frequency.set(token, (frequency.get(token) || 0) + 1));
  let topTokenCount = 0;
  frequency.forEach((count) => {
    if (count > topTokenCount) {
      topTokenCount = count;
    }
  });
  const alphaTokens = tokens.filter((token) => /\p{L}/u.test(token));
  const numericTokens = tokens.filter((token) => /^[\p{N}]+(?:[.,:][\p{N}]+)*$/u.test(token));
  const letterChars = (raw.match(/\p{L}/gu) || []).length;
  const digitChars = (raw.match(/\p{N}/gu) || []).length;
  return {
    tokenCount,
    alphaTokenCount: alphaTokens.length,
    numericTokenCount: numericTokens.length,
    uniqueRatio: tokenCount ? frequency.size / tokenCount : 0,
    topTokenRatio: tokenCount ? topTokenCount / tokenCount : 0,
    numericRatio: tokenCount ? numericTokens.length / tokenCount : 0,
    alphaCharRatio: letterChars + digitChars > 0 ? letterChars / (letterChars + digitChars) : 0,
  };
}

function groupSignalText(group) {
  return (group.lines || []).map((line) => String(line.text || "").trim()).join(" ").trim();
}

function durationSeconds(startedAt, endedAt) {
  const startMs = timeValue(startedAt);
  const endMs = timeValue(endedAt);
  if (!startMs || !endMs || endMs <= startMs) {
    return 0;
  }
  return Math.round((endMs - startMs) / 1000);
}

function classifyMeetingGroup(group, tinyThresholdSeconds) {
  const text = groupSignalText(group);
  const seconds = durationSeconds(group.started_at, group.ended_at);
  const stats = signalStats(text);
  const tiny = seconds > 0 && seconds <= tinyThresholdSeconds;
  const sparseShort = seconds <= Math.max(45, tinyThresholdSeconds * 3) && stats.alphaTokenCount < 8;
  const numericHeavy = stats.numericRatio >= 0.58 && stats.alphaTokenCount < 26;
  const repetitive = stats.topTokenRatio >= 0.24 && stats.uniqueRatio <= 0.42 && stats.alphaTokenCount < 30;
  const veryLowLanguageSignal = stats.alphaCharRatio < 0.22 && stats.alphaTokenCount < 18;
  const almostEmpty = text.length < 40;
  const hide = tiny || sparseShort || numericHeavy || repetitive || veryLowLanguageSignal || almostEmpty;
  let reason = "";
  if (tiny) {
    reason = "short";
  } else if (sparseShort || almostEmpty) {
    reason = "low-speech";
  } else if (numericHeavy) {
    reason = "numeric";
  } else if (repetitive) {
    reason = "repetitive";
  } else if (veryLowLanguageSignal) {
    reason = "low-language";
  }
  return { hide, reason, seconds };
}

function filterMeetingGroups(groups) {
  if (!hideTinySegmentsEnabled()) {
    return { visibleGroups: groups, hiddenCount: 0 };
  }
  const tinyThreshold = tinySegmentThresholdSecondsValue();
  const visibleGroups = [];
  let hiddenCount = 0;
  groups.forEach((group) => {
    const classification = classifyMeetingGroup(group, tinyThreshold);
    if (classification.hide) {
      hiddenCount += 1;
      return;
    }
    visibleGroups.push(group);
  });
  return { visibleGroups, hiddenCount };
}

function previewTextForGroup(group, maxChars = 360) {
  const plain = groupCleanText(group).replace(/\s+/g, " ").trim();
  if (!plain) {
    return "(No transcript text)";
  }
  if (plain.length <= maxChars) {
    return plain;
  }
  return `${plain.slice(0, maxChars).trimEnd()}...`;
}

function previewTextForSessionGroup(group, maxChars = 360) {
  const chapters =
    Array.isArray(group.chapter_groups) && group.chapter_groups.length
      ? group.chapter_groups
      : buildSessionChapterGroups(group, { applyNoiseFilter: true });
  if (!chapters.length) {
    return previewTextForGroup(group, maxChars);
  }
  let best = chapters[0];
  let bestScore = -Infinity;
  chapters.forEach((chapter) => {
    const stats = signalStats(groupSignalText(chapter));
    const score = stats.alphaTokenCount * 2 + stats.uniqueRatio * 10 - stats.numericRatio * 8;
    if (score > bestScore) {
      best = chapter;
      bestScore = score;
    }
  });
  return previewTextForGroup(best, maxChars);
}

function chronologicalItems(items) {
  return [...items].sort((a, b) => {
    const startDiff = timeValue(a.started_at) - timeValue(b.started_at);
    if (startDiff !== 0) {
      return startDiff;
    }
    return Number(a.id) - Number(b.id);
  });
}

function uniqueById(items) {
  const map = new Map();
  items.forEach((item) => map.set(Number(item.id), item));
  return [...map.values()].sort((a, b) => Number(b.id) - Number(a.id));
}

function buildTurns(items, mergeGapSeconds = 2.5) {
  const turns = [];
  const ordered = chronologicalItems(items);
  ordered.forEach((item) => {
    const sourceId = String(item.source_id || "").trim();
    const speaker = speakerLabel(item);
    const text = String(item.text || "").trim();
    if (!text) {
      return;
    }
    const last = turns.length ? turns[turns.length - 1] : null;
    if (
      last &&
      last.source_id === sourceId &&
      (last.speaker || "") === (speaker || "") &&
      timeValue(item.started_at) <= timeValue(last.ended_at) + mergeGapSeconds * 1000
    ) {
      last.text = mergeChunkText(last.text, text);
      if (timeValue(item.ended_at) > timeValue(last.ended_at)) {
        last.ended_at = item.ended_at;
      }
      last.ids.push(Number(item.id));
      if (item.session_id && !last.session_id) {
        last.session_id = item.session_id;
      }
      return;
    }
    turns.push({
      id: Number(item.id),
      ids: [Number(item.id)],
      source_id: sourceId,
      speaker: speaker || null,
      session_id: item.session_id || null,
      started_at: item.started_at,
      ended_at: item.ended_at,
      text,
    });
  });
  return turns;
}

function buildSessionGroups(items, fallbackGapSeconds = 90) {
  const realSessionGroups = new Map();
  const syntheticTrackers = new Map();
  const syntheticGroups = [];

  chronologicalItems(items).forEach((item) => {
    const source = String(item.source_id || "").trim() || "unknown-source";
    const sessionId = String(item.session_id || "").trim() || null;
    const startedMs = timeValue(item.started_at);
    const endedMs = timeValue(item.ended_at);

    // Real source session IDs are authoritative: one group per source/session pair.
    if (sessionId) {
      const key = `session:${source}:${sessionId}`;
      let group = realSessionGroups.get(key);
      if (!group) {
        group = {
          key,
          source_id: source,
          session_id: sessionId,
          started_at: item.started_at,
          ended_at: item.ended_at,
          chunks: [],
        };
        realSessionGroups.set(key, group);
      }
      group.chunks.push(item);
      if (startedMs < timeValue(group.started_at)) {
        group.started_at = item.started_at;
      }
      if (endedMs > timeValue(group.ended_at)) {
        group.ended_at = item.ended_at;
      }
      return;
    }

    // Synthetic fallback only for chunks without a real session_id.
    const tracker = syntheticTrackers.get(source) || { index: 0, current: null };
    const gapSplit =
      tracker.current && startedMs > timeValue(tracker.current.ended_at) + fallbackGapSeconds * 1000;
    if (!tracker.current || gapSplit) {
      if (tracker.current) {
        syntheticGroups.push(tracker.current);
      }
      const syntheticKey = `session:${source}:synthetic:${tracker.index}`;
      tracker.current = {
        key: syntheticKey,
        source_id: source,
        session_id: null,
        started_at: item.started_at,
        ended_at: item.ended_at,
        chunks: [],
      };
      tracker.index += 1;
    }
    tracker.current.chunks.push(item);
    if (startedMs < timeValue(tracker.current.started_at)) {
      tracker.current.started_at = item.started_at;
    }
    if (endedMs > timeValue(tracker.current.ended_at)) {
      tracker.current.ended_at = item.ended_at;
    }
    syntheticTrackers.set(source, tracker);
  });

  syntheticTrackers.forEach((tracker) => {
    if (tracker.current) {
      syntheticGroups.push(tracker.current);
    }
  });

  const groups = [...realSessionGroups.values(), ...syntheticGroups];
  groups.sort((a, b) => timeValue(b.ended_at) - timeValue(a.ended_at));
  groups.forEach((group) => {
    group.lines = buildTurns(group.chunks, 2.5);
    group.chunk_ids = group.chunks.map((item) => Number(item.id));
    group.source_ids = [group.source_id];
    group.speaker_count = new Set(group.lines.map((line) => `${line.source_id}/${line.speaker || ""}`)).size;
    group.chapter_groups = [];
    group.chapter_count = 0;
  });
  return groups;
}

function buildMeetingGroups(items, options = {}) {
  const gapSeconds = Number(options.gapSeconds || 75);
  const maxMinutes = Number(options.maxMinutes || 30);
  const maxChars = Number(options.maxChars || 18000);
  const maxChunks = Number(options.maxChunks || 350);
  const maxDurationMs = maxMinutes * 60 * 1000;
  const ordered = chronologicalItems(items);
  const groups = [];
  let current = null;

  ordered.forEach((item) => {
    const startedMs = timeValue(item.started_at);
    const endedMs = timeValue(item.ended_at);
    const text = String(item.text || "").trim();
    if (!current) {
      current = {
        started_at: item.started_at,
        ended_at: item.ended_at,
        chunks: [item],
        char_count: text.length,
      };
      return;
    }
    const currentEndMs = timeValue(current.ended_at);
    const currentStartMs = timeValue(current.started_at);
    const gapSplit = startedMs > currentEndMs + gapSeconds * 1000;
    const durationSplit = endedMs > currentStartMs + maxDurationMs;
    const charsSplit = current.char_count + text.length > maxChars;
    const chunkSplit = current.chunks.length >= maxChunks;

    const previousChunk = current.chunks.length ? current.chunks[current.chunks.length - 1] : null;
    const previousSession = String(previousChunk?.session_id || "").trim();
    const thisSession = String(item.session_id || "").trim();
    const sameSource = String(previousChunk?.source_id || "").trim() === String(item.source_id || "").trim();
    const sessionBoundarySplit =
      sameSource &&
      Boolean(previousSession || thisSession) &&
      previousSession !== thisSession &&
      startedMs >= currentEndMs;

    const sameMeeting = !(gapSplit || durationSplit || charsSplit || chunkSplit || sessionBoundarySplit);
    if (!sameMeeting) {
      groups.push(current);
      current = {
        started_at: item.started_at,
        ended_at: item.ended_at,
        chunks: [item],
        char_count: text.length,
      };
      return;
    }
    current.chunks.push(item);
    current.char_count += text.length;
    if (endedMs > currentEndMs) {
      current.ended_at = item.ended_at;
    }
  });

  if (current) {
    groups.push(current);
  }

  const enriched = groups.map((group) => {
    const chunkIds = group.chunks.map((item) => Number(item.id)).sort((a, b) => a - b);
    const firstId = chunkIds[0] || 0;
    const lastId = chunkIds[chunkIds.length - 1] || 0;
    const key = `meeting:${firstId}:${lastId}`;
    const sourceIds = [...new Set(group.chunks.map((item) => String(item.source_id || "").trim()).filter(Boolean))];
    const lines = buildTurns(group.chunks, 2.5);
    return {
      key,
      started_at: group.started_at,
      ended_at: group.ended_at,
      chunks: group.chunks,
      chunk_ids: chunkIds,
      source_ids: sourceIds,
      lines,
      speaker_count: new Set(lines.map((line) => `${line.source_id}/${line.speaker || ""}`)).size,
      duration: formatDuration(group.started_at, group.ended_at),
    };
  });

  enriched.sort((a, b) => timeValue(b.ended_at) - timeValue(a.ended_at));
  return enriched;
}

function defaultTitleForGroup(group, mode) {
  const start = new Date(group.started_at);
  const dateLabel = Number.isNaN(start.getTime())
    ? "Unknown Date"
    : start.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  const timeLabel = Number.isNaN(start.getTime())
    ? ""
    : start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  if (mode === GROUPING_MODE.SESSION) {
    const source = String(group.source_id || "source");
    return `${source} Session ${dateLabel}${timeLabel ? ` ${timeLabel}` : ""}`;
  }
  const sourceSummary =
    group.source_ids && group.source_ids.length
      ? group.source_ids.length === 1
        ? group.source_ids[0]
        : `${group.source_ids.length} sources`
      : "meeting";
  return `${sourceSummary} ${dateLabel}${timeLabel ? ` ${timeLabel}` : ""}`;
}

function titleForGroup(group, mode) {
  const generated = state.generatedTitles[group.key];
  if (generated) {
    return generated;
  }
  return defaultTitleForGroup(group, mode);
}

function groupLinesText(group) {
  return (group.lines || [])
    .map((line) => {
      const who = line.speaker ? `${line.source_id}/${line.speaker}` : line.source_id;
      return `[${formatRange(line.started_at, line.ended_at)}] ${who}: ${line.text}`;
    })
    .join("\n");
}

function normalizeTranscriptText(value) {
  return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function dedupeConsecutiveTexts(values) {
  const out = [];
  for (const value of values || []) {
    const text = String(value || "").trim();
    if (!text) {
      continue;
    }
    if (!out.length) {
      out.push(text);
      continue;
    }
    const previous = out[out.length - 1];
    const normalized = normalizeTranscriptText(text);
    const previousNormalized = normalizeTranscriptText(previous);
    if (!normalized || normalized === previousNormalized) {
      continue;
    }
    // Keep the richer variant when one line is a near-superset of the other.
    if (normalized.length > 90 && normalized.includes(previousNormalized)) {
      out[out.length - 1] = text;
      continue;
    }
    if (previousNormalized.length > 90 && previousNormalized.includes(normalized)) {
      continue;
    }
    out.push(text);
  }
  return out;
}

function dedupeConsecutiveLines(lines) {
  const out = [];
  for (const line of lines || []) {
    const text = String(line?.text || "").trim();
    if (!text) {
      continue;
    }
    const normalized = normalizeTranscriptText(text);
    if (!out.length) {
      out.push({ ...line, text });
      continue;
    }
    const previous = out[out.length - 1];
    const previousNormalized = normalizeTranscriptText(previous.text);
    if (!normalized || normalized === previousNormalized) {
      continue;
    }
    if (normalized.length > 90 && normalized.includes(previousNormalized)) {
      out[out.length - 1] = { ...line, text };
      continue;
    }
    if (previousNormalized.length > 90 && previousNormalized.includes(normalized)) {
      continue;
    }
    out.push({ ...line, text });
  }
  return out;
}

function formatPlainTranscriptLine(line, options = {}) {
  const text = String(line?.text || "").trim();
  if (!text) {
    return "";
  }
  const includeTimestamps = options.timestamps !== false;
  const includeSpeakers = options.speakers !== false;
  const who = line?.speaker ? `${line.source_id}/${line.speaker}` : String(line?.source_id || "").trim();
  if (includeTimestamps && includeSpeakers && who) {
    return `[${formatRange(line.started_at, line.ended_at)}] ${who}: ${text}`;
  }
  if (includeTimestamps) {
    return `[${formatRange(line.started_at, line.ended_at)}] ${text}`;
  }
  if (includeSpeakers && who) {
    return `${who}: ${text}`;
  }
  return text;
}

function groupPlainTranscriptText(group, options = {}) {
  const deduped = dedupeConsecutiveLines(group.lines || []);
  return deduped
    .map((line) => formatPlainTranscriptLine(line, options))
    .filter(Boolean)
    .join("\n")
    .trim();
}

function groupCleanText(group) {
  return groupPlainTranscriptText(group, { timestamps: false, speakers: false });
}

function groupHeaderLine(group, mode) {
  return `[${formatRange(group.started_at, group.ended_at)}] ${titleForGroup(group, mode)}`;
}

function groupPlainText(group, mode, includeHeader = true) {
  const lines = groupLinesText(group);
  if (!includeHeader) {
    return lines;
  }
  const sourceSummary = `${(group.source_ids || []).length} source${
    (group.source_ids || []).length === 1 ? "" : "s"
  }`;
  const speakerSummary = `${group.speaker_count || 0} speaker${group.speaker_count === 1 ? "" : "s"}`;
  const chunkSummary = `${(group.chunks || []).length} chunk${(group.chunks || []).length === 1 ? "" : "s"}`;
  const sessionLine = mode === GROUPING_MODE.SESSION && group.session_id ? `\nSession: ${group.session_id}` : "";
  const calendarMatch = groupCalendarMatch(group);
  const calendarLine = calendarMatch
    ? `\nCalendar: ${calendarMatch.title || "(Untitled meeting)"} | ${formatRange(
        calendarMatch.started_at,
        calendarMatch.ended_at
      )}`
    : "";
  return `${groupHeaderLine(group, mode)}
Begin: ${formatTimestamp(group.started_at)}
End: ${formatTimestamp(group.ended_at)}
Duration: ${formatDuration(group.started_at, group.ended_at)}
Counts: ${sourceSummary} | ${speakerSummary} | ${chunkSummary}${sessionLine}${calendarLine}
${lines}`.trim();
}

function groupCalendarMatch(group) {
  const candidates = [];
  for (const chunk of group.chunks || []) {
    if (chunk && typeof chunk === "object" && chunk.calendar_match) {
      candidates.push(chunk.calendar_match);
    }
  }
  if (!candidates.length) {
    return null;
  }
  const byEvent = new Map();
  for (const match of candidates) {
    const eventKey = String(match.event_id || "").trim() || "__unknown__";
    const existing = byEvent.get(eventKey);
    if (!existing) {
      byEvent.set(eventKey, {
        match,
        count: 1,
        overlapSeconds: Number(match.overlap_seconds || 0),
        score: Number(match.match_score || 0),
      });
      continue;
    }
    existing.count += 1;
    existing.overlapSeconds += Number(match.overlap_seconds || 0);
    existing.score += Number(match.match_score || 0);
    // Keep the strongest representative for links/timestamps.
    if (Number(match.overlap_seconds || 0) > Number(existing.match.overlap_seconds || 0)) {
      existing.match = match;
    }
  }
  const ranked = [...byEvent.values()].sort((left, right) => {
    if (right.count !== left.count) {
      return right.count - left.count;
    }
    if (right.overlapSeconds !== left.overlapSeconds) {
      return right.overlapSeconds - left.overlapSeconds;
    }
    return right.score - left.score;
  });
  return ranked[0]?.match || candidates[0];
}

function sampleIdsEvenly(ids, limit = 240) {
  const ordered = Array.isArray(ids)
    ? [...ids].map((value) => Number(value)).filter((value) => Number.isFinite(value)).sort((a, b) => a - b)
    : [];
  if (ordered.length <= limit) {
    return ordered;
  }
  const out = [];
  const step = ordered.length / limit;
  for (let index = 0; index < limit; index += 1) {
    const sampleIndex = Math.min(ordered.length - 1, Math.floor(index * step));
    out.push(ordered[sampleIndex]);
  }
  return [...new Set(out)];
}

function buildViewModel() {
  const mode = groupingModeValue();
  const plainOptions = plainTextOptions();
  if (mode === GROUPING_MODE.RAW) {
    const ordered = [...state.items].sort((a, b) => timeValue(b.ended_at) - timeValue(a.ended_at));
    return {
      mode,
      groups: [],
      lines: [],
      rawItems: ordered,
      plainText: chronologicalItems(ordered)
        .map((item) =>
          formatPlainTranscriptLine(
            {
              source_id: item.source_id,
              speaker: speakerLabel(item),
              started_at: item.started_at,
              ended_at: item.ended_at,
              text: item.text,
            },
            plainOptions
          )
        )
        .filter(Boolean)
        .join("\n"),
      summary: `${ordered.length} chunks`,
    };
  }

  if (mode === GROUPING_MODE.SMART) {
    const turns = buildTurns(state.items, 2.2).sort((a, b) => timeValue(b.ended_at) - timeValue(a.ended_at));
    return {
      mode,
      groups: [],
      lines: turns,
      rawItems: [],
      plainText: [...turns]
        .sort((a, b) => timeValue(a.started_at) - timeValue(b.started_at))
        .map((line) => formatPlainTranscriptLine(line, plainOptions))
        .filter(Boolean)
        .join("\n"),
      summary: `${turns.length} turns`,
    };
  }

  const groups =
    mode === GROUPING_MODE.SESSION
      ? buildSessionGroups(state.items, meetingGapSecondsValue())
      : buildMeetingGroups(state.items, {
          gapSeconds: meetingGapSecondsValue(),
          maxMinutes: meetingMaxMinutesValue(),
          maxChars: 18000,
          maxChunks: 350,
        });

  const totalGroupCount = groups.length;
  let outputGroups = groups;
  let hiddenGroupCount = 0;
  if (mode === GROUPING_MODE.MEETING) {
    const filtered = filterMeetingGroups(groups);
    outputGroups = filtered.visibleGroups;
    hiddenGroupCount = filtered.hiddenCount;
  } else if (mode === GROUPING_MODE.SESSION) {
    outputGroups = outputGroups.map((group) => {
      const chapterGroups = buildSessionChapterGroups(group, { applyNoiseFilter: true });
      group.chapter_groups = chapterGroups;
      group.chapter_count = chapterGroups.length;
      return group;
    });
    if (hideTinySegmentsEnabled()) {
      const before = outputGroups.length;
      outputGroups = outputGroups.filter((group) => {
        if ((group.chapter_count || 0) > 0) {
          return true;
        }
        const stats = signalStats(groupSignalText(group));
        return stats.alphaTokenCount >= 18;
      });
      hiddenGroupCount = before - outputGroups.length;
    }
  }

  const plainText = [...outputGroups]
    .sort((a, b) => timeValue(a.started_at) - timeValue(b.started_at))
    .map((group) => {
      if (mode === GROUPING_MODE.SESSION) {
        return sessionCleanText(group, plainOptions);
      }
      return groupPlainTranscriptText(group, plainOptions);
    })
    .filter(Boolean)
    .join("\n\n");

  const chunkCount = outputGroups.reduce((sum, group) => sum + group.chunks.length, 0);
  const baseLabel = mode === GROUPING_MODE.SESSION ? "source sessions" : "meetings";
  const visibleSummary =
    outputGroups.length === totalGroupCount
      ? `${outputGroups.length} ${baseLabel}`
      : `${outputGroups.length} of ${totalGroupCount} ${baseLabel}`;
  const hiddenSummary =
    hiddenGroupCount > 0
      ? mode === GROUPING_MODE.SESSION
        ? ` • ${hiddenGroupCount} low-signal sessions hidden`
        : ` • ${hiddenGroupCount} short/noise hidden`
      : "";
  return {
    mode,
    groups: outputGroups,
    allGroups: groups,
    hiddenGroupCount,
    lines: [],
    rawItems: [],
    plainText,
    summary: `${visibleSummary} • ${chunkCount} chunks${hiddenSummary}`,
  };
}

function render() {
  if (!state.items.length) {
    el.list.innerHTML = '<div class="entry"><p class="text muted">No transcript entries loaded yet.</p></div>';
    el.meta.textContent = "No entries loaded.";
    el.plainText.value = "";
    state.lastView = null;
    renderGoogleCalendarEvents();
    return;
  }

  const view = buildViewModel();
  state.lastView = view;
  const mode = view.mode;

  if (mode === GROUPING_MODE.RAW) {
    el.list.innerHTML = view.rawItems
      .map((item) => {
        const speaker = speakerLabel(item);
        const sessionPart = item.session_id ? ` | session ${item.session_id}` : "";
        const meta = `${item.source_id}${speaker ? ` | speaker ${speaker}` : ""} | ${formatRange(
          item.started_at,
          item.ended_at
        )}${sessionPart} | chunk #${item.id}`;
        return `
          <article class="entry" id="${escapeHtml(chunkDomId(item.id))}" data-chunk-id="${Number(item.id)}">
            <div class="meta">${escapeHtml(meta)}</div>
            <p class="text">${escapeHtml(item.text)}</p>
          </article>
        `;
      })
      .join("");
  } else if (mode === GROUPING_MODE.SMART) {
    el.list.innerHTML = view.lines
      .map((line) => {
        const who = line.speaker ? `${line.source_id}/${line.speaker}` : line.source_id;
        return `
          <article class="entry">
            <div class="meta">${escapeHtml(`${who} | ${formatRange(line.started_at, line.ended_at)} | ${line.ids.length} chunk${line.ids.length === 1 ? "" : "s"}`)}</div>
            <p class="text">${escapeHtml(line.text)}</p>
          </article>
        `;
      })
      .join("");
  } else {
    if (!view.groups.length) {
      const message =
        mode === GROUPING_MODE.MEETING && view.hiddenGroupCount > 0
          ? `All ${view.hiddenGroupCount} meeting segments are hidden by the short/noise filter. Disable filtering to review all segments.`
          : mode === GROUPING_MODE.SESSION && view.hiddenGroupCount > 0
            ? `All ${view.hiddenGroupCount} sessions are currently low-signal and hidden. Disable filtering to inspect raw sessions.`
            : "No grouped transcript entries available for this view.";
      el.list.innerHTML = `<article class="entry"><p class="text muted">${escapeHtml(message)}</p></article>`;
      el.meta.textContent = `${view.summary}${state.hasMore ? " • older entries available" : ""} • ${groupingLabel(mode)}.`;
      el.plainText.value = view.plainText;
      return;
    }
    el.list.innerHTML = view.groups
      .map((group) => {
        const heading = titleForGroup(group, mode);
        const begin = `Begin: ${formatTimestamp(group.started_at)}`;
        const end = `End: ${formatTimestamp(group.ended_at)}`;
        const duration = `Duration: ${formatDuration(group.started_at, group.ended_at)}`;
        const counts = `${group.source_ids.length} source${
          group.source_ids.length === 1 ? "" : "s"
        } | ${group.speaker_count} speaker${group.speaker_count === 1 ? "" : "s"} | ${group.chunks.length} chunk${
          group.chunks.length === 1 ? "" : "s"
        }${
          mode === GROUPING_MODE.SESSION
            ? ` | ${(group.chapter_count || 0)} chapter${group.chapter_count === 1 ? "" : "s"}`
            : ""
        }${mode === GROUPING_MODE.SESSION && group.session_id ? ` | session ${group.session_id}` : ""}`;
        const lines = groupLinesText(group);
        const preview =
          mode === GROUPING_MODE.SESSION ? previewTextForSessionGroup(group) : previewTextForGroup(group);
        const calendarMatch = groupCalendarMatch(group);
        const calendarLine = calendarMatch
          ? `<p class="text muted">Calendar: ${escapeHtml(
              `${calendarMatch.title || "(Untitled meeting)"} • ${formatRange(
                calendarMatch.started_at,
                calendarMatch.ended_at
              )}`
            )}</p>`
          : "";
        const calendarLink = calendarMatch?.html_link
          ? `<a class="ghost-btn" href="${escapeHtml(calendarMatch.html_link)}" target="_blank" rel="noreferrer">Open Event</a>`
          : "";
        const joinLink = calendarMatch?.hangout_link
          ? `<a class="ghost-btn" href="${escapeHtml(calendarMatch.hangout_link)}" target="_blank" rel="noreferrer">Join Link</a>`
          : "";
        const fullCopyLabel = mode === GROUPING_MODE.SESSION ? "Copy Session" : "Copy Meeting";
        const chapterCopyButton =
          mode === GROUPING_MODE.SESSION && (group.chapter_count || 0) > 0
            ? `<button class="ghost-btn" type="button" data-copy-group="${escapeHtml(group.key)}" data-copy-kind="chapters">Copy Chapters</button>`
            : "";
        return `
          <article class="entry" id="${escapeHtml(groupDomId(group.key))}" data-group-key="${escapeHtml(group.key)}">
            <div class="source-row">
              <div class="source-main">
                <div class="meta">${escapeHtml(heading)}</div>
              </div>
              <div class="source-row-actions">
                <button class="ghost-btn" type="button" data-copy-group="${escapeHtml(group.key)}" data-copy-kind="full">${fullCopyLabel}</button>
                <button class="ghost-btn" type="button" data-copy-group="${escapeHtml(group.key)}" data-copy-kind="text">Copy Text</button>
                ${chapterCopyButton}
                ${calendarLink}
                ${joinLink}
              </div>
            </div>
            <p class="text muted">${escapeHtml(begin)}</p>
            <p class="text muted">${escapeHtml(end)}</p>
            <p class="text muted">${escapeHtml(duration)}</p>
            <p class="text muted">${escapeHtml(counts)}</p>
            ${calendarLine}
            <p class="text">${escapeHtml(preview)}</p>
            <details class="transcript-details">
              <summary>Show full transcript</summary>
              <pre>${escapeHtml(lines)}</pre>
            </details>
          </article>
        `;
      })
      .join("");
  }

  el.meta.textContent = `${view.summary}${state.hasMore ? " • older entries available" : ""} • ${groupingLabel(mode)}.`;
  el.plainText.value = view.plainText;
  renderGoogleCalendarEvents();
}

function loadedPlainText() {
  if (!state.items.length) {
    return "";
  }
  return buildViewModel().plainText;
}

function overlappingChunksForEvent(event) {
  const eventStart = timeValue(event?.start_at);
  const eventEnd = timeValue(event?.end_at);
  if (!eventStart || !eventEnd || eventEnd <= eventStart) {
    return [];
  }
  return state.items.filter((item) => {
    const itemStart = timeValue(item?.started_at);
    const itemEnd = timeValue(item?.ended_at);
    if (!itemStart || !itemEnd || itemEnd <= itemStart) {
      return false;
    }
    return intervalOverlapMs(itemStart, itemEnd, eventStart, eventEnd) > 0;
  });
}

function bestGroupForEvent(event) {
  const view = state.lastView;
  if (!view || !Array.isArray(view.groups) || !view.groups.length) {
    return null;
  }
  const eventStart = timeValue(event?.start_at);
  const eventEnd = timeValue(event?.end_at);
  if (!eventStart || !eventEnd || eventEnd <= eventStart) {
    return null;
  }
  let best = null;
  let bestOverlap = 0;
  for (const group of view.groups) {
    const groupStart = timeValue(group?.started_at);
    const groupEnd = timeValue(group?.ended_at);
    if (!groupStart || !groupEnd || groupEnd <= groupStart) {
      continue;
    }
    const overlap = intervalOverlapMs(groupStart, groupEnd, eventStart, eventEnd);
    if (overlap <= 0) {
      continue;
    }
    if (overlap > bestOverlap) {
      best = group;
      bestOverlap = overlap;
    }
  }
  return best;
}

function normalizeCalendarEvents(events) {
  if (!Array.isArray(events)) {
    return [];
  }
  const normalized = events
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      ...item,
      event_id: String(item.event_id || "").trim(),
    }));
  normalized.sort((left, right) => timeValue(left.start_at) - timeValue(right.start_at));
  return normalized;
}

function renderGoogleCalendarEvents() {
  if (!el.googleSyncEvents || !el.googleSyncEventsMeta) {
    return;
  }
  const events = Array.isArray(state.googleCalendarEvents) ? state.googleCalendarEvents : [];
  if (!events.length) {
    el.googleSyncEventsMeta.textContent = "No calendar events loaded.";
    el.googleSyncEvents.innerHTML = "";
    return;
  }

  const overlapChunks = events.reduce((sum, event) => sum + overlappingChunksForEvent(event).length, 0);
  el.googleSyncEventsMeta.textContent = `${events.length} synced event${
    events.length === 1 ? "" : "s"
  } • ${overlapChunks} overlapping loaded chunk${overlapChunks === 1 ? "" : "s"}.`;

  el.googleSyncEvents.innerHTML = events
    .map((event, index) => {
      const title = String(event.summary || "").trim() || "(Untitled meeting)";
      const overlaps = overlappingChunksForEvent(event);
      const overlapCount = overlaps.length;
      const linkedCount = overlaps.filter(
        (item) => String(item?.calendar_match?.event_id || "").trim() === String(event.event_id || "").trim()
      ).length;
      const group = bestGroupForEvent(event);
      const focusButton = group || overlapCount > 0
        ? `<button class="ghost-btn" type="button" data-focus-event="${escapeHtml(eventKey(event, index))}">Focus Transcript</button>`
        : "";
      const openLink = event.html_link
        ? `<a class="ghost-btn" href="${escapeHtml(event.html_link)}" target="_blank" rel="noreferrer">Open Event</a>`
        : "";
      const joinLink = event.hangout_link
        ? `<a class="ghost-btn" href="${escapeHtml(event.hangout_link)}" target="_blank" rel="noreferrer">Join Link</a>`
        : "";
      const overlapLine =
        overlapCount > 0
          ? `Transcript overlap: ${overlapCount} chunk${overlapCount === 1 ? "" : "s"}${
              linkedCount > 0 ? ` (${linkedCount} linked by calendar match)` : ""
            }`
          : "Transcript overlap: none in currently loaded transcripts";
      return `
        <article class="entry">
          <div class="meta">${escapeHtml(title)} | ${escapeHtml(formatRange(event.start_at, event.end_at))}</div>
          <p class="text muted">${escapeHtml(overlapLine)}</p>
          <div class="source-row-actions">
            ${focusButton}
            ${openLink}
            ${joinLink}
          </div>
        </article>
      `;
    })
    .join("");
}

function flashFocusedElement(target) {
  if (!(target instanceof HTMLElement)) {
    return;
  }
  target.classList.add("focus-highlight");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  window.setTimeout(() => {
    target.classList.remove("focus-highlight");
  }, 1400);
}

function focusTranscriptForEvent(eventRef) {
  const events = Array.isArray(state.googleCalendarEvents) ? state.googleCalendarEvents : [];
  const event = events.find((item, index) => eventKey(item, index) === eventRef);
  if (!event) {
    setStatus("Calendar event is no longer available in the loaded list.", true);
    return;
  }

  const group = bestGroupForEvent(event);
  if (group) {
    const target = document.getElementById(groupDomId(group.key));
    if (target) {
      flashFocusedElement(target);
      setStatus(`Focused transcript overlap for "${String(event.summary || "(Untitled meeting)")}".`);
      return;
    }
  }

  const overlaps = overlappingChunksForEvent(event);
  if (overlaps.length > 0) {
    const target = document.getElementById(chunkDomId(overlaps[0].id));
    if (target) {
      flashFocusedElement(target);
      setStatus(`Focused transcript chunk overlap for "${String(event.summary || "(Untitled meeting)")}".`);
      return;
    }
  }

  setStatus("No overlapping transcript is visible in the current view.", true);
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

function renderGoogleSyncStatus(statusPayload) {
  const target = el.googleSyncStatus;
  if (!target) {
    return;
  }
  if (!statusPayload || typeof statusPayload !== "object") {
    target.textContent = "Google sync status unavailable.";
    return;
  }
  const reachable = Boolean(statusPayload.reachable);
  const connected = Boolean(statusPayload.connected);
  const enabled = Boolean(statusPayload.enabled);
  const cached = Boolean(statusPayload.calendar_cached);
  const count = Number(statusPayload.calendar_event_count);
  const syncedAt = statusPayload.calendar_synced_at;
  const syncedLabel = syncedAt ? `${formatTimestamp(syncedAt)} (${formatRelativeAge(syncedAt)})` : "never";
  const scopes = Array.isArray(statusPayload.scopes) ? statusPayload.scopes.length : 0;

  if (!reachable) {
    target.textContent =
      "Google sync service is offline. Start `google-sync-service` to enable calendar matching.";
  } else if (!connected) {
    target.textContent = "Google account not connected yet. Click Connect Google (one-time).";
  } else if (!cached) {
    target.textContent = `Connected (scopes: ${scopes}). Calendar not synced yet. Click Sync Calendar.`;
  } else {
    const eventLabel = Number.isFinite(count) ? `${count} event${count === 1 ? "" : "s"}` : "events";
    const enabledText = enabled ? "matching enabled" : "matching disabled";
    target.textContent = `Connected • ${eventLabel} cached • last sync ${syncedLabel} • ${enabledText}.`;
  }

  if (statusPayload.error && reachable === false) {
    target.textContent += ` (${statusPayload.error})`;
  }

  if (el.googleSyncNow) {
    el.googleSyncNow.disabled = !reachable || !connected;
  }
}

async function loadGoogleSyncStatus(options = {}) {
  const silent = Boolean(options.silent);
  try {
    const payload = await request("/v1/google-sync/status");
    state.googleSync = payload;
    renderGoogleSyncStatus(payload);
    if (!payload?.reachable || !payload?.connected) {
      state.googleCalendarEvents = [];
      renderGoogleCalendarEvents();
    }
    return payload;
  } catch (error) {
    if (!silent && el.googleSyncStatus) {
      el.googleSyncStatus.textContent = `Calendar status check failed: ${error.message}`;
    }
    return null;
  }
}

async function loadGoogleCalendarEvents(options = {}) {
  const silent = Boolean(options.silent);
  try {
    const payload = await request("/v1/google-sync/calendar/latest");
    state.googleCalendarEvents = normalizeCalendarEvents(payload?.events || []);
    renderGoogleCalendarEvents();
    return state.googleCalendarEvents;
  } catch (error) {
    state.googleCalendarEvents = [];
    renderGoogleCalendarEvents();
    if (!silent && el.googleSyncEventsMeta) {
      el.googleSyncEventsMeta.textContent = `Calendar event list unavailable: ${error.message}`;
    }
    return [];
  }
}

async function connectGoogleSync() {
  const fallbackBase = state.googleSync?.service_url
    ? String(state.googleSync.service_url).replace(/\/+$/, "")
    : "http://127.0.0.1:8792";
  const connectUrl = "/v1/google-sync/connect";
  let resolvedUrl = connectUrl;
  // Prefer proxy route, but fall back to direct google-sync-service connect.
  try {
    const probe = await fetch(connectUrl, { method: "HEAD", redirect: "manual" });
    if (probe.status === 404) {
      resolvedUrl = `${fallbackBase}/connect`;
    }
  } catch {
    resolvedUrl = `${fallbackBase}/connect`;
  }
  const popup = window.open(resolvedUrl, "_blank", "noopener,noreferrer");
  if (!popup) {
    window.location.href = resolvedUrl;
    return;
  }
  if (el.googleSyncStatus) {
    el.googleSyncStatus.textContent = "Google connect window opened. Finish consent, then click Refresh Calendar Status.";
  }
}

async function syncGoogleCalendarNow(options = {}) {
  const silent = Boolean(options.silent);
  const lookbackHours = googleLookbackHoursValue();
  const lookaheadHours = googleLookaheadHoursValue();
  try {
    if (!silent && el.googleSyncStatus) {
      el.googleSyncStatus.textContent = "Syncing Google Calendar...";
    }
    const payload = await request("/v1/google-sync/calendar/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lookback_hours: lookbackHours,
        lookahead_hours: lookaheadHours,
      }),
    });
    await loadGoogleSyncStatus({ silent: true });
    if (Array.isArray(payload?.events)) {
      state.googleCalendarEvents = normalizeCalendarEvents(payload.events);
      renderGoogleCalendarEvents();
    } else {
      await loadGoogleCalendarEvents({ silent: true });
    }
    if (!silent && el.googleSyncStatus) {
      const count = Number(payload?.count);
      const countLabel = Number.isFinite(count) ? `${count} event${count === 1 ? "" : "s"}` : "events";
      el.googleSyncStatus.textContent = `Calendar synced successfully (${countLabel}).`;
    }
    return payload;
  } catch (error) {
    if (!silent && el.googleSyncStatus) {
      el.googleSyncStatus.textContent = `Calendar sync failed: ${error.message}`;
    }
    return null;
  }
}

async function maybeAutoSyncCalendar() {
  if (state.googleSyncAutoSyncInFlight) {
    return;
  }
  const status = state.googleSync || (await loadGoogleSyncStatus({ silent: true }));
  if (!status || !status.reachable || !status.connected) {
    return;
  }
  const syncedAt = Date.parse(String(status.calendar_synced_at || ""));
  const tenMinutesMs = 10 * 60 * 1000;
  const stale = !Number.isFinite(syncedAt) || Date.now() - syncedAt > tenMinutesMs;
  if (!stale) {
    return;
  }
  state.googleSyncAutoSyncInFlight = true;
  try {
    await syncGoogleCalendarNow({ silent: true });
  } finally {
    state.googleSyncAutoSyncInFlight = false;
  }
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
      state.generatedTitles = {};
      state.generatedTitleAttempts = {};
      render();
      await loadSources();
      await loadGoogleSyncStatus({ silent: true });
      await loadGoogleCalendarEvents({ silent: true });
    }
    await maybeAutoSyncCalendar();
    const params = new URLSearchParams({
      limit: String(pageSizeValue()),
      since_seconds: String(sinceSecondsValue()),
      compact: "false",
      sessionized: "false",
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
    void autoGenerateTitles();
    setStatus(
      `Loaded ${newItems.length} chunks${state.hasMore ? " (more available)" : ""} at ${new Date().toLocaleTimeString()} • ${groupingLabel()}.`
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
      `Loaded ${state.items.length} chunks but stopped before end (page cap reached or pagination stalled).`,
      true
    );
    return;
  }
  setStatus(`Loaded all available chunks (${state.items.length}).`);
}

async function generateTitlesForCurrentGroups(options = {}) {
  const limit = Number(options.limit || 24);
  const force = Boolean(options.force);
  const silent = Boolean(options.silent);
  if (state.titleLoading) {
    return;
  }
  const mode = groupingModeValue();
  if (mode !== GROUPING_MODE.MEETING && mode !== GROUPING_MODE.SESSION) {
    if (!silent) {
      setStatus("Title generation is available for Meeting segments and Source sessions only.", true);
    }
    return;
  }
  const view = buildViewModel();
  const groups = view.groups || [];
  if (!groups.length) {
    if (!silent) {
      setStatus("No groups loaded yet for title generation.", true);
    }
    return;
  }

  state.titleLoading = true;
  const maxGroups = Math.min(groups.length, Math.max(1, limit));
  let generated = 0;
  let failed = 0;
  if (!silent) {
    setStatus(`Generating titles for ${maxGroups} ${mode === GROUPING_MODE.MEETING ? "meetings" : "sessions"}...`);
  }
  for (let idx = 0; idx < maxGroups; idx += 1) {
    const group = groups[idx];
    if (state.generatedTitles[group.key] && !force) {
      continue;
    }
    if (state.generatedTitleAttempts[group.key] && !force) {
      continue;
    }
    const transcriptIds = sampleIdsEvenly(group.chunk_ids || [], 240);
    if (!transcriptIds.length) {
      continue;
    }
    state.generatedTitleAttempts[group.key] = true;
    try {
      const payload = await request("/v1/transcripts/title", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript_ids: transcriptIds, max_words: 8 }),
      });
      const title = String(payload.title || "").trim();
      if (title) {
        state.generatedTitles[group.key] = title;
        generated += 1;
      } else {
        failed += 1;
      }
    } catch {
      failed += 1;
    }
  }
  state.titleLoading = false;
  render();
  if (!silent) {
    setStatus(`Title generation complete: ${generated} generated${failed > 0 ? `, ${failed} failed` : ""}.`);
  }
}

async function autoGenerateTitles() {
  const mode = groupingModeValue();
  if (mode !== GROUPING_MODE.MEETING && mode !== GROUPING_MODE.SESSION) {
    return;
  }
  const view = state.lastView || buildViewModel();
  const groups = view.groups || [];
  if (!groups.length) {
    return;
  }
  const needsTitle = groups.some(
    (group, idx) => idx < 8 && !state.generatedTitles[group.key] && !state.generatedTitleAttempts[group.key]
  );
  if (!needsTitle) {
    return;
  }
  await generateTitlesForCurrentGroups({ limit: 8, silent: true });
}

async function copyLoaded() {
  const text = loadedPlainText().trim();
  if (!text) {
    setStatus("Nothing loaded to copy.", true);
    return;
  }
  const copied = await copyTextToClipboard(text);
  if (copied) {
    const timestampLabel = plainIncludeTimestampsEnabled() ? "with timestamps" : "without timestamps";
    setStatus(`Copied ${groupingLabel().toLowerCase()} plain text (${timestampLabel}).`);
    return;
  }
  setStatus("Clipboard copy blocked by browser.", true);
}

async function copyTextToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const probe = document.createElement("textarea");
    probe.value = text;
    probe.setAttribute("readonly", "readonly");
    probe.style.position = "fixed";
    probe.style.left = "-9999px";
    probe.style.top = "0";
    document.body.appendChild(probe);
    probe.focus();
    probe.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(probe);
    return Boolean(ok);
  }
}

function buildSessionChapterGroups(sessionGroup, options = {}) {
  const applyNoiseFilter =
    typeof options.applyNoiseFilter === "boolean" ? options.applyNoiseFilter : hideTinySegmentsEnabled();
  const chapters = buildMeetingGroups(sessionGroup.chunks || [], {
    gapSeconds: Math.max(35, meetingGapSecondsValue()),
    maxMinutes: Math.max(10, Math.min(45, meetingMaxMinutesValue())),
    maxChars: 10000,
    maxChunks: 220,
  }).sort((a, b) => timeValue(a.started_at) - timeValue(b.started_at));
  if (!applyNoiseFilter) {
    return chapters;
  }
  const tinyThreshold =
    Number.isFinite(Number(options.tinyThresholdSeconds)) && Number(options.tinyThresholdSeconds) > 0
      ? Number(options.tinyThresholdSeconds)
      : tinySegmentThresholdSecondsValue();
  return chapters.filter((chapter) => !classifyMeetingGroup(chapter, tinyThreshold).hide);
}

function sessionChaptersPlainText(sessionGroup, options = {}) {
  const chapters =
    Array.isArray(sessionGroup.chapter_groups) && sessionGroup.chapter_groups.length
      ? sessionGroup.chapter_groups
      : buildSessionChapterGroups(sessionGroup, { applyNoiseFilter: true });
  if (!chapters.length) {
    return "";
  }
  return chapters
    .map((chapter, index) => {
      const body = groupPlainTranscriptText(chapter, options);
      return `[Chapter ${index + 1}/${chapters.length}]\n${body}`.trim();
    })
    .join("\n\n");
}

function sessionCleanText(sessionGroup, options = {}) {
  const chapters =
    Array.isArray(sessionGroup.chapter_groups) && sessionGroup.chapter_groups.length
      ? sessionGroup.chapter_groups
      : buildSessionChapterGroups(sessionGroup, { applyNoiseFilter: true });
  if (!chapters.length) {
    return groupPlainTranscriptText(sessionGroup, options);
  }
  return chapters
    .map((chapter) => groupPlainTranscriptText(chapter, options))
    .filter(Boolean)
    .join("\n\n");
}

function sessionOutlinePlainText(sessionGroup) {
  const chapters =
    Array.isArray(sessionGroup.chapter_groups) && sessionGroup.chapter_groups.length
      ? sessionGroup.chapter_groups
      : buildSessionChapterGroups(sessionGroup, { applyNoiseFilter: true });
  if (!chapters.length) {
    return groupPlainText(sessionGroup, GROUPING_MODE.SESSION, true);
  }
  const sourceSummary = `${(sessionGroup.source_ids || []).length} source${
    (sessionGroup.source_ids || []).length === 1 ? "" : "s"
  }`;
  const speakerSummary = `${sessionGroup.speaker_count || 0} speaker${
    sessionGroup.speaker_count === 1 ? "" : "s"
  }`;
  const chunkSummary = `${(sessionGroup.chunks || []).length} chunk${
    (sessionGroup.chunks || []).length === 1 ? "" : "s"
  }`;
  const chapterSummary = `${chapters.length} chapter${chapters.length === 1 ? "" : "s"}`;
  const chapterLines = chapters
    .map((chapter, index) => {
      const chapterPreview = previewTextForGroup(chapter, 220);
      return `[Chapter ${index + 1}/${chapters.length}] ${formatRange(
        chapter.started_at,
        chapter.ended_at
      )}\n${chapterPreview}`;
    })
    .join("\n");
  return `${groupHeaderLine(sessionGroup, GROUPING_MODE.SESSION)}
Begin: ${formatTimestamp(sessionGroup.started_at)}
End: ${formatTimestamp(sessionGroup.ended_at)}
Duration: ${formatDuration(sessionGroup.started_at, sessionGroup.ended_at)}
Counts: ${sourceSummary} | ${speakerSummary} | ${chunkSummary} | ${chapterSummary}
${sessionGroup.session_id ? `Session: ${sessionGroup.session_id}\n` : ""}${chapterLines}`.trim();
}

async function copyGroupByKey(key, kind = "full") {
  const view = state.lastView;
  if (!view || !Array.isArray(view.groups) || !view.groups.length) {
    setStatus("No grouped transcript loaded to copy.", true);
    return;
  }
  const group = view.groups.find((item) => item.key === key);
  if (!group) {
    setStatus("Selected group no longer exists in current view.", true);
    return;
  }
  let payload = "";
  if (kind === "chapters") {
    if (view.mode !== GROUPING_MODE.SESSION) {
      setStatus("Chapter copy is only available in Source sessions view.", true);
      return;
    }
    payload = sessionChaptersPlainText(group, { timestamps: true, speakers: true }).trim();
  } else {
    if (kind === "text") {
      payload =
        view.mode === GROUPING_MODE.SESSION
          ? sessionCleanText(group, { timestamps: true, speakers: true }).trim()
          : groupPlainTranscriptText(group, { timestamps: true, speakers: true }).trim();
    } else {
      payload = groupPlainText(group, view.mode, true).trim();
    }
  }
  if (!payload) {
    setStatus("Selected group has no text to copy.", true);
    return;
  }
  const copied = await copyTextToClipboard(payload);
  if (copied) {
    if (kind === "chapters") {
      setStatus("Copied session chapters to clipboard (with timestamps).");
    } else if (kind === "text") {
      setStatus(
        `Copied ${view.mode === GROUPING_MODE.SESSION ? "session" : "meeting"} text to clipboard (with timestamps).`
      );
    } else {
      setStatus(`Copied ${view.mode === GROUPING_MODE.SESSION ? "session" : "meeting"} to clipboard.`);
    }
    return;
  }
  setStatus("Clipboard copy blocked by browser.", true);
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
    const preview = text.length > 400 ? `${text.slice(0, 400)}…` : text;
    setStatus(`Clipboard read OK (${text.length} chars). Preview: ${preview.replaceAll("\n", " ")}`);
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
  const text = loadedPlainText().trim();
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
  state.generatedTitles = {};
  state.generatedTitleAttempts = {};
  render();
  setStatus("Cleared loaded chunks.");
}

function wireEvents() {
  if (el.googleSyncConnect) {
    el.googleSyncConnect.addEventListener("click", () => {
      void connectGoogleSync();
    });
  }
  if (el.googleSyncNow) {
    el.googleSyncNow.addEventListener("click", () => {
      void syncGoogleCalendarNow();
    });
  }
  if (el.googleSyncRefresh) {
    el.googleSyncRefresh.addEventListener("click", () => {
      void (async () => {
        await loadGoogleSyncStatus();
        await loadGoogleCalendarEvents();
      })();
    });
  }
  el.loadFirst.addEventListener("click", () => loadPage(true));
  el.loadMore.addEventListener("click", () => {
    if (!state.hasMore) {
      setStatus("No older chunks available.");
      return;
    }
    loadPage(false);
  });
  el.loadAll.addEventListener("click", loadAllPages);
  if (el.generateTitles) {
    el.generateTitles.addEventListener("click", () => generateTitlesForCurrentGroups({ force: true }));
  }
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
  if (el.groupingMode) {
    el.groupingMode.addEventListener("change", () => {
      render();
      void autoGenerateTitles();
      setStatus(`Switched to ${groupingLabel()} view.`);
    });
  }
  if (el.meetingGapSeconds) {
    el.meetingGapSeconds.addEventListener("change", () => {
      const mode = groupingModeValue();
      if (mode === GROUPING_MODE.MEETING || mode === GROUPING_MODE.SESSION) {
        render();
        void autoGenerateTitles();
        setStatus(`Meeting gap set to ${meetingGapSecondsValue()} seconds.`);
      }
    });
  }
  if (el.meetingMaxMinutes) {
    el.meetingMaxMinutes.addEventListener("change", () => {
      const mode = groupingModeValue();
      if (mode === GROUPING_MODE.MEETING || mode === GROUPING_MODE.SESSION) {
        render();
        void autoGenerateTitles();
        setStatus(`Max meeting length set to ${meetingMaxMinutesValue()} minutes.`);
      }
    });
  }
  if (el.tinyMaxSeconds) {
    el.tinyMaxSeconds.addEventListener("change", () => {
      const mode = groupingModeValue();
      if (mode === GROUPING_MODE.MEETING || mode === GROUPING_MODE.SESSION) {
        render();
      }
      setStatus(`Tiny/noise threshold set to ${tinySegmentThresholdSecondsValue()} seconds.`);
    });
  }
  if (el.hideTinySegments) {
    el.hideTinySegments.addEventListener("change", () => {
      const mode = groupingModeValue();
      if (mode === GROUPING_MODE.MEETING || mode === GROUPING_MODE.SESSION) {
        render();
      }
      setStatus(
        hideTinySegmentsEnabled()
          ? "Short/noise filtering enabled."
          : "Short/noise filtering disabled."
      );
    });
  }
  if (el.plainIncludeTimestamps) {
    el.plainIncludeTimestamps.addEventListener("change", () => {
      render();
      setStatus(
        plainIncludeTimestampsEnabled()
          ? "Plain text now includes timestamps."
          : "Plain text timestamps removed."
      );
    });
  }
  if (el.plainIncludeSpeakers) {
    el.plainIncludeSpeakers.addEventListener("change", () => {
      render();
      setStatus(
        plainIncludeSpeakersEnabled()
          ? "Plain text now includes source/speaker labels."
          : "Plain text source/speaker labels removed."
      );
    });
  }
  if (el.list) {
    el.list.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      const button = target.closest("button[data-copy-group]");
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const key = String(button.dataset.copyGroup || "").trim();
      const kind = String(button.dataset.copyKind || "full").trim().toLowerCase();
      if (!key) {
        return;
      }
      void copyGroupByKey(key, kind);
    });
  }
  if (el.googleSyncEvents) {
    el.googleSyncEvents.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      const button = target.closest("button[data-focus-event]");
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      const eventRef = String(button.dataset.focusEvent || "").trim();
      if (!eventRef) {
        return;
      }
      focusTranscriptForEvent(eventRef);
    });
  }
}

async function init() {
  wireEvents();
  render();
  await loadGoogleSyncStatus({ silent: true });
  await loadGoogleCalendarEvents({ silent: true });
  await loadPage(true);
}

init();
