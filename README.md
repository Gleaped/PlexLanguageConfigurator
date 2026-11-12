# Plex Language Configurator

Set the audio and subtitle language for a whole series (or a movie) in one go, for any Plex Home user.

Plex remembers your audio and subtitle choice per file, so switching a 100-episode show to Italian audio normally means 100 clicks. This tool lets you pick the language once per series, and it applies it to every episode that has that language. It tells you how many episodes have each language, for example `Italiano (38/42)`, and lists the episodes that don't before you apply.

It runs as a Docker container with a small web UI.

## What you need

- Your Plex server's address, as reachable from the container, e.g. `http://192.168.1.10:32400`.
- The **server owner's** Plex token ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)). The owner's token is what lets the tool act on behalf of Plex Home users.

## Install on Unraid

1. In Unraid, open **Docker** and click **Add Container** at the bottom.
2. Fill in:
   - **Name:** `PlexLanguageConfigurator`
   - **Repository:** `ghcr.io/gleaped/plexlanguageconfigurator:latest`
   - **Network Type:** `Bridge`
   - **WebUI:** `http://[IP]:[PORT:8080]/`
3. Click **Add another Path, Port, Variable, Label or Device** for each of these:
   - **Port:** Container Port `8080`, Host Port `8080` (or any free port).
   - **Path:** Container Path `/data`, Host Path `/mnt/user/appdata/plexlang`. This keeps the library index, so restarts and updates don't rescan.
   - **Variable:** Key `PLEX_URL`, Value `http://<your-unraid-ip>:32400`.
   - **Variable:** Key `PLEX_TOKEN`, Value your token, with Mask set to *Yes*.
   - Optional: `BAZARR_URL` and `BAZARR_API_KEY`, and any setting from the table below.
4. Click **Apply**, then open the container's **WebUI**.

The easier way is the template, which has every setting already filled in. From the Unraid terminal:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-PlexLanguageConfigurator.xml \
  https://raw.githubusercontent.com/Gleaped/PlexLanguageConfigurator/main/unraid/plexlang.xml
```

Then in **Docker → Add Container** pick *PlexLanguageConfigurator* from the **Template** dropdown, fill in the Plex URL and token, and click **Apply**.

**Updating an existing container:** after the new image is published, click the container icon → **Force update**. If you set the container up by hand earlier, add the `/data` path above (Edit → Add another Path) so the index is kept between updates.

### First start

The container starts serving right away. In the background it builds a language index of your whole library and saves it to `/data`. The status line at the top of the page shows progress. Reads are batched (about 100 items per Plex request, 4 in parallel), so even large libraries usually finish within minutes. After that:

- Pages read from the saved index and never wait on Plex.
- Every 30 minutes (`REFRESH_MINUTES`) it lists the library and re-reads only items that changed or were added.
- Once a week (`FULL_RESCAN_HOURS`) it re-reads everything, to catch changes Plex doesn't flag, such as a new external subtitle file. **Full rescan** on the Browse page starts one now.

## Install with Docker Compose

Edit `PLEX_URL` and `PLEX_TOKEN` in [`docker-compose.yml`](docker-compose.yml), then:

```bash
docker compose up -d
```

## Using it

Open `http://<host>:8080`:

