import re
from urllib.parse import parse_qs, urlsplit


def access_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"^/[a-z]{2}(?:-[A-Za-z]+)?/video/", "/video/", parts.path)
    return parts._replace(path=path).geturl()


def parse(url: str, raw: dict) -> tuple[str, str, str] | None:
    # yt-dlp returns a BV id (possibly _pN) even when the input uses an AV id.
    source = str(raw.get("id") or raw.get("bvid") or url)
    match = re.search(r"(BV[A-Za-z0-9]{10}|[aA][vV]\d+)", source)
    if not match:
        return None
    content_id = match[1]
    if content_id[:2].lower() == "av":
        content_id = "av" + content_id[2:]
    part = parse_qs(urlsplit(url).query).get("p", ["1"])[0]
    extracted_part = re.search(r"_p(\d+)$", source)
    if extracted_part:
        part = extracted_part[1]
    if not part.isdecimal() or int(part) < 1:
        return None
    # Ordinal pN is stable within a post and retained in its canonical access URL.
    part = str(int(part))
    return content_id, part, f"https://www.bilibili.com/video/{content_id}?p={part}"
