"""Anonymous Bilibili native search with a fresh server-issued visitor session.

Uses the same public ``search/type`` endpoint as yt-dlp's BiliBiliSearchIE.
Unlike the extractor's synthetic buvid3, bootstrap a normal anonymous homepage
request so the platform can issue its own visitor cookies. No account login,
cookie files or existing browser profiles are accessed.
"""

import html
import re

import httpx

from ...config import Runtime
from ...models import Candidate, CollectionError, Platform, SearchBatch, SearchRequest

HOME_URL = "https://www.bilibili.com/"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Referer": "https://search.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


def _response_json(response: httpx.Response) -> dict:
    if response.status_code in (403, 412):
        raise CollectionError("platform_access_blocked", "search")
    if response.status_code == 429:
        raise CollectionError("rate_limited", "search")
    if not response.is_success:
        raise CollectionError("native_search_http_error", "search")
    try:
        body = response.json()
    except ValueError:
        raise CollectionError("native_search_invalid_response", "search") from None
    if not isinstance(body, dict):
        raise CollectionError("native_search_invalid_response", "search")
    return body


def _parse(body: dict, request: SearchRequest) -> SearchBatch:
    code = body.get("code")
    if code in (-352, -412):
        raise CollectionError("platform_access_blocked", "search")
    if code == -101:
        raise CollectionError("needs_login", "search")
    if code != 0:
        raise CollectionError("native_search_api_error", "search")
    data = body.get("data")
    if not isinstance(data, dict):
        raise CollectionError("native_search_invalid_response", "search")
    # Risk-control responses can have code=0 and only a voucher. They are not
    # evidence that the query has no results or the remote search is exhausted.
    if data.get("v_voucher"):
        raise CollectionError("platform_access_blocked", "search")
    if not isinstance(data.get("result"), list):
        raise CollectionError("native_search_invalid_response", "search")
    candidates = []
    for position, entry in enumerate(data["result"], 1):
        if not isinstance(entry, dict):
            continue
        bvid = str(entry.get("bvid") or "")
        aid = str(entry.get("aid") or "")
        if re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            content_id = bvid
        elif aid.isdecimal():
            content_id = "av" + aid
        else:
            continue
        title = entry.get("title")
        if isinstance(title, str):
            title = html.unescape(re.sub(r"<[^>]*>", "", title))
        else:
            title = None
        candidates.append(
            Candidate(
                platform=Platform.BILIBILI,
                url=f"https://www.bilibili.com/video/{content_id}",
                title=title,
                author=entry.get("author") if isinstance(entry.get("author"), str) else None,
                query=request.query,
                mode="native",
                position=f"{request.page}:{position}",
            )
        )
    if data["result"] and not candidates:
        raise CollectionError("native_search_no_video_entries", "search")
    total_pages = data.get("numPages")
    next_page = (
        request.page + 1 if isinstance(total_pages, int) and request.page < total_pages else None
    )
    if not isinstance(total_pages, int) and data["result"]:
        raise CollectionError("native_search_pagination_missing", "search")
    return SearchBatch(
        request=request,
        candidates=candidates[: request.limit],
        pagination="page",
        next_page=next_page,
        stop_reason="page_limit" if next_page else "returned_batch_end",
        raw=body,
    )


def search(request: SearchRequest, runtime: Runtime) -> SearchBatch:
    if request.platform != Platform.BILIBILI or request.mode != "native":
        raise CollectionError("bilibili_native_request_required", "search")
    params = {
        "search_type": "video",
        "keyword": request.query,
        "page": request.page,
        "page_size": min(request.limit, 50),
        "order": "totalrank",
    }
    try:
        with httpx.Client(
            headers=HEADERS, timeout=runtime.timeout_seconds, follow_redirects=True
        ) as client:
            home = client.get(HOME_URL)
            if home.status_code in (403, 412):
                raise CollectionError("platform_access_blocked", "search")
            if home.status_code == 429:
                raise CollectionError("rate_limited", "search")
            if not home.is_success:
                raise CollectionError("visitor_session_bootstrap_failed", "search")
            return _parse(_response_json(client.get(SEARCH_URL, params=params)), request)
    except httpx.TimeoutException:
        raise CollectionError("timeout", "search") from None
    except httpx.HTTPError:
        raise CollectionError("native_search_network_error", "search") from None
