import json
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest

from video_collect.config import Runtime
from video_collect.identity import identify
from video_collect.media import inspect
from video_collect.models import (
    Candidate,
    CollectionError,
    DownloadRequest,
    SearchRequest,
    VideoMetadata,
)
from video_collect.platforms.xiaohongshu import download as download_module
from video_collect.platforms.xiaohongshu import search as search_module
from video_collect.platforms.xiaohongshu.detail import formats_from_note, media_url, normalize_note
from video_collect.platforms.xiaohongshu.search import candidates_from_feeds
from video_collect.platforms.xiaohongshu.session import access_issue

NOTE_ID = "674051740000000007027a15"
URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=example"


def test_native_search_preserves_token_and_only_returns_videos():
    request = SearchRequest(platform="xiaohongshu", query="LABUBU", limit=3)
    feeds = [
        {
            "id": NOTE_ID,
            "modelType": "note",
            "xsecToken": "signed+/=",
            "noteCard": {"type": "video", "displayTitle": "Publisher title"},
        },
        {
            "id": "1234567890abcdef12345678",
            "modelType": "note",
            "xsecToken": "token",
            "noteCard": {"type": "normal"},
        },
        {
            "id": "1234567890abcdef12345679",
            "modelType": "live_v2",
            "xsecToken": "token",
            "noteCard": {"type": "video"},
        },
        {"id": "1234567890abcdef12345670", "modelType": "note", "noteCard": {"type": "video"}},
    ]
    candidates = candidates_from_feeds(feeds, request)
    assert len(candidates) == 1
    assert "xsec_token=signed%2B%2F%3D" in candidates[0].url
    assert candidates[0].mode == "native"
    assert identify(candidates[0].url).content_id == NOTE_ID


def test_pagination_is_not_faked():
    with pytest.raises(CollectionError, match="limit_not_page_cursor"):
        search_module.search(
            SearchRequest(platform="xiaohongshu", query="LABUBU", page=2), Runtime()
        )


def test_visible_login_wall_is_an_error_not_empty_success(monkeypatch):
    page = Mock()
    page.evaluate.return_value = []

    @contextmanager
    def browser(*args, **kwargs):
        yield page

    monkeypatch.setattr(search_module, "browser_page", browser)
    monkeypatch.setattr(search_module, "navigate", lambda *args: None)
    monkeypatch.setattr(search_module, "access_issue", lambda page: "needs_login")
    with pytest.raises(CollectionError, match="needs_login"):
        search_module.search(SearchRequest(platform="xiaohongshu", query="LABUBU"), Runtime())


def test_search_reads_results_after_first_scroll(monkeypatch):
    page = Mock()
    states = iter(
        [
            None,
            [
                {
                    "id": NOTE_ID,
                    "xsecToken": "token",
                    "modelType": "note",
                    "noteCard": {"type": "video"},
                }
            ],
        ]
    )
    page.evaluate.side_effect = lambda script: (
        next(states) if script == search_module.READ_FEEDS else None
    )

    @contextmanager
    def browser(*args, **kwargs):
        yield page

    monkeypatch.setattr(search_module, "browser_page", browser)
    monkeypatch.setattr(search_module, "navigate", lambda *args: None)
    monkeypatch.setattr(search_module, "access_issue", lambda page: None)
    result = search_module.search(
        SearchRequest(platform="xiaohongshu", query="LABUBU", limit=1), Runtime()
    )
    assert len(result.candidates) == 1


def test_missing_page_state_is_not_a_successful_search(monkeypatch):
    page = Mock()
    page.evaluate.return_value = None

    @contextmanager
    def browser(*args, **kwargs):
        yield page

    monkeypatch.setattr(search_module, "browser_page", browser)
    monkeypatch.setattr(search_module, "navigate", lambda *args: None)
    monkeypatch.setattr(search_module, "access_issue", lambda page: None)
    with pytest.raises(CollectionError, match="guest_search_unavailable"):
        search_module.search(SearchRequest(platform="xiaohongshu", query="LABUBU"), Runtime())


def test_ip_risk_is_not_misreported_as_missing_login():
    page = Mock()
    page.locator.return_value.inner_text.return_value = (
        "安全限制\nIP存在风险，请切换可靠网络环境后重试\n300012"
    )
    assert access_issue(page) == "platform_access_blocked"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/v.mp4",
        "https://xhscdn.com.evil.test/v.mp4",
        "file:///etc/passwd",
        "https://user:secret@sns-video-bd.xhscdn.com/v.mp4",
    ],
)
def test_download_does_not_follow_untrusted_media_hosts(url):
    assert media_url(url) is None


def test_media_v2_and_millisecond_stream_duration_are_supported():
    streams = {
        "h264": [
            {
                "masterUrl": "https://sns-video-bd.xhscdn.com/example.mp4",
                "width": 1920,
                "height": 1080,
                "duration": 2500,
                "size": 100,
            }
        ]
    }
    note = {"video": {"mediaV2": json.dumps({"media": {"stream": streams}})}}
    formats = formats_from_note(note)
    assert formats[0]["duration"] == 2.5
    assert formats[0]["height"] == 1080


def test_detail_never_accepts_another_note_or_an_image_post():
    candidate = Candidate(platform="xiaohongshu", url=URL)
    with pytest.raises(CollectionError, match="identity_mismatch"):
        normalize_note({"noteId": "1234567890abcdef12345678", "type": "video"}, candidate)
    with pytest.raises(CollectionError, match="not_a_video"):
        normalize_note({"noteId": NOTE_ID, "type": "normal"}, candidate)


def _download_request(tmp_path, max_bytes):
    return DownloadRequest(
        metadata=VideoMetadata(
            identity=identify(URL),
            access_url=URL,
            raw={
                "_backend": "xiaohongshu-browser",
                "formats": [{"url": "https://sns-video-bd.xhscdn.com/source.mp4"}],
            },
        ),
        directory=tmp_path,
        max_bytes=max_bytes,
    )


def test_stream_download_obeys_actual_byte_budget(tmp_path, monkeypatch):
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"a" * 200))
    monkeypatch.setattr(
        download_module.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs)
    )
    with pytest.raises(CollectionError, match="download_byte_budget"):
        download_module.download(_download_request(tmp_path, 100), Runtime())
    assert not (tmp_path / "source.mp4").exists()


def test_downloaded_stream_is_a_real_readable_video(tmp_path, monkeypatch, tiny_video):
    payload = tiny_video.read_bytes()
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    monkeypatch.setattr(
        download_module.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = download_module.download(_download_request(tmp_path, len(payload) + 10), Runtime())
    assert result.backend == "xiaohongshu-browser"
    assert inspect(result.path).duration_seconds == pytest.approx(2, abs=0.1)


def test_untrusted_redirect_is_not_fetched(tmp_path, monkeypatch):
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://evil.test/media"})
    )
    monkeypatch.setattr(
        download_module.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs)
    )
    with pytest.raises(CollectionError, match="media_redirect_untrusted"):
        download_module.download(_download_request(tmp_path, 100), Runtime())
