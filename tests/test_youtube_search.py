import pytest

from video_curator.config import Runtime
from video_curator.models import CollectionError, SearchRequest
from video_curator.platforms.youtube import search as native


def request(**changes):
    return SearchRequest(platform="youtube", query="LABUBU mascot dance shorts", limit=2, **changes)


def test_search_uses_youtube_native_extractor_and_preserves_limit_semantics(monkeypatch):
    calls = []

    def extract(url, settings, runtime, *, flat=False):
        calls.append((url, flat, settings.cookie_file))
        return {
            "entries": [
                {"id": "BaW_jenozKc", "url": "BaW_jenozKc", "title": "native title"},
                {"id": "9bZkp7q19f0", "webpage_url": "https://youtu.be/9bZkp7q19f0"},
            ]
        }

    monkeypatch.setattr(native.ytdlp, "extract", extract)
    batch = native.search(request(), Runtime())
    assert calls == [("ytsearch2:LABUBU mascot dance shorts", True, None)]
    assert batch.pagination == "limit" and batch.next_page is None
    assert batch.stop_reason == "limit_reached"
    assert batch.candidates[0].mode == "native"
    assert batch.candidates[0].title == "native title"
    assert batch.candidates[1].url == "https://youtu.be/9bZkp7q19f0"


def test_remote_page_cursor_is_not_invented(monkeypatch):
    monkeypatch.setattr(native.ytdlp, "extract", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(CollectionError, match="limit_not_page_cursor"):
        native.search(request(page=2), Runtime())


def test_missing_entries_is_not_a_successful_empty_search():
    with pytest.raises(CollectionError, match="native_search_invalid_response"):
        native._parse({"id": "query"}, request())


def test_empty_valid_response_does_not_claim_platform_exhaustion():
    batch = native._parse({"entries": []}, request())
    assert batch.candidates == []
    assert batch.stop_reason == "returned_batch_end"
    assert batch.next_page is None


def test_nonvideo_or_other_platform_results_are_not_imported():
    with pytest.raises(CollectionError, match="no_video_entries"):
        native._parse(
            {
                "entries": [
                    {"url": "https://youtube.com.evil.test/watch?v=BaW_jenozKc"},
                    {"url": "https://www.youtube.com/@somechannel"},
                    None,
                ]
            },
            request(),
        )


def test_response_cannot_exceed_requested_limit():
    entries = [
        {"url": f"https://youtu.be/{ident}"}
        for ident in ("BaW_jenozKc", "9bZkp7q19f0", "dOGwn68H9Kc")
    ]
    assert len(native._parse({"entries": entries}, request()).candidates) == 2
