from contextlib import contextmanager
from importlib import import_module
from urllib.parse import quote

import pytest

from video_curator.config import Runtime
from video_curator.models import CollectionError, Platform, SearchRequest
from video_curator.platforms.douyin.parsing import parse_video, video_records
from video_curator.platforms.douyin.session import (
    detail_response_url,
    gate_code,
    page_gate,
    search_response_url,
)

ID = "7289029835526114620"


def video_record(content_id=ID):
    return {
        "aweme_id": content_id,
        "desc": "video publisher description",
        "create_time": 1700000000,
        "author": {"uid": "123", "nickname": "publisher"},
        "video": {
            "width": 160,
            "height": 120,
            "duration": 2000,
            "play_addr": {"url_list": ["https://v3-web.douyinvod.com/example.mp4"]},
        },
    }


def test_existing_ytdlp_parser_reads_official_record_without_network(monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("Page parsing must not fetch the unsigned detail API or another platform")

    monkeypatch.setattr("yt_dlp.YoutubeDL.urlopen", network_forbidden)
    info = parse_video(video_record(), ID)
    assert info["id"] == ID
    assert info["duration"] == 2
    assert info["description"] == "video publisher description"
    assert info["channel"] == "publisher"
    assert info["formats"][0]["url"].startswith("https://v3-web.douyinvod.com/")
    assert info["_backend"] == "douyin-browser"
    assert "cookies" not in info


def test_detail_identity_mismatch_is_rejected_before_parsing():
    with pytest.raises(CollectionError, match="resolved_resource_mismatch"):
        parse_video(video_record("99999"), ID)


def test_video_records_exclude_image_posts_and_unrelated_ids():
    data = {
        "data": [
            {"aweme_info": video_record()},
            {"aweme_info": {"aweme_id": "22", "images": [{}]}},
            {"id": "33", "video": {}},
        ]
    }
    assert [r["aweme_id"] for r in video_records(data)] == [ID]


def test_native_search_requires_matching_site_endpoint_and_query():
    url = "https://www.douyin.com/aweme/v1/web/search/item/?keyword=" + quote("拉布布")
    assert search_response_url(url, "拉布布")
    assert not search_response_url(url, "cats")
    assert not search_response_url(
        "https://evil.test/aweme/v1/web/search/item/?keyword=cats", "cats"
    )
    assert not search_response_url(
        "https://www.douyin.com/aweme/v1/web/tab/feed/?keyword=cats", "cats"
    )
    assert detail_response_url(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + ID, ID
    )
    assert not detail_response_url(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=99", ID
    )


def test_explicit_login_and_captcha_are_distinct_from_generic_navbar():
    assert gate_code("抖音", "登录 注册 视频 搜索") is None
    assert gate_code("验证码中间页", "") == "verification_required"
    assert gate_code("抖音搜索", "请登录后搜索") == "needs_login"


class Response:
    status = 200

    def __init__(self, body):
        self.url = "https://www.douyin.com/aweme/v1/web/search/item/?keyword=cats"
        self.body = body

    def json(self):
        return self.body


class Active:
    url = "about:blank"

    def __init__(self, response):
        self.response = response
        self.mouse = self

    def on(self, name, listener):
        self.listener = listener

    def goto(self, url, **kwargs):
        self.url = url
        if "/search/" in url:
            self.listener(self.response)

    def wait_for_timeout(self, value):
        pass

    def wheel(self, *args):
        pass

    def title(self):
        return "抖音"

    def locator(self, selector):
        return self

    def inner_text(self, **kwargs):
        return "搜索"


def test_native_query_returns_only_page_video_records(monkeypatch):
    module = import_module("video_curator.platforms.douyin.search")
    active = Active(
        Response({"status_code": 0, "data": [{"aweme_info": video_record()}], "has_more": False})
    )

    @contextmanager
    def page(*args, **kwargs):
        yield active

    monkeypatch.setattr(module, "page", page)
    result = module.search(
        SearchRequest(platform=Platform.DOUYIN, query="cats", limit=2), Runtime()
    )
    assert result.candidates[0].url.endswith("/video/" + ID)
    assert result.candidates[0].mode == "native"
    assert result.pagination == "limit" and result.next_page is None
    assert result.stop_reason == "returned_batch_end"


def test_missing_or_blocked_response_is_not_successful_empty_search(monkeypatch):
    module = import_module("video_curator.platforms.douyin.search")
    active = Active(Response({"status_code": 8, "data": []}))

    @contextmanager
    def page(*args, **kwargs):
        yield active

    monkeypatch.setattr(module, "page", page)
    monkeypatch.setattr(
        module, "wait_for", lambda active, ready, runtime, stage: page_gate(active, stage)
    )
    with pytest.raises(CollectionError, match="guest_search_unavailable"):
        module.search(SearchRequest(platform=Platform.DOUYIN, query="cats", limit=2), Runtime())


def test_public_share_hydration_null_renditions_and_exact_identity():
    import json

    from video_curator.platforms.douyin.share import parse_share

    record = video_record()
    record["video"]["bit_rate"] = None
    html = (
        "<script>window._ROUTER_DATA = "
        + json.dumps({"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [record]}}}})
        + ";</script>"
    )
    result = parse_share(html, ID)
    assert result["_backend"] == "douyin-share"
    assert result["id"] == ID
    with pytest.raises(CollectionError, match="share_video_unavailable"):
        parse_share(html, "99999")


def test_public_share_gate_and_missing_state_are_not_empty_metadata():
    from video_curator.platforms.douyin.share import parse_share

    with pytest.raises(CollectionError, match="verification_required"):
        parse_share("<html><title>验证码中间页</title></html>", ID)
    with pytest.raises(CollectionError, match="share_page_state_unavailable"):
        parse_share("<html></html>", ID)


def test_public_share_initializes_only_new_visitor_and_reloads_once(monkeypatch):
    import json
    from http.cookiejar import Cookie

    from video_curator.platforms.douyin import share

    empty = '<script>window._ROUTER_DATA = {"loaderData": {}};</script>'
    full = (
        "<script>window._ROUTER_DATA = "
        + json.dumps({"loaderData": {"videoInfoRes": {"item_list": [video_record()]}}})
        + ";</script>"
    )
    calls = []

    class Cookies:
        jar = [
            Cookie(
                0,
                "visitor",
                "new-test-value",
                None,
                False,
                ".iesdouyin.com",
                True,
                True,
                "/",
                True,
                False,
                None,
                True,
                None,
                None,
                {},
                False,
            )
        ]

        def __bool__(self):
            return True

    class Client:
        cookies = Cookies()

        def __init__(self, **kwargs):
            assert "cookies" not in kwargs
            assert "Cookie" not in kwargs["headers"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            calls.append(url)
            return type(
                "Reply",
                (),
                {
                    "is_redirect": False,
                    "status_code": 200,
                    "text": empty if len(calls) == 1 else full,
                },
            )()

    monkeypatch.setattr(share.httpx, "Client", Client)
    info, cookies = share.fetch(ID, Runtime())
    assert info["id"] == ID and len(calls) == 2
    assert calls[0] == calls[1]
    assert cookies[0]["name"] == "visitor"
