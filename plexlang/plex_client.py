"""Thin wrapper around plexapi: connect, switch to a Home user, find titles."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from plexapi.server import PlexServer


@dataclass
class HomeUser:
    title: str
    username: str | None
    id: int | None
    is_admin: bool = False


def connect(url: str, token: str, timeout: int = 30) -> PlexServer:
    return PlexServer(url, token, timeout=timeout)


def home_users(plex: PlexServer) -> list[HomeUser]:
    """The admin plus every Plex Home user (shared friends are left out)."""
    account = plex.myPlexAccount()
    users = [HomeUser(title=account.title or account.username, username=account.username,
                      id=account.id, is_admin=True)]
    for u in account.users():
        if getattr(u, "home", False):
            users.append(HomeUser(title=u.title, username=u.username or None, id=u.id))
    return users


def find_user(plex: PlexServer, name: str) -> HomeUser:
    wanted = name.strip().lower()
    users = home_users(plex)
    for u in users:
        if wanted in {(u.title or "").lower(), (u.username or "").lower(), str(u.id)}:
            return u
    names = ", ".join(u.title for u in users)
    raise LookupError(f"No Home user called '{name}'. Home users: {names}")


def as_user(plex: PlexServer, user: HomeUser) -> PlexServer:
    """A PlexServer connection acting as this user, so selections are saved for them."""
    if user.is_admin:
        return plex
    return plex.switchUser(user.title)


def search(plex: PlexServer, query: str) -> list:
    """Search movie and TV sections individually, avoiding global /library/all."""
    results = []
    seen = set()
    for section in plex.library.sections():
        if getattr(section, "type", None) not in ("show", "movie"):
            continue
        for item in section.search(title=query):
            key = str(getattr(item, "ratingKey", ""))
            if key and key not in seen:
                seen.add(key)
                results.append(item)
    return results


def resolve_title(plex: PlexServer, query: str):
    """Find one show or movie by rating key or title.

    Raises LookupError listing the candidates when the title is ambiguous.
    """
    if query.isdigit():
        return plex.fetchItem(int(query))
    results = search(plex, query)
    if not results:
        raise LookupError(f"Nothing found for '{query}'.")
    exact = [r for r in results if r.title.lower() == query.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(results) == 1:
        return results[0]
    lines = [f"  {r.ratingKey}: {r.title} ({getattr(r, 'year', '?')}, {r.type})" for r in results]
    raise LookupError(
        f"'{query}' matches several titles. Use the number instead:\n" + "\n".join(lines)
    )


def playable_items(plex: PlexServer, title, workers: int = 8) -> list:
    """All episodes of a show (or the movie itself), with their streams loaded.

    Library listings don't include audio/subtitle streams, so each item is
    fetched in full. This runs in parallel because big shows have hundreds of
    episodes.
    """
    if title.type in ("show", "season"):
        keys = [ep.ratingKey for ep in title.episodes()]
    else:
        keys = [title.ratingKey]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(plex.fetchItem, keys))
