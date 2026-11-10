# Browse by language

The web interface now has a **Browse by language** page alongside Search & configure.

## What it does

- Select **Audio** or **Subtitles**.
- Select all media, movies only, or series only.
- Select a language to see every matching movie or series visible to the selected Plex Home user.
- Series show language coverage as `episodes containing the language / total episodes`.
- Selecting a result opens the existing language configuration panel for that title and user.
- **Refresh library index** rebuilds the index after library or media changes.

The browse index is generated per Plex Home user and cached in memory for 10 minutes. It inspects media streams, not just title metadata, so the results reflect languages actually present in the files. The initial scan can take time for large libraries, particularly TV libraries with many episodes. Items that cannot be inspected are skipped and included in the error count; they do not abort the entire scan.

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

## 24-hour cache and sorting

The per-user browse index is cached in process memory for **24 hours**. Use
**Force refresh index** to rebuild it immediately. A refresh rescans the
selected user's accessible movie and TV sections and replaces the previous
cached index. The cache is per Plex Home user; restarting the container also
clears it.

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

**Important:** Bazarr's assigned language profile for each title controls which
subtitle languages it searches for. This integration does not alter profiles.
Bazarr's `search-missing` action can download subtitles automatically when a
provider finds a suitable match. Review your Bazarr language profiles,
providers, score thresholds, and download behavior before using bulk search.

The integration uses Bazarr's authenticated API (`X-API-KEY`) and the
per-series `PATCH /api/series?seriesid=...&action=search-missing` or per-movie
`PATCH /api/movies?radarrid=...&action=search-missing` operations. See the
[Bazarr API](https://bazarr.ericweissgerber.com/api/) and
[Bazarr subtitle settings](https://wiki.bazarr.media/Additional-Configuration/Settings/Subtitles/).
