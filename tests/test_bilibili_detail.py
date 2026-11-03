import httpx
import pytest

from video_curator import download, metadata
from video_curator.config import Runtime
from video_curator.models import Candidate, CollectionError, DownloadRequest
from video_curator.platforms.bilibili import detail as module

BV = "BV17x411w7KC"
URL = f"https://www.bilibili.com/video/{BV}"


def payload(parts=1):
    return {
        "code": 0,
        "data": {
            "View": {
                "bvid": BV,
                "aid": 170001,
                "title": "测试视频",
                "desc": "简介",
                "pic": "https://i0.hdslb.com/test.jpg",
                "pubdate": 1700000000,
                "duration": 999,
                "owner": {"mid": 123, "name": "作者"},
                "pages": [
                    {"page": i, "cid": 100 + i, "part": f"片段{i}", "duration": 10 * i}
                    for i in range(1, parts + 1)
                ],
            },
            "Tags": [{"tag_name": "动画"}],
            "Related": [{"bvid": "must-not-be-followed"}],
        },
    }


def setup_client(monkeypatch, handler):
    original = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        return handler(request, len(requests))

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(module.httpx, "Client", lambda **kw: original(transport=transport, **kw))
    return requests


def test_auto_metadata_is_one_request_without_player_or_formats(monkeypatch, tmp_path):
    calls = setup_client(monkeypatch, lambda req, n: httpx.Response(200, json=payload()))
    monkeypatch.setattr(metadata.ytdlp, "extract", lambda *a, **kw: pytest.fail("no extraction"))
    details = metadata.fetch(Candidate(platform="bilibili", url=URL), Runtime())
    assert len(calls) == 1
    assert calls[0].url.path == "/x/web-interface/view/detail"
    assert calls[0].url.params["bvid"] == BV
    assert details.title == "测试视频" and details.tags == ["动画"]
    assert details.description == "简介" and details.duration_seconds == 10
    assert details.author_id == "123" and details.published_at
    assert "formats" not in details.raw and "Related" not in details.raw
    assert details.raw["_collection_related_parts"] == []
    captured = []
    monkeypatch.setattr(
        download.ytdlp, "download", lambda *a: captured.append(a[0]) or tmp_path / "x.mp4"
    )
    download.obtain(
        DownloadRequest(metadata=details, directory=tmp_path, max_bytes=1024), Runtime()
    )
    assert captured == [f"{URL}?p=1"]


def test_av_and_part_identity_duration_and_related_parts(monkeypatch):
    calls = setup_client(monkeypatch, lambda req, n: httpx.Response(200, json=payload(3)))
    first = metadata.fetch(Candidate(platform="bilibili", url=URL), Runtime())
    assert first.raw["_collection_related_parts"] == [f"{URL}?p=2", f"{URL}?p=3"]
    part = metadata.fetch(
        Candidate(platform="bilibili", url="https://www.bilibili.com/video/av170001?p=2"), Runtime()
    )
    assert calls[-1].url.params["aid"] == "170001"
    assert part.identity.key == f"bilibili:{BV}:2"
    assert part.duration_seconds == 20 and part.raw["cid"] == 102
    assert part.title.endswith("p02 片段2")
    assert "_collection_related_parts" not in part.raw


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({}, 429, "rate_limited"),
        ({}, 412, "platform_access_blocked"),
        ({}, 403, "platform_access_blocked"),
        ({"code": -352}, 200, "platform_access_blocked"),
        ({"code": -799}, 200, "platform_access_blocked"),
        ({"code": -509}, 200, "rate_limited"),
        ({"code": -101}, 200, "needs_login"),
        ({"code": 0, "data": {"v_voucher": "challenge"}}, 200, "verification_required"),
        ({"code": -404}, 200, "video_unavailable"),
        ({"code": 0, "data": {}}, 200, "native_detail_invalid_response"),
    ],
)
def test_risk_and_invalid_responses_do_not_retry_or_fallback(monkeypatch, body, status, code):
    calls = setup_client(monkeypatch, lambda req, n: httpx.Response(status, json=body))
    with pytest.raises(CollectionError, match=code):
        metadata.fetch(Candidate(platform="bilibili", url=URL), Runtime(retries=3))
    assert len(calls) == 1


def test_shortlink_and_transient_retry_are_paced(monkeypatch):
    clock = [0.0]
    timestamps = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def respond(req, number):
        timestamps.append(clock[0])
        if number == 1:
            return httpx.Response(302, headers={"Location": URL})
        if number == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=payload())

    calls = setup_client(monkeypatch, respond)
    result = metadata.fetch(Candidate(platform="bilibili", url="https://b23.tv/example"), Runtime())
    assert result.identity.content_id == BV
    assert len(calls) == 3 and timestamps == [0, 2, 4]


def test_shortlink_never_follows_another_platform(monkeypatch):
    calls = setup_client(
        monkeypatch,
        lambda req, n: httpx.Response(
            302, headers={"Location": "https://www.youtube.com/watch?v=BaW_jenozKc"}
        ),
    )
    with pytest.raises(CollectionError, match="shortlink_platform_mismatch"):
        metadata.fetch(Candidate(platform="bilibili", url="https://b23.tv/example"), Runtime())
    assert len(calls) == 1


def test_missing_part_and_mismatched_video_fail_closed(monkeypatch):
    setup_client(monkeypatch, lambda req, n: httpx.Response(200, json=payload()))
    for url, error in [
        (URL + "?p=2", "video_part_unavailable"),
        ("https://www.bilibili.com/video/BV1wmpb6VEu9", "metadata_identity_mismatch"),
    ]:
        with pytest.raises(CollectionError, match=error):
            metadata.fetch(Candidate(platform="bilibili", url=url), Runtime())
