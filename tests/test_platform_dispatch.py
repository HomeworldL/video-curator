import json
import shutil
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from video_curator import download, metadata, search
from video_curator.config import Runtime, Topic
from video_curator.identity import identify
from video_curator.models import (
    Candidate,
    CollectionError,
    DownloadRequest,
    DownloadResult,
    Platform,
    SearchBatch,
    SearchRequest,
    VideoMetadata,
)
from video_curator.providers import ytdlp


def test_shared_search_terms_apply_to_every_platform_and_keep_optional_additions():
    topic = Topic(
        id="subject",
        search={
            "shared_queries": ["subject animation", "subject dance"],
            "queries": {Platform.BILIBILI: ["subject dance", "subject mascot"]},
        },
    )
    planned = search.plan(topic, Runtime(), page_size=5)
    for platform in Platform:
        terms = [request.query for request in planned if request.platform == platform]
        expected = ["subject animation", "subject dance"]
        if platform == Platform.BILIBILI:
            expected.append("subject mascot")
        assert terms == expected


def test_web_search_ignores_bare_shortlink_host_and_non_video_page(monkeypatch):
    monkeypatch.setattr(
        search.web,
        "search",
        lambda *args: [
            {"href": "https://v.douyin.com/"},
            {"href": "https://www.douyin.com/search/LABUBU"},
            {"href": "https://v.douyin.com/abc123/"},
            {"href": "https://www.douyin.com/video/7289029835526114620"},
        ],
    )
    batch = search.discover(
        SearchRequest(platform=Platform.DOUYIN, query="LABUBU", mode="web", limit=10),
        Runtime(),
    )
    assert [candidate.url for candidate in batch.candidates] == [
        "https://v.douyin.com/abc123/",
        "https://www.douyin.com/video/7289029835526114620",
    ]


@pytest.mark.parametrize("platform", list(Platform))
def test_native_dispatch_does_not_call_external_search(platform, monkeypatch):
    request = SearchRequest(platform=platform, query="robot", mode="native", limit=2)
    expected = SearchBatch(
        request=request, candidates=[], pagination="limit", stop_reason="returned_batch_end"
    )
    monkeypatch.setitem(search.NATIVE_SEARCH, platform, lambda req, runtime: expected)

    def external(*args, **kwargs):
        pytest.fail("Native discovery must not silently use external search")

    monkeypatch.setattr(search.web, "search", external)
    assert search.discover(request, Runtime()) is expected


def test_native_access_failure_is_preserved(monkeypatch):
    def blocked(*args):
        raise CollectionError("platform_access_blocked", "search")

    monkeypatch.setitem(search.NATIVE_SEARCH, Platform.XIAOHONGSHU, blocked)
    with pytest.raises(CollectionError, match="platform_access_blocked"):
        search.discover(SearchRequest(platform="xiaohongshu", query="robot"), Runtime())


@pytest.mark.parametrize(
    "platform,url,source_id,backend",
    [
        (
            "douyin",
            "https://www.douyin.com/video/7289029835526114620",
            "7289029835526114620",
            "douyin-share",
        ),
        (
            "xiaohongshu",
            "https://www.xiaohongshu.com/explore/674051740000000007027a15?xsec_token=example",
            "674051740000000007027a15",
            "xiaohongshu-browser",
        ),
    ],
)
def test_specialized_detail_is_normalized_once(platform, url, source_id, backend, monkeypatch):
    module = __import__(f"video_curator.platforms.{platform}.detail", fromlist=["detail"])
    monkeypatch.setattr(
        module,
        "detail",
        lambda candidate, runtime: {
            "id": source_id,
            "title": "robot",
            "duration": 3.25,
            "_backend": backend,
        },
    )
    details = metadata.fetch(Candidate(platform=platform, url=url), Runtime())
    assert details.identity.key == identify(url).key
    assert details.duration_seconds == 3.25
    assert details.raw["_backend"] == backend
    if platform == "xiaohongshu":
        assert details.access_url == url