1. Pick a user.
2. Search for a series or movie and click it.
3. Choose the audio and subtitle language (or *Off*, or *Don't change*). Languages that aren't in every episode show a count, like `Italiano (38/42)`.
4. **Preview** shows how many episodes match and lists the ones that don't. **Apply** saves the choice for that user. Episodes without the language are left as they are.

### Missing Italian

The **Missing Italian** tab lists every series and movie the selected user can see that has no Italian, or has it in only some episodes (for example `Audio 12/40 · Subs none`). You can filter by audio, subtitles, either or both, and by movies or series. **Download CSV** exports the list with IMDb/TVDB/TMDB IDs, which you can use in Sonarr or Radarr. If Bazarr is configured, **Search Bazarr for missing subtitles** previews and runs a subtitle search for exactly those titles. The language is set by `FOCUS_LANGUAGE` (default `ita`).

### Get an Italian version (Sonarr, Radarr, Seerr)

When Sonarr, Radarr and/or Seerr are configured, the Missing Italian list gets checkboxes. Tick titles and press **Preview request**. For each title you'll see what will happen and anything in your setup that would stop it from working:

- **Series in Sonarr:** optionally switch to your Italian quality profile, then search **only the episodes Plex says lack Italian** (matched by season and episode number).
- **Movies in Radarr:** optionally switch profile, then search the movie.
- **Not in Sonarr/Radarr:** request it in Seerr, using the TMDB ID from Plex. Seerr refuses to re-request something it already has, so Seerr is mainly useful for titles Sonarr/Radarr don't manage, or for sending requests to a separate Italian Sonarr/Radarr server in Seerr (set `SEERR_SONARR_SERVER` / `SEERR_RADARR_SERVER`). `ARR_REQUEST_VIA=seerr` sends everything through Seerr.

Nothing is sent until you press **Start requests**. The app never edits your profiles or custom formats. It only reads them. **Setup check** shows, for each profile, whether a search will actually prefer an Italian release and replace your current file.

**How to set up Sonarr v4 / Radarr so this works.** Language is chosen by custom formats:

1. Create a custom format **Italian** with a *Language* condition set to *Italian*.
2. Create a custom format **MULTi ITA** with a *Release Title* condition like `\b(MULTi|ITA)\b`, and add *Language: Italian* as a required condition. MULTi on its own also matches French + English releases.
3. In the profile you want (or a copy, for example *HD - Italian*), score **MULTi ITA** highest (for example 200) and **Italian** lower (for example 100). That way a release with both Italian and the original audio wins over an Italian-only one, which would replace your original-language track.
4. Keep **Upgrades allowed** on, and set **Upgrade until custom format score** at least as high as the MULTi score. In Radarr, set the profile language to *Any*, not *Original* or English.
5. Set `SONARR_ITALIAN_PROFILE` / `RADARR_ITALIAN_PROFILE` to that profile's name, or leave them empty and the best Italian-ready profile is picked.

### Optimise Sonarr / Radarr profiles (Italian + English + Japanese)

The **Sonarr / Radarr** tab can tune your quality profiles so releases with more of your languages win. It is built to be safe on a live setup:

1. **Setup check** (read-only) shows, per profile, whether a search would prefer an Italian release and why not.
2. Set the scores (defaults: Italian 200, English 100, Japanese 100, MULTi / Dual audio bonus 50) and press **Preview**. The app lists every change: custom formats it would create, score changes per profile, and fixes to the upgrade cut-off, "upgrades allowed" and profile language (`Original` blocks non-original audio).
3. Tick the profiles to change, tick the confirmation box, and press **Back up and apply**. The original profiles are saved to `/data/backups/` first.
4. **Restore** on the Backups card puts the original profiles back and deletes only the custom formats the app created.

Scores add up, so a release with Italian + English + Japanese scores 450 and beats one with a single language. Your own existing custom formats are reused and a score you set higher is never lowered. Created formats are prefixed `PLC ` so you can recognise them. Nothing is applied without the confirmation, and running it twice changes nothing the second time.

### Italian label in Plex

The same tab shows how many titles have Italian and how many already carry the `Italian` label. **Update labels in Plex** adds the label to the ones that are missing it. Set `AUTO_LABEL=true` to have this happen after every refresh instead.

The label goes on the series or movie, not on each episode, and the same single label is used everywhere. Plex indexes labels, so filtering a library by **Label → Italian** or building a smart collection on it is fast. Only titles whose label needs to change are touched. Labels are server-wide, so every user sees them. Plex can also use labels to restrict what managed users see, so pick a label name you don't already use for that.

When an episode has more than one track in the chosen language, the tool prefers main audio over commentary tracks, and full subtitles over forced ones.

## Configuration

| Variable | Required | Default | What it is |
|---|---|---|---|
| `PLEX_URL` | yes | | Plex server address as reachable from the container |
| `PLEX_TOKEN` | yes | | Server owner's Plex token |
| `PORT` | no | `8080` | Port the web UI listens on inside the container |
| `FOCUS_LANGUAGE` | no | `ita` | Language for the Missing page and the label |
| `LANGUAGE_LABEL` | no | `Italian` | Plex label for titles with that language |
| `AUTO_LABEL` | no | `false` | `true` keeps labels in sync after every refresh |
| `LABEL_CRITERIA` | no | `any` | `any`, `audio` or `subtitles`: which streams count |
| `LABEL_MIN_COVERAGE` | no | `0` | Minimum % of a series' episodes that need the language (0 = any episode) |
| `LABEL_REMOVE_STALE` | no | `false` | `true` also removes the label from titles that no longer have the language |
| `REFRESH_MINUTES` | no | `30` | How often to check Plex for changes |
| `FULL_RESCAN_HOURS` | no | `168` | How often to re-read everything |
| `PLEX_CONCURRENCY` | no | `4` | Parallel Plex requests while indexing |
| `PLEX_BATCH_SIZE` | no | `100` | Items read per Plex request |
| `BAZARR_URL`, `BAZARR_API_KEY` | no | | Optional Bazarr integration |
| `SONARR_URL`, `SONARR_API_KEY` | no | | Optional, for requesting Italian episodes |
| `RADARR_URL`, `RADARR_API_KEY` | no | | Optional, for requesting Italian movies |
| `SEERR_URL`, `SEERR_API_KEY` | no | | Optional Overseerr/Jellyseerr |
| `SONARR_ITALIAN_PROFILE`, `RADARR_ITALIAN_PROFILE` | no | best Italian-ready | Quality profile (name or id) titles are switched to before searching |
| `SEERR_SONARR_SERVER`, `SEERR_RADARR_SERVER` | no | Seerr default | Seerr server (name or id) to send Italian requests to |
| `SEERR_SONARR_PROFILE`, `SEERR_RADARR_PROFILE` | no | server default | Profile on that Seerr server |
| `ARR_REQUEST_VIA` | no | `auto` | `auto`, `arr` or `seerr` |
| `PUID`, `PGID` | no | `99`, `100` | Owner of files in `/data` (Unraid's nobody:users) |

## Troubleshooting

- **"Can't reach Plex at …"**: `PLEX_URL` is wrong or not reachable from the container. Use the Unraid server's LAN IP, not `localhost`. If Plex is set to *Secure connections: Required* (Settings → Network), plain `http://` is refused; set it to *Preferred*.
- **"Plex rejected the token"**: use the token of the account that owns the server, not a Home user's.
- **A user is missing from the list**: only the owner and Plex Home members are shown; shared friends aren't supported.
- **Container exits right away**: check its log; it says so when `PLEX_URL` or `PLEX_TOKEN` is missing.

## Security

The web UI has no login: anyone who can open it can change language settings for your Home users. Keep it on your local network and don't expose the port to the internet.

## Command line

The image also contains a CLI, run inside the container:

```bash
docker exec -it PlexLanguageConfigurator plexlang languages "Breaking Bad" --user Alice
docker exec -it PlexLanguageConfigurator plexlang set "Breaking Bad" --user Alice --audio ita --subs eng --dry-run
```

## Development

```bash
pip install -e '.[dev]'
pytest
docker build -t plexlang .
```

GitHub Actions runs the tests and publishes the image to `ghcr.io/gleaped/plexlanguageconfigurator` on every push to `main`.
