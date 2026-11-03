import re
from urllib.parse import parse_qs, urlsplit


def parse(url: str, raw: dict) -> tuple[str, str, str] | None:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    video_id = raw.get("id") or query.get("v", [None])[0]
    if not video_id:
        segments = parts.path.strip("/").split("/")
        if parts.hostname == "youtu.be":
            video_id = segments[0]
        elif len(segments) > 1 and segments[0] in ("shorts", "embed", "live"):
            video_id = segments[1]
    if video_id and re.fullmatch(r"[\w-]{11}", str(video_id)):
        return str(video_id), "", f"https://www.youtube.com/watch?v={video_id}"
    return None
