"""Shared DDGS search. Does not decide whether a search hit is relevant."""

from ddgs import DDGS
from ddgs.exceptions import DDGSException

from ..models import CollectionError


def search(query: str, page_size: int, page: int, timeout_seconds: float) -> list[dict]:
    try:
        return list(
            DDGS(timeout=int(timeout_seconds)).text(query, max_results=page_size, page=page)
        )
    except DDGSException:
        raise CollectionError("web_search_error", "search") from None
