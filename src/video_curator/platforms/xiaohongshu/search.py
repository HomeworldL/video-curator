"""Xiaohongshu's own browser search, preserving the access token per result."""

from pathlib import Path
from urllib.parse import urlencode

from ...config import Runtime
from ...models import Candidate, CollectionError, Platform, SearchBatch, SearchRequest
from ...providers.browser import page as browser_page
from .session import access_issue, navigate

READ_FEEDS = Path(__file__).with_name("search_state.js").read_text(encoding="utf-8")


def candidates_from_feeds(feeds: list, request: SearchRequest) -> list[Candidate]:
    candidates = {}
    for position, feed in enumerate(feeds, 1):
        if not isinstance(feed, dict) or feed.get("modelType") not in (None, "note"):
            continue
        card = feed.get("noteCard") or {}
        # Search may return image posts despite a video parameter. Never relabel them.
        if card.get("type") != "video":
            continue
        note_id = feed.get("id")
        token = feed.get("xsecToken")
        if (
            not isinstance(note_id, str)
            or len(note_id) != 24
            or any(c not in "0123456789abcdefABCDEF" for c in note_id)
        ):
            continue
        if not token:
            continue  # A bare ID is insufficient for a usable signed detail URL.
        url = f"https://www.xiaohongshu.com/explore/{note_id}?" + urlencode(
            {"xsec_token": token, "xsec_source": "pc_search"}
        )
        user = card.get("user") or {}
        candidates[note_id] = Candidate(
            platform=Platform.XIAOHONGSHU,
            url=url,
            title=card.get("displayTitle"),
            author=user.get("nickname") or user.get("nickName"),
            query=request.query,
            mode="native",
            position=str(position),
        )
    return list(candidates.values())[: request.limit]


def search(request: SearchRequest, runtime: Runtime) -> SearchBatch:
    if request.platform != Platform.XIAOHONGSHU or request.mode != "native":
        raise CollectionError("unsupported_search_request", "search")
    if request.page != 1:
        raise CollectionError("native_search_has_limit_not_page_cursor", "search")
    url = "https://www.xiaohongshu.com/search_result?" + urlencode(
        {"keyword": request.query, "source": "web_explore_feed"}
    )
    found = {}
    with browser_page(runtime.platforms[Platform.XIAOHONGSHU], runtime, stage="search") as page:
        navigate(page, url, "search")
        page.wait_for_timeout(1200)
        no_growth = 0
        # Bound scrolls; this is a first-N search, not a persistent remote cursor.
        for _ in range(min(8, 2 + (request.limit + 4) // 5)):
            feeds = page.evaluate(READ_FEEDS)
            if not isinstance(feeds, list):
                feeds = []
            previous = len(found)
            for candidate in candidates_from_feeds(feeds, request):
                found[candidate.url] = candidate
            if len(found) >= request.limit:
                return SearchBatch(
                    request=request,
                    candidates=list(found.values())[: request.limit],
                    pagination="limit",
                    stop_reason="limit_reached",
                )
            issue = access_issue(page)
            if issue:
                if not found:
                    raise CollectionError(issue, "search")
                return SearchBatch(
                    request=request,
                    candidates=list(found.values()),
                    pagination="limit",
                    stop_reason=issue,
                )
            no_growth = no_growth + 1 if len(found) == previous else 0
            if no_growth >= 2:
                break
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 900))")
            page.wait_for_timeout(1000)
        if not found and page.evaluate(READ_FEEDS) is None:
            raise CollectionError("guest_search_unavailable", "search")
    return SearchBatch(
        request=request,
        candidates=list(found.values()),
        pagination="limit",
        stop_reason="bounded_scroll_end",
    )
