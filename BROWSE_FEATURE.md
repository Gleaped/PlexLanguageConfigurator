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
