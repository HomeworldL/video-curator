"""Read Douyin's public mobile share page using a new anonymous HTTP session."""

import json
import re
from urllib.parse import urljoin, urlsplit

import httpx

from ...config import Runtime
from ...models import CollectionError
from .parsing import parse_video, video_records
from .session import gate_code

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


def parse_share(html: str, content_id: str) -> dict:
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    code = gate_code(title[1] if title else "", "")
    if code:
        raise CollectionError(code, "metadata")
    match = re.search(r"window\._ROUTER_DATA\s*=\s*", html)
    if not match:
        raise CollectionError("share_page_state_unavailable", "metadata")
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end() :])
    except ValueError:
        raise CollectionError("share_page_state_invalid", "metadata") from None
    for record in video_records(data):
        if str(record["aweme_id"]) == content_id:
            return parse_video(record, content_id, backend="douyin-share")
    raise CollectionError("share_video_unavailable", "metadata")


def fetch(content_id: str, runtime: Runtime) -> tuple[dict, list[dict]]:
    if not content_id.isdecimal():
        raise CollectionError("invalid_resource_id", "metadata")
    url = f"https://www.iesdouyin.com/share/video/{content_id}/"
    try:
        with httpx.Client(
            timeout=runtime.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": MOBILE_USER_AGENT},
        ) as client:
            for _ in range(6):
                response = client.get(url)
                if not response.is_redirect:
                    break
                url = urljoin(url, response.headers["location"])
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or parts.hostname not in (
                    "www.iesdouyin.com",
                    "www.douyin.com",
                    "iesdouyin.com",
                    "douyin.com",
                ):
                    raise CollectionError("share_redirect_platform_mismatch", "metadata")
            else:
                raise CollectionError("share_redirect_limit", "metadata")
            if response.status_code == 429:
                raise CollectionError("rate_limited", "metadata")
            if response.status_code != 200:
                raise CollectionError("share_page_unavailable", "metadata")
            try:
                info = parse_share(response.text, content_id)
            except CollectionError as exc:
                # The first public share response may only initialize a visitor
                # cookie. A single normal reload can then include videoInfoRes.
                # Never retry a CAPTCHA gate or invent a logged-in session.
                if (
                    exc.code not in ("share_video_unavailable", "share_page_state_unavailable")
                    or not client.cookies
                ):
                    raise
                response = client.get(url)
                if response.status_code != 200:
                    raise CollectionError("share_page_unavailable", "metadata") from None
                info = parse_share(response.text, content_id)
            info["http_headers"] = {"User-Agent": MOBILE_USER_AGENT, "Referer": url}
            cookies = [
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "secure": c.secure,
                    **({"expires": c.expires} if c.expires else {}),
                }
                for c in client.cookies.jar
            ]
            return info, cookies
    except httpx.HTTPError:
        raise CollectionError("share_request_failed", "metadata") from None
