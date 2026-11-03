import httpx
import pytest

from video_curator.config import Runtime
from video_curator.models import CollectionError, SearchRequest
from video_curator.platforms.bilibili import search as native


def request(page=1):
    return SearchRequest(
        platform="bilibili", query="LABUBU 动画", mode="native", limit=5, page=page
    )


def test_native_search_uses_fresh_guest_cookie_and_exposes_real_page(monkeypatch):
    original_client = httpx.Client
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.path == "/":
            return httpx.Response(
                200, headers={"set-cookie": "buvid3=fresh-visitor; Domain=.bilibili.com; Path=/"}
            )
        assert req.headers["cookie"] == "buvid3=fresh-visitor"
        assert req.url.params["search_type"] == "video"
        assert req.url.params["page"] == "2"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "numPages": 3,
                    "result": [
                        {
                            "bvid": "BV17x411w7KC",
                            "aid": 170001,
                            "title": '<em class="keyword">LABUBU</em> &amp; 动画',
                            "author": "creator",
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(
        native.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    batch = native.search(request(2), Runtime())
    assert len(calls) == 2
    assert batch.pagination == "page" and batch.next_page == 3
    assert batch.candidates[0].url == "https://www.bilibili.com/video/BV17x411w7KC"
    assert batch.candidates[0].title == "LABUBU & 动画"
    assert batch.candidates[0].mode == "native"


def test_voucher_response_is_blocked_not_an_empty_success():
    with pytest.raises(CollectionError, match="platform_access_blocked"):
        native._parse({"code": 0, "data": {"v_voucher": "opaque"}}, request())


def test_empty_valid_search_can_be_exhausted():
    batch = native._parse({"code": 0, "data": {"result": [], "numPages": 0}}, request())
    assert not batch.candidates and batch.next_page is None
    assert batch.stop_reason == "returned_batch_end"


@pytest.mark.parametrize(
    "body,code",
    [
        ({"code": -101}, "needs_login"),
        ({"code": -352}, "platform_access_blocked"),
        ({"code": 0, "data": {}}, "native_search_invalid_response"),
        (
            {"code": 0, "data": {"result": [{"author": "not a video"}], "numPages": 1}},
            "native_search_no_video_entries",
        ),
    ],
)
def test_error_responses_are_not_candidates(body, code):
    with pytest.raises(CollectionError, match=code):
        native._parse(body, request())