@pytest.mark.parametrize(
    "platform,url,backend",
    [
        ("douyin", "https://www.douyin.com/video/7289029835526114620", "douyin-share"),
        (
            "xiaohongshu",
            "https://www.xiaohongshu.com/explore/674051740000000007027a15",
            "xiaohongshu-browser",
        ),
    ],
)
def test_download_uses_the_backend_that_produced_metadata(
    platform, url, backend, monkeypatch, tmp_path
):
    module = __import__(f"video_curator.platforms.{platform}.download", fromlist=["download"])
    expected = DownloadResult(path=tmp_path / "source.mp4", backend=backend)
    monkeypatch.setattr(module, "download", lambda req, runtime: expected)
    details = VideoMetadata(identity=identify(url), access_url=url, raw={"_backend": backend})
    assert (
        download.obtain(
            DownloadRequest(metadata=details, directory=tmp_path, max_bytes=1024), Runtime()
        )
        is expected
    )


def test_preparsed_download_never_reextracts_url_or_mutates_input(
    monkeypatch, tmp_path, tiny_video
):
    clients = []

    class Client:
        def __init__(self, opts):
            self.opts = opts
            self.cookiejar = CookieJar()
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            pytest.fail("Prepared formats must not re-enter the unsigned detail API")

        def process_ie_result(self, info, download):
            info["title"] = "mutated by upstream"
            shutil.copyfile(tiny_video, Path(self.opts["outtmpl"]).parent / "source.mp4")
            return info

    monkeypatch.setattr(ytdlp, "YoutubeDL", Client)
    raw = {
        "id": "sample",
        "title": "original",
        "formats": [{"url": "https://media.example/test.mp4"}],
    }
    visitor = [
        {"name": "visitor", "value": "fixture", "domain": ".douyin.com", "path": "/", "expires": -1}
    ]
    output = ytdlp.download_info(
        raw,
        tmp_path,
        Runtime().platforms[Platform.DOUYIN],
        Runtime(),
        32 * 1024**2,
        cookies=visitor,
    )
    assert output.is_file() and raw["title"] == "original"
    assert len(clients[0].cookiejar) == 1
    assert not list(tmp_path.glob("*.cookies*"))


def test_cli_native_failure_has_nonzero_exit(collection, monkeypatch, tmp_path, capsys):
    import yaml

    from video_curator.cli import main

    job, _, _ = collection
    topic = tmp_path / "topic.yaml"
    runtime = tmp_path / "runtime.yaml"
    topic.write_text(yaml.safe_dump(job.topic.model_dump(mode="json")))
    runtime.write_text(yaml.safe_dump(job.runtime.model_dump(mode="json")))

    def blocked(*args):
        raise CollectionError("verification_required", "search")

    monkeypatch.setitem(search.NATIVE_SEARCH, Platform.DOUYIN, blocked)
    code = main(
        [
            "--topic",
            str(topic),
            "--runtime",
            str(runtime),
            "search",
            "--platform",
            "douyin",
            "--mode",
            "native",
            "--topic",
            "robot",
            "--page-size",
            "1",
        ]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["stopped"] == "verification_required"


def test_xhs_shortlink_is_expanded_before_native_detail(monkeypatch):
    import httpx

    from video_curator.platforms.xiaohongshu import detail as detail_module

    note_id = "674051740000000007027a15"
    resolved = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=example"
    original_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(302, headers={"Location": resolved})
            if request.url.host == "xhslink.com"
            else httpx.Response(200)
        )
    )
    monkeypatch.setattr(
        metadata.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    received = []

    def detail(candidate, runtime):
        received.append(candidate.url)
        return {
            "id": note_id,
            "webpage_url": candidate.url,
            "title": "source",
            "_backend": "xiaohongshu-browser",
        }

    monkeypatch.setattr(detail_module, "detail", detail)
    result = metadata.fetch(
        Candidate(platform="xiaohongshu", url="https://xhslink.com/a/example"), Runtime()
    )
    assert received == [resolved]
    assert result.access_url == resolved
