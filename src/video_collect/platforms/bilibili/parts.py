"""Expand actual yt-dlp anthology entries; never invent parts from a title."""

from urllib.parse import parse_qs, urlsplit


def has_explicit_part(url: str) -> bool:
    return bool(parse_qs(urlsplit(url).query).get("p"))


def entry_urls(raw: dict) -> list[str]:
    return [
        entry.get("webpage_url") or entry["url"]
        for entry in raw.get("entries") or []
        if entry and (entry.get("webpage_url") or entry.get("url"))
    ]
