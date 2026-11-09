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
3. Click **Add another Path, Port, Variable, Label or Device** three times:
   - **Port:** Container Port `8080`, Host Port `8080` (or any free port).
   - **Variable:** Key `PLEX_URL`, Value `http://<your-unraid-ip>:32400`.
   - **Variable:** Key `PLEX_TOKEN`, Value your token, with Mask set to *Yes*.
4. Click **Apply**, then open the container's **WebUI**.

Alternatively, save [`unraid/plexlang.xml`](unraid/plexlang.xml) to `/boot/config/plugins/dockerMan/templates-user/my-PlexLanguageConfigurator.xml` on your flash drive, then pick it from the **Template** dropdown in **Add Container** and fill in the two Plex fields.

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

When an episode has more than one track in the chosen language, the tool prefers main audio over commentary tracks, and full subtitles over forced ones.

## Configuration

| Variable | Required | Default | What it is |
|---|---|---|---|
| `PLEX_URL` | yes | | Plex server address as reachable from the container |
| `PLEX_TOKEN` | yes | | Server owner's Plex token |
| `PORT` | no | `8080` | Port the web UI listens on inside the container |

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
