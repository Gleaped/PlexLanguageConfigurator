# Plex Language Configurator

Set the audio and subtitle language for a whole series (or a movie) in one go, for any Plex Home user.

Plex remembers your audio and subtitle choice per file, so switching a 100-episode show to Italian audio normally means 100 clicks. This tool lets you pick the language once per series, and it applies it to every episode that has that language. It tells you how many episodes have each language, for example `Italiano (ita) - 38 of 42 episodes`, and lists the episodes that don't.

> Status: command line tool and web UI are done. A Docker image for Unraid comes next.

## Setup

You need Python 3.10+, the URL of your Plex server, and your **admin** Plex token
([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).
The admin token is what lets the tool act on behalf of your Plex Home users.

```bash
pip install .
export PLEX_URL=http://192.168.1.10:32400
export PLEX_TOKEN=your-admin-token
```

## Web UI

```bash
plexlang web --port 8080
```

Open `http://<host>:8080`, pick a user, search for a series or movie, choose the audio and subtitle language, then **Preview** to see how many episodes have it (and which don't) before you **Apply**. Languages that aren't in every episode show a count, like `Italiano (38/42)`.

The web UI has no login of its own: anyone who can reach it can change language settings for your Home users, so keep it on your local network.

## Command line

```bash
# Who can I configure? (admin + Plex Home users)
plexlang users

# Find a title (shows its number, handy when titles are ambiguous)
plexlang search "Doctor Who"

# Which languages does this show have, and in how many episodes?
plexlang languages "Breaking Bad" --user Alice

# Preview, then apply: Italian audio, English subtitles
plexlang set "Breaking Bad" --user Alice --audio ita --subs eng --dry-run
plexlang set "Breaking Bad" --user Alice --audio ita --subs eng

# Turn subtitles off for the whole show, leave audio alone
plexlang set "Breaking Bad" --user Alice --subs off
```

Languages use the codes Plex shows (ISO 639-2, like `eng`, `ita`, `deu`); `plexlang languages` lists them.

### How streams are chosen

When an episode has more than one track in the chosen language, the tool prefers the main audio over commentary tracks, and full subtitles over forced ones. Episodes without the language are left as they are and listed in the output.

The choice is saved for the selected user only, just like picking a track in the Plex player.

## Development

```bash
pip install -e '.[dev]'
pytest
```
