"""Fetch text and part metadata without requesting player URLs or formats.

The public view/detail endpoint is also used by MediaCrawler. This adapter is
independently implemented with the project's existing httpx dependency.
"""

import re
import time
from http.cookiejar import MozillaCookieJar

import httpx

from ...config import Runtime
from ...identity import identify, platform_for_url
from ...models import Candidate, CollectionError, Platform
from .parts import has_explicit_part
from .search import HEADERS
from .urls import access_url

DETAIL_URL = "https://api.bilibili.com/x/web-interface/view/detail"


def _check_http(response: httpx.Response):
    if response.status_code == 429:
        raise CollectionError("rate_limited", "metadata")
    if response.status_code in (403, 412):
        raise CollectionError("platform_access_blocked", "metadata")
    if response.status_code >= 500:
        raise CollectionError("upstream_unavailable", "metadata")
    if not response.is_success and not response.is_redirect:
        raise CollectionError("native_detail_http_error", "metadata")


def _parse(body: dict, candidate: Candidate) -> dict:
    if not isinstance(body, dict):
        raise CollectionError("native_detail_invalid_response", "metadata")
    code = body.get("code")
    if code in (-352, -403, -412, -799):
        raise CollectionError("platform_access_blocked", "metadata")
    if code == -509:
        raise CollectionError("rate_limited", "metadata")
    if code == -101:
        raise CollectionError("needs_login", "metadata")
    if code in (-404, 62002):
        raise CollectionError("video_unavailable", "metadata")
    data = body.get("data")
    if body.get("v_voucher") or (isinstance(data, dict) and data.get("v_voucher")):
        raise CollectionError("verification_required", "metadata")
    if code != 0:
        raise CollectionError("native_detail_api_error", "metadata")
    if not isinstance(data, dict) or not isinstance(data.get("View"), dict):
        raise CollectionError("native_detail_invalid_response", "metadata")
    view = data["View"]
    bvid = view.get("bvid")
    pages, tags = view.get("pages"), data.get("Tags")
    if (
        not isinstance(bvid, str)
        or not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid)
        or not isinstance(pages, list)
        or not pages
        or not isinstance(tags, list)
        or not isinstance(view.get("title"), str)
    ):
        raise CollectionError("native_detail_invalid_response", "metadata")
    identity = identify(candidate.url)
    source_id = identity.content_id
    if source_id != bvid and source_id != f"av{view.get('aid')}":
        raise CollectionError("metadata_identity_mismatch", "metadata")
    parts = {}
    for page in pages:
        if (
            not isinstance(page, dict)
            or type(page.get("page")) is not int
            or page["page"] < 1
            or page["page"] in parts
        ):
            raise CollectionError("native_detail_invalid_response", "metadata")
        parts[page["page"]] = page
    part_number = int(identity.part_id)
    if part_number not in parts:
        raise CollectionError("video_part_unavailable", "metadata")
    part = parts[part_number]
    owner, stat = view.get("owner") or {}, view.get("stat") or {}
    if not isinstance(owner, dict) or not isinstance(stat, dict):
        raise CollectionError("native_detail_invalid_response", "metadata")
    title = view["title"]
    if len(parts) > 1:
        title += f" p{part_number:02d} {part.get('part') or ''}"
    result = {
        "id": bvid,
        "webpage_url": f"https://www.bilibili.com/video/{bvid}?p={part_number}",
        "title": title,
        "description": view.get("desc"),
        "tags": [tag["tag_name"] for tag in tags if isinstance(tag, dict) and tag.get("tag_name")],
        "uploader": owner.get("name"),
        "uploader_id": owner.get("mid"),
        "timestamp": view.get("pubdate"),
        "duration": part.get("duration", view.get("duration")),
        "thumbnail": view.get("pic"),
        "view_count": stat.get("view"),
        "like_count": stat.get("like"),
        "comment_count": stat.get("reply"),
        "cid": part.get("cid"),
        "pages": pages,
        "_backend": "bilibili-detail",
    }
    if not has_explicit_part(candidate.url):
        result["_collection_related_parts"] = [
            f"https://www.bilibili.com/video/{bvid}?p={number}"
            for number in sorted(parts)
            if number != part_number
        ]
    return result


def detail(candidate: Candidate, runtime: Runtime) -> dict:
    settings = runtime.platforms[Platform.BILIBILI]
    cookies = None
    if settings.cookie_file:
        cookies = MozillaCookieJar(str(settings.cookie_file))
        try:
            cookies.load(ignore_discard=True)
        except (OSError, ValueError):
            raise CollectionError("session_cookie_error", "metadata") from None
    last_request = None

    def pace(request):
        nonlocal last_request
        if last_request is not None:
            time.sleep(
                max(0, settings.request_interval_seconds - (time.monotonic() - last_request))
            )
        last_request = time.monotonic()

    with httpx.Client(
        headers={**HEADERS, "Referer": "https://www.bilibili.com/"},
        cookies=cookies,
        timeout=runtime.timeout_seconds,
        follow_redirects=False,
        event_hooks={"request": [pace]},
    ) as client:

        def get(url, **kwargs):
            # Only transient read/network/server failures retry. Risk signals stop
            # immediately; retries also pass through the HTTP request rate hook.
            for attempt in range(runtime.retries + 1):
                try:
                    response = client.get(url, **kwargs)
                    _check_http(response)
                    return response
                except httpx.TimeoutException:
                    error = CollectionError("timeout", "metadata")
                except httpx.HTTPError:
                    error = CollectionError("native_detail_network_error", "metadata")
                except CollectionError as exc:
                    if exc.code != "upstream_unavailable":
                        raise
                    error = exc
                if attempt == runtime.retries:
                    raise error
                time.sleep(min(30, max(2, settings.request_interval_seconds) * 2**attempt))

        url = access_url(candidate.url)
        identity = identify(url)
        for _ in range(6):
            if not identity.provisional:
                break
            response = get(url)
            if not response.is_redirect:
                raise CollectionError("unresolved_identity", "metadata")
            url = str(response.next_request.url)
            if platform_for_url(url) != Platform.BILIBILI:
                raise CollectionError("shortlink_platform_mismatch", "metadata")
            identity = identify(url)
        if identity.provisional:
            raise CollectionError("unresolved_identity", "metadata")
        source_id = identity.content_id
        params = {"aid": source_id[2:]} if source_id.startswith("av") else {"bvid": source_id}
        response = get(DETAIL_URL, params=params)
        if response.is_redirect:
            raise CollectionError("verification_required", "metadata")
        try:
            body = response.json()
        except ValueError:
            raise CollectionError("native_detail_invalid_response", "metadata") from None
        return _parse(body, candidate.model_copy(update={"url": url}))
