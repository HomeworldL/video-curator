"""Map actual note page state to common source fields and downloadable streams."""

import json
from urllib.parse import urlsplit

from ...config import Runtime
from ...models import Candidate, CollectionError, Platform
from ...providers.browser import page as browser_page
from .session import access_issue, navigate
from .urls import parse

READ_NOTE = """(noteId) => {
    const state = window.__INITIAL_STATE__?.note?.noteDetailMap;
    const value = state?.value ?? state?._value ?? state?._rawValue ?? state;
    return value?.[noteId]?.note ?? null;
}"""


def media_url(url) -> str | None:
    if not isinstance(url, str):
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        return None
    return url if host.endswith(".xhscdn.com") or host == "xhscdn.com" else None


def formats_from_note(note: dict) -> list[dict]:
    video = note.get("video") or {}
    media = video.get("media") or {}
    # mediaV2 is a JSON-encoded alternate representation used by some current pages.
    if not media.get("stream") and isinstance(video.get("mediaV2"), str):
        try:
            alternate = json.loads(video["mediaV2"])
            media = alternate.get("media") or alternate.get("video", {}).get("media") or alternate
        except (TypeError, ValueError):
            pass
    formats = []
    for encoding, streams in (media.get("stream") or {}).items():
        if not isinstance(streams, list):
            continue
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            urls = [stream.get("masterUrl"), *(stream.get("backupUrls") or [])]
            for url in dict.fromkeys(media_url(url) for url in urls if media_url(url)):
                formats.append(
                    {
                        "url": url,
                        "ext": "mp4",
                        "format_id": f"{encoding}-{stream.get('qualityType', '')}",
                        "width": stream.get("width"),
                        "height": stream.get("height"),
                        "filesize": stream.get("size"),
                        "vcodec": stream.get("videoCodec"),
                        "duration": (float(stream["duration"]) / 1000)
                        if stream.get("duration")
                        else None,
                        "tbr": stream.get("avgBitrate") or stream.get("videoBitrate") or 0,
                    }
                )
    # Prefer declared streams. An origin key is an additional candidate, never proof of quality.
    key = (video.get("consumer") or {}).get("originVideoKey")
    if isinstance(key, str) and key and not key.startswith(("/", ".")) and ":" not in key:
        url = "https://sns-video-bd.xhscdn.com/" + key
        formats.append({"url": url, "ext": "mp4", "format_id": "origin"})
    return sorted(
        formats,
        key=lambda item: (item.get("height") or 0, item.get("width") or 0, item.get("tbr") or 0),
        reverse=True,
    )


def normalize_note(note: dict, candidate: Candidate) -> dict:
    identity = parse(candidate.url, {})
    if identity is None or note.get("noteId") != identity[0]:
        raise CollectionError("detail_identity_mismatch", "metadata")
    if note.get("type") != "video":
        raise CollectionError("not_a_video", "metadata")
    formats = formats_from_note(note)
    if not formats:
        raise CollectionError("media_formats_unavailable", "metadata")
    user = note.get("user") or {}
    video = note.get("video") or {}
    duration = (video.get("capa") or {}).get("duration")
    if not duration:
        duration = next((item["duration"] for item in formats if item.get("duration")), None)
    return {
        "id": identity[0],
        "webpage_url": candidate.url,
        "title": note.get("title"),
        "description": note.get("desc"),
        "uploader": user.get("nickname") or user.get("nickName"),
        "uploader_id": user.get("userId"),
        "timestamp": float(note["time"]) / 1000 if note.get("time") else None,
        "duration": duration,
        "tags": [
            tag["name"]
            for tag in note.get("tagList") or []
            if isinstance(tag, dict) and tag.get("name")
        ],
        "formats": formats,
        "_backend": "xiaohongshu-browser",
    }


def detail(candidate: Candidate, runtime: Runtime) -> dict:
    identity = parse(candidate.url, {})
    if identity is None:
        raise CollectionError("unresolved_identity", "metadata")
    with browser_page(runtime.platforms[Platform.XIAOHONGSHU], runtime, stage="metadata") as page:
        navigate(page, candidate.url, "metadata")
        page.wait_for_timeout(1200)
        note = page.evaluate(READ_NOTE, identity[0])
        if not isinstance(note, dict) or not note:
            raise CollectionError(access_issue(page) or "note_page_unavailable", "metadata")
        return normalize_note(note, candidate)
