# Raw Transcript

Extract a raw transcript from the audio_assist SQLite database and save it as a markdown file.

## Arguments

The user will describe the conversation in natural language. Parse for:
- **Time range**: start time, end time (or "just finished" = now). Convert local EST to UTC (+5h) for DB queries.
- **Participants**: names, emails, any identifying info for the file header.
- **Output path**: default `/Users/tdeshane/mark-sebast/docs/meeting-notes/YYYY-MM-DD-<description>.md` (matches existing convention, e.g. `2026-02-25-mark-david-todd.md`)

## Database

- **Path**: `data/audio_assist.db`
- **Table**: `transcripts` — columns: `id`, `source_id`, `session_id`, `started_at`, `ended_at`, `text`, `speaker`, `created_at`
- **Sources**: `desk-mic` = Todd's microphone (speaker is always "Todd Deshane"), `app-audio` = remote call audio (speaker may be blank)
- Timestamps are ISO-8601 UTC with `+00:00` suffix.

## Extraction Logic

1. Query both sources for the time window, ordered by `started_at ASC`.
2. **Deduplicate**: desk-mic and app-audio capture simultaneously. For each app-audio entry, if a desk-mic entry exists within 3 seconds with similar text prefix, skip the app-audio entry (it's echo).
3. **Label**: desk-mic entries → "Todd Deshane". Remaining app-audio entries → "Remote Speaker" (or use speaker column if populated).
4. **Merge**: Consecutive entries from the same speaker within 15 seconds → combine text.
5. **Trim**: After the last real goodbye/sign-off, remove trailing noise (lone "you", "Okay", silence artifacts).
6. **Format**: Markdown with header (date, participants) and timestamped speaker lines.

## Output Format

```markdown
# Raw Transcript - <description>
**Date:** <date and time range in EST>
**Participants:** <names>

---

**[HH:MM:SS PM] Speaker Name:** Transcript text here.
```
