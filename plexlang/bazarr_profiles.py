"""Helpers for inspecting Bazarr language profiles without mutating them."""
from __future__ import annotations


def profile_language_values(value, in_language_context: bool = False) -> set[str]:
    """Extract language codes/names while ignoring the profile's own name."""
    found: set[str] = set()
    keys = {"code", "code2", "code3", "name", "languagecode", "language_code",
            "languageid", "language_id"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized == "languages":
                found.update(profile_language_values(child, True))
            elif normalized == "language":
                if isinstance(child, (str, int)):
                    found.add(str(child).strip().casefold())
                else:
                    found.update(profile_language_values(child, True))
            elif in_language_context and normalized in keys and isinstance(child, (str, int)):
                found.add(str(child).strip().casefold())
            else:
                found.update(profile_language_values(child, in_language_context))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(profile_language_values(child, in_language_context))
    elif in_language_context and isinstance(value, (str, int)):
        found.add(str(value).strip().casefold())
    return found


def suggest_profiles(profiles: list[dict], language: str, language_name: str,
                     aliases: set[str] | None = None) -> list[dict]:
    """Rank profiles containing the requested subtitle language first.

    This is deliberately advisory: profile names alone are not treated as proof
    that a profile contains a language, and no profile is assigned automatically.
    """
    targets = {str(x).casefold() for x in (aliases or {language, language_name}) if x}
    targets.update({language.casefold(), language_name.casefold()})
    output = []
    for profile in profiles:
        values = profile_language_values(profile)
        matches = sorted(targets.intersection(values))
        output.append({
            "id": str(profile.get("profileId", "")),
            "name": profile.get("name", "Unnamed profile"),
            "contains_language": bool(matches),
            "matched_aliases": matches,
            "languages_detected": sorted(values),
            "reason": (f"Contains {language_name} ({', '.join(matches)})." if matches else
                       "No exact language match detected in the profile data."),
        })
    output.sort(key=lambda item: (not item["contains_language"], item["name"].casefold()))
    return output
