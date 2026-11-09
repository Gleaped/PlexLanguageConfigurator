"""Command line interface.

    plexlang users
    plexlang search "Breaking Bad"
    plexlang languages "Breaking Bad" --user Alice
    plexlang set "Breaking Bad" --user Alice --audio ita --subs eng --dry-run
    plexlang set "Breaking Bad" --user Alice --audio ita --subs off

Connection settings come from --url/--token or PLEX_URL/PLEX_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import core, plex_client


def _item_label(item) -> str:
    if getattr(item, "type", None) == "episode":
        return f"{item.grandparentTitle} S{item.parentIndex:02}E{item.index:02} {item.title}"
    return item.title


def _connect(args):
    if not args.url or not args.token:
        sys.exit("Set PLEX_URL and PLEX_TOKEN (or pass --url and --token).")
    return plex_client.connect(args.url, args.token)


def _load(args):
    admin = _connect(args)
    user = plex_client.find_user(admin, args.user)
    plex = plex_client.as_user(admin, user)
    title = plex_client.resolve_title(plex, args.title)
    print(f"Loading '{title.title}' for {user.title}...", file=sys.stderr)
    items = plex_client.playable_items(plex, title)
    return user, title, items


def cmd_users(args):
    for u in plex_client.home_users(_connect(args)):
        print(f"{u.title}{'  (admin)' if u.is_admin else ''}")


def cmd_search(args):
    for r in plex_client.search(_connect(args), args.query):
        print(f"{r.ratingKey}: {r.title} ({getattr(r, 'year', '?')}, {r.type})")


def print_summary(summary: core.LanguageSummary):
    print(f"{summary.total} episode(s)\n")
    print("Audio:")
    for o in summary.audio or []:
        print(f"  {o.label()}")
    if not summary.audio:
        print("  (none)")
    print("\nSubtitles:")
    print(f"  Off ({core.OFF})")
    for o in summary.subtitles:
        print(f"  {o.label()}")


def cmd_languages(args):
    _, _, items = _load(args)
    print_summary(core.summarize(items))


def cmd_set(args):
    if not args.audio and not args.subs:
        sys.exit("Choose --audio and/or --subs.")
    user, title, items = _load(args)
    plan = core.build_plan(items, audio=args.audio, subtitles=args.subs)

    if args.audio:
        print(f"Audio '{args.audio}': {plan.audio_matched} of {plan.total} episodes")
        for item in plan.audio_missing:
            print(f"  missing: {_item_label(item)}")
    if args.subs:
        print(f"Subtitles '{args.subs}': {plan.subtitles_matched} of {plan.total} episodes")
        for item in plan.subtitles_missing:
            print(f"  missing: {_item_label(item)}")

    if args.dry_run:
        print(f"\nDry run: would update {len(plan.changes)} file(s) for {user.title}. Nothing changed.")
        return
    if not plan.changes:
        print("\nNothing to change.")
        return
    updated = core.apply_plan(plan)
    print(f"\nUpdated {updated} file(s) of '{title.title}' for {user.title}.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plexlang", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("PLEX_URL"), help="Plex server URL")
    p.add_argument("--token", default=os.environ.get("PLEX_TOKEN"), help="Admin Plex token")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("users", help="List the admin and Plex Home users").set_defaults(func=cmd_users)

    s = sub.add_parser("search", help="Find shows and movies")
    s.add_argument("query")
    s.set_defaults(func=cmd_search)

    for name, func, helptext in (
        ("languages", cmd_languages, "Show available languages with episode counts"),
        ("set", cmd_set, "Set audio and/or subtitle language for a whole series or movie"),
    ):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("title", help="Show or movie title, or its number from 'search'")
        c.add_argument("--user", required=True, help="Home user name (or the admin)")
        if name == "set":
            c.add_argument("--audio", help="Audio language code, e.g. ita or eng")
            c.add_argument("--subs", help="Subtitle language code, or 'off'")
            c.add_argument("--dry-run", action="store_true", help="Show what would change")
        c.set_defaults(func=func)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except LookupError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
