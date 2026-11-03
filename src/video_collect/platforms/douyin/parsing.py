"""Parse actual Douyin page/API records using yt-dlp's existing media parser."""

from collections.abc import Iterator

from yt_dlp import YoutubeDL
from yt_dlp.extractor.tiktok import DouyinIE

from ...models import CollectionError
from ...providers.ytdlp import QuietLogger


def video_records(value: object) -> Iterator[dict]:
    """Find video records, excluding image-only posts and arbitrary identifier matches."""
    pending = [value]
    visited = 0
    while pending and visited < 10000:
        current = pending.pop()
        visited += 1
        if isinstance(current, dict):
            if (
                str(current.get("aweme_id", "")).isdecimal()
                and isinstance(current.get("video"), dict)
                and any(
                    current["video"].get(key)
                    for key in ("play_addr", "play_addr_h264", "play_addr_bytevc1", "bit_rate")
                )
            ):
                yield current
            else:
                pending.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))


def parse_video(record: dict, expected_id: str, *, backend="douyin-browser") -> dict:
    if str(record.get("aweme_id")) != expected_id:
        raise CollectionError("resolved_resource_mismatch", "metadata")
    if not list(video_records(record)):
        raise CollectionError("media_formats_unavailable", "metadata")
    # Public share pages use null for an absent rendition list; the existing
    # upstream app parser expects this field to be iterable.
    record = {
        **record,
        "video": {**record["video"], "bit_rate": record["video"].get("bit_rate") or []},
    }
    # This is the same parser used by DouyinIE._real_extract. No signature code is
    # copied, and subtitle fetching is disabled; only the supplied page record is parsed.
    try:
        with YoutubeDL(
            {
                "quiet": True,
                "logger": QuietLogger(),
                "cachedir": False,
                "writesubtitles": False,
                "writeautomaticsub": False,
            }
        ) as client:
            info = DouyinIE(client)._parse_aweme_video_app(record)
            info = client.sanitize_info(info)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CollectionError("backend_parser_incompatible", "metadata") from None
    if not info.get("formats"):
        raise CollectionError("media_formats_unavailable", "metadata")
    info["webpage_url"] = f"https://www.douyin.com/video/{expected_id}"
    info["_backend"] = backend
    return info
