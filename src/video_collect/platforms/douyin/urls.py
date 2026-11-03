import re
from urllib.parse import parse_qs, urlsplit


def parse(url: str, raw: dict) -> tuple[str, str, str] | None:
    video_id = str(raw.get("id") or "")
    if not video_id.isdecimal():
        match = re.search(r"/(?:share/)?video/(\d+)", urlsplit(url).path)
        video_id = match[1] if match else parse_qs(urlsplit(url).query).get("modal_id", [""])[0]
    if video_id.isdecimal():
        return video_id, "", f"https://www.douyin.com/video/{video_id}"
    return None
