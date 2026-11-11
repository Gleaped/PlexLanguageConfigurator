# Bazarr bulk workflow enhancements

This update implements the five requested improvements to the language Browse page.

## 1. Read-only bulk preview

Use **Preview Bazarr changes** before triggering any actions. The preview resolves each Plex item against the Bazarr catalog and displays:

- Plex title and media type;
- matched Bazarr item ID;
- current Bazarr profile;
- mapped target profile, if one exists;
- whether a global profile assignment would change;
- Bazarr wanted-subtitle status and missing language counts;
- titles that could not be uniquely matched.

No Bazarr mutation is performed during preview. The backend stores a short-lived preview and only accepts selected Plex keys belonging to that preview. Profile changes are global to the Bazarr item and affect every Plex user; the UI requires a second confirmation before execution.

## 2. Per-title subtitle status

The status panel uses Bazarr's wanted-subtitle endpoints:

- `GET /api/movies/wanted`
- `GET /api/episodes/wanted`

The app reports missing languages/counts where Bazarr identifies missing wanted subtitles. An item with no wanted-list row is shown as **No wanted entries (not verified)**. It does not claim the subtitle files are complete: Bazarr's wanted list is not a complete disk integrity audit.

Bazarr catalog and wanted status are cached for five minutes while the Plex audio index remains cached for 24 hours.

## 3. Selective processing

Preview candidates are individually selectable, with select-all enabled by default. Only checked items are submitted to the queue. The backend validates that every selected key belongs to the preview; it will reject injected or stale keys. Ambiguous/unmatched items cannot be selected for execution.

## 4. Background queue, progress, retry, cancellation and logs

- A single-worker `ThreadPoolExecutor` processes items sequentially.
- The web request returns a job ID immediately; the UI polls progress.
- Each title gets one automatic retry after a failed attempt.
- Failed titles can be retried manually. Titles not started before cancellation can also be retried.
- Cancellation is checked between title operations. An in-flight Bazarr HTTP request is allowed to finish.
- Per-title success/failure and attempt counts are shown in the UI.
- Audit events are appended to `/data/bazarr-job-history.jsonl`, rotated at approximately 2 MB, and exposed through `GET /api/bazarr/jobs/history`.

The in-memory queue and live progress are lost if the configurator container restarts. The JSONL event log persists when `/data` is mapped to Unraid appdata.

## 5. Smarter profile suggestions

The profile selector now has language-aware suggestions. It reads `GET /api/system/languages` and `GET /api/system/languages/profiles`, matches language codes/names (for example `it`, `ita`, `Italian`) against actual profile language definitions, and ranks profiles containing the selected language first.

Profile names alone are not considered proof that the language is included. Suggestions are advisory only: they never assign a profile automatically. Check forced, hearing-impaired, exclude-audio and cutoff settings in Bazarr before saving a mapping.

## API routes added

- `GET /api/bazarr/profile-suggestions?language=ita`
- `GET /api/bazarr/status?user=<plex-home-user>`
- `POST /api/browse/bazarr-preview`
- `POST /api/browse/bazarr-jobs`
- `GET /api/bazarr/jobs/<job_id>`
- `POST /api/bazarr/jobs/<job_id>/cancel`
- `POST /api/bazarr/jobs/<job_id>/retry`
- `GET /api/bazarr/jobs/history`

The previous direct `/api/browse/bazarr-search` route now returns HTTP 410 to prevent accidentally bypassing the preview/selection workflow.

## Unraid persistence

Keep the existing `/data` mapping:

- Host: `/mnt/user/appdata/plexlanguageconfigurator`
- Container: `/data`
- Access: Read/Write

This directory stores the language-to-profile mapping file and the Bazarr job audit log.

## Validation performed

- Python compilation: passed.
- JavaScript syntax check: passed.
- Unit tests: 15 passed.
- Live Plex/Bazarr integration: not performed in this build environment.
