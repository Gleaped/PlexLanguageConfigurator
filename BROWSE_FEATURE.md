# Browse by language

The web interface now has a **Browse by language** page alongside Search & configure.

## What it does

- Select **Audio** or **Subtitles**.
- Select all media, movies only, or series only.
- Select a language to see every matching movie or series visible to the selected Plex Home user.
- Series show language coverage as `episodes containing the language / total episodes`.
- Selecting a result opens the existing language configuration panel for that title and user.
- **Refresh library index** rebuilds the index after library or media changes.

The browse page reads the shared library index (see the README's *First start*), filtered to what the selected Home user can see. It inspects media streams, not just title metadata, so results reflect languages actually present in the files. Items that cannot be read are skipped and counted as errors.

## API

- `GET /api/browse/languages?user=<home-user>&kind=audio`
  - Returns the available language codes and title/item counts.
  - `kind` can be `audio` or `subtitles`.
  - Add `refresh=1` to rebuild the per-user index.
- `GET /api/browse?user=<home-user>&kind=audio&language=ita&type=all`
  - Returns matching titles and episode coverage.
  - `type` can be `all`, `movie`, or `show`.

## Plex API search adjustment

The existing title search now queries each accessible movie/TV library section separately instead of issuing the global `/library/all` search request. This avoids the global endpoint and its `includeGuids` parameter combination that produced HTTP 500 in the reported setup.

## Index and sorting

The index is built in the background, saved to `/data/library-index.json`, and refreshed incrementally (see the README). **Full rescan** re-reads everything in the background while current results stay available.

The results can be sorted by title in either direction. When **Series only**
is selected, additional sorts are available:

- Total episode count (ascending or descending).
- Percentage of episodes that contain the selected language (ascending or descending).

Coverage is calculated as `episodes containing selected language / total episodes`.

## Optional Bazarr integration

Set both environment variables on the container:

```text
BAZARR_URL=http://192.168.0.XX:6767
BAZARR_API_KEY=your_bazarr_api_key
```

Use the address and port reachable from the Plex Language Configurator
container. The URL may include `/api`; the client adds it when omitted.

When browsing **Audio** and a language is selected, the **Search missing
subtitles in Bazarr** action:

1. Selects only Plex movies/series in the current media-type filter that have
   the chosen audio language.
2. Retrieves Bazarr's indexed series and movie catalog.
3. Matches Plex titles to Bazarr entries by IMDb ID (and TVDB ID for series).
4. Calls Bazarr's per-title `search-missing` action for each matched entry.
5. Reports successful triggers, titles not present in Bazarr, and failures.

Matching prefers stable IMDb/TVDB identifiers. If those are unavailable, it
allows an exact normalized title plus year match only when that combination is
unique in Bazarr. It does not use fuzzy matching, so ambiguous remakes or
similarly named titles are reported as unmatched rather than guessed.

**Important:** Bazarr's assigned language profile controls which subtitle
languages it searches for. By default this integration retains each title's
existing profile. If you configure a language-to-profile mapping, it assigns
the selected profile before searching. Profile assignment is global in Bazarr,
not scoped to the selected Plex Home user. Bazarr's `search-missing` action can
download subtitles automatically when a provider finds a suitable match.
Review your profiles, providers, score thresholds, and download behavior before
using bulk search.

The integration uses Bazarr's authenticated API (`X-API-KEY`) and the
per-series `PATCH /api/series?seriesid=...&action=search-missing` or per-movie
`PATCH /api/movies?radarrid=...&action=search-missing` operations. See the
[Bazarr API](https://bazarr.ericweissgerber.com/api/) and
[Bazarr subtitle settings](https://wiki.bazarr.media/Additional-Configuration/Settings/Subtitles/).


## Configurable Bazarr audio-language profile mapping

The Browse page can map each selected Plex audio language code (for example
`ita`) to a Bazarr language profile. When **Search missing subtitles in
Bazarr** runs, the app first assigns the mapped profile to each matched
Sonarr/Radarr item whose current profile differs, then triggers Bazarr's
`search-missing` action. If no mapping exists for the selected language, the
item's existing Bazarr profile is left unchanged.

The app retrieves profile IDs/names from `GET /api/system/languages/profiles`
and assigns profiles using `POST /api/series?seriesid=...&profileid=...` or
`POST /api/movies?radarrid=...&profileid=...`. Bazarr's own handlers recalculate
missing-subtitle state when the profile is changed. It then calls the existing
per-title `PATCH ...?action=search-missing` endpoint.

**Important scope warning:** Bazarr language profiles are assigned to Sonarr/Radarr
media items, not to Plex Home users. Changing a profile is a global change for
that series/movie and affects every Plex user. The UI requires confirmation
before bulk actions and reports how many profiles were changed. The app does not
edit the definitions of Bazarr profiles; it only assigns an existing profile.

Mappings are persisted in
`/data/bazarr-language-profile-mappings.json` by default. To keep them when
recreating/updating the Docker container, add an Unraid path mapping such as:

- Host path: `/mnt/user/appdata/plexlanguageconfigurator`
- Container path: `/data`
- Access mode: Read/Write

You can override the directory with `PLEXLANG_DATA_DIR` or set the complete
file path with `BAZARR_PROFILE_MAPPINGS_FILE`. If `/data` is not mounted, the
mapping file may be lost when the container is removed/recreated.

The profile selector appears only when Bazarr is configured and Browse is set
to Audio. Choose **Keep each title's existing profile** to disable a mapping
for that language.


## Bazarr bulk-operation safety, status, queue and profile recommendations

The Bazarr workflow uses a two-step preview/execute process:

1. `POST /api/browse/bazarr-preview` performs read-only matching and returns each
   candidate's Plex key, Bazarr ID, current profile, proposed profile and subtitle
   status. It also returns unmatched titles separately.
2. The user selects the exact titles to process in the UI.
3. `POST /api/browse/bazarr-jobs` validates that selected Plex keys belong to the
   server-side preview and queues only those items. A forged or stale selection
   is rejected.

No profile assignment or subtitle search is performed during preview. The UI
shows the current-to-target profile change and warns that profile assignment is
global in Bazarr (not per Plex Home user). The user must explicitly confirm the
selected operation.

### Per-title subtitle status

The application reads Bazarr's `GET /api/movies/wanted` and
`GET /api/episodes/wanted` endpoints. It displays missing wanted languages and
counts where Bazarr reports them. If a title has no wanted-list entries, the UI
labels it **No wanted entries (not verified)**. This is intentionally not
presented as proof that every subtitle file exists or is indexed on disk; the
wanted list is Bazarr's view of missing subtitles under the item's current
profile.

Status/catalog data is cached for five minutes to avoid repeatedly querying
Bazarr while users change sort order or filters.

### Background job queue

Bazarr operations run in a single-worker background queue so large selections
do not hold the web request open or overwhelm Bazarr with parallel calls. The
UI polls job progress and displays per-title success/failure. Each title gets
one automatic retry after a failed attempt. Failed or cancelled titles can be
retried manually, and queued/running jobs can be cancelled between title
operations (an in-flight Bazarr HTTP request is allowed to finish).

Job events are appended to:

```text
/data/bazarr-job-history.jsonl
```

The log rotates at approximately 2 MB. The API exposes the latest 200 events at
`GET /api/bazarr/jobs/history`. The `/data` volume mapping is therefore
recommended for both profile mappings and job history. In-memory job progress
is lost if the configurator container restarts; the JSONL audit log remains.

### Smarter profile suggestions

`GET /api/bazarr/profile-suggestions?language=ita` inspects Bazarr's language
catalogue and profile JSON. It matches language codes and names (for example
`it`, `ita`, `Italian`) against the actual languages contained in each profile.
Profiles containing the selected language are suggested first. Profile names
alone are not treated as evidence, and suggestions never assign anything
automatically. Review forced, hearing-impaired, exclude-audio and cutoff rules
inside Bazarr before choosing a profile.

### Relevant Bazarr endpoints

- `GET /api/system/languages`
- `GET /api/system/languages/profiles`
- `GET /api/series` and `GET /api/movies`
- `GET /api/episodes/wanted` and `GET /api/movies/wanted`
- `POST /api/series` and `POST /api/movies` for profile assignment
- `PATCH /api/series` and `PATCH /api/movies` for `search-missing`

See the [Bazarr API](https://bazarr.ericweissgerber.com/api/) for the API schema.
