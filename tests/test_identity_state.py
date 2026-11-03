import json

import pytest

from video_collect import search
from video_collect.identity import identify
from video_collect.metadata import normalize
from video_collect.models import Candidate, CollectionError
from video_collect.platforms.bilibili.urls import access_url
from video_collect.state import State


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/BaW_jenozKc?t=1",
        "https://www.youtube.com/shorts/BaW_jenozKc",
        "https://www.youtube.com/watch?v=BaW_jenozKc&list=abc",
        "https://www.youtube.com/embed/BaW_jenozKc",
    ],
)
def test_youtube_variants(url):
    assert identify(url).key == "youtube:BaW_jenozKc:"


def test_platform_boundaries_and_unknown_shortlink():
    with pytest.raises(CollectionError):
        identify("https://youtube.com.evil.test/watch?v=BaW_jenozKc")
    with pytest.raises(CollectionError):
        identify("https://youtube.com@evil.test/watch?v=BaW_jenozKc")
    assert identify("https://b23.tv/abc123").provisional
    assert identify("https://v.douyin.com/abc123").provisional


def test_bili_parts_are_distinct_and_av_maps_from_metadata():
    av = "https://www.bilibili.com/video/av170001?p=2"
    bvid = "BV17x411w7KC"
    assert identify(av, {"id": bvid + "_p2"}).key == f"bilibili:{bvid}:2"
    assert identify(f"https://www.bilibili.com/video/{bvid}").part_id == "1"
    assert (
        identify(av, {"id": bvid + "_p2"}).key
        != identify(f"https://www.bilibili.com/video/{bvid}").key
    )


def test_xhs_signed_access_preserved():
    url = "https://www.xiaohongshu.com/explore/64c8b9e0000000000100abcd?xsec_token=secret&xsec_source=pc"
    plain = url.split("?")[0]
    assert identify(url).key == identify(plain).key
    details = normalize(
        Candidate(platform="xiaohongshu", url=url),
        {"id": "64c8b9e0000000000100abcd", "title": "video", "webpage_url": plain},
    )
    assert details.access_url == url
    assert "xsec_token" not in details.identity.canonical_url


def test_bili_localized_path_preserves_part_and_access_query():
    localized = "https://www.bilibili.com/zh-Hans/video/BV17x411w7KC/?p=2&token=example"
    assert access_url(localized) == "https://www.bilibili.com/video/BV17x411w7KC/?p=2&token=example"


def test_web_search_title_is_not_publisher_evidence():
    candidate = Candidate(
        platform="youtube",
        url="https://youtu.be/BaW_jenozKc",
        title="robot imitation dance",
        mode="web",
    )
    assert normalize(candidate, {"id": "BaW_jenozKc"}).title is None


def test_discoveries_preserved_when_av_and_bv_merge(tmp_path):
    state = State(tmp_path / "state.sqlite")
    av = "https://www.bilibili.com/video/av170001"
    bv = "https://www.bilibili.com/video/BV17x411w7KC"
    a = Candidate(platform="bilibili", url=av, query="first", mode="native")
    b = Candidate(platform="bilibili", url=bv, query="second", mode="web")
    old_key, _ = state.add(a, identify(av))
    canonical, _ = state.add(b, identify(bv))
    detail = normalize(a, {"id": "BV17x411w7KC", "title": "video", "webpage_url": bv})
    assert state.merge_metadata(old_key, detail) == canonical
    assert len(state.rows()) == 1
    with state.connect() as db:
        assert db.execute("SELECT count(*) FROM discoveries").fetchone()[0] == 2
        assert len({r[0] for r in db.execute("SELECT resource_key FROM aliases")}) == 1
    key, inserted = state.add(a, identify(av))
    assert key == canonical and not inserted


def test_recover_only_uncertain_inflight_semantics(collection):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    key = details.identity.key
    job.state.save_screen(key, {"status": "in_flight", "error": None})
    job.state.update(key, "processing")
    job.state.recover()
    assert job.state.get(key)["status"] == "interrupted"
    assert job.state.screens()[key]["status"] == "failed"
    assert job.state.screens()[key]["error"] == "outcome_unknown"


def test_utc_units_and_missing_fields():
    detail = normalize(
        Candidate(platform="youtube", url="https://youtu.be/BaW_jenozKc"),
        {"id": "BaW_jenozKc", "timestamp": 0, "duration": "12.5"},
    )
    assert detail.duration_seconds == 12.5
    assert detail.published_at == "1970-01-01T00:00:00+00:00"
    assert detail.description is None
    assert json.loads(detail.model_dump_json())["title"] is None


def test_stage_selection_orders_by_record_id_and_keeps_retry_limits(tmp_path):
    state = State(tmp_path / "state.sqlite")
    for suffix, record_id, attempts in [("a", "003", 0), ("b", "001", 2), ("c", "002", 3)]:
        url = f"https://youtu.be/{suffix * 11}"
        key, _ = state.add(Candidate(platform="youtube", url=url), identify(url))
        with state.connect() as db:
            db.execute(
                "UPDATE resources SET record_id=?, attempts=? WHERE key=?",
                (record_id, attempts, key),
            )
    assert [r["record_id"] for r in state.rows()] == ["001", "002", "003"]
    assert [r["record_id"] for r in state.rows(max_attempts=3)] == ["001", "003"]
    assert [r["record_id"] for r in state.rows(max_attempts=3, limit=1)] == ["001"]
    assert [r["record_id"] for r in state.rows(missing_metadata=True, limit=2)] == ["001", "002"]


def test_stage_retry_limits_are_independent_and_applied_before_limit(tmp_path):
    from video_collect.models import VideoMetadata

    state = State(tmp_path / "state.sqlite")
    keys = []
    for number, url in enumerate(["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"]):
        identity = identify(url)
        key, _ = state.add(Candidate(platform="youtube", url=url), identity)
        keys.append(key)
        for _ in range(2):
            state.update(key, "technical_error", "timeout", attempt=True, stage="metadata")
        state.merge_metadata(key, VideoMetadata(identity=identity, access_url=url))
        state.update(key, "metadata_ready", stage="metadata")
        with state.connect() as db:
            db.execute("UPDATE resources SET record_id=? WHERE key=?", (str(number), key))
    state.update(keys[0], "technical_error", "timeout", attempt=True, stage="download")
    rows = state.rows(max_attempts=1, attempt_stage="download", limit=1, require_metadata=True)
    assert [r["key"] for r in rows] == [keys[1]]
    assert state.rows(max_attempts=2, attempt_stage="metadata") == []


def test_legacy_database_migrates_without_charging_metadata_failures_to_download(tmp_path):
    from video_collect.models import VideoMetadata

    path = tmp_path / "state.sqlite"
    state = State(path)
    url = "https://youtu.be/aaaaaaaaaaa"
    key, _ = state.add(Candidate(platform="youtube", url=url), identify(url))
    with state.connect() as db:
        db.execute("ALTER TABLE resources DROP COLUMN stage_attempts_json")
        db.execute("UPDATE resources SET attempts=2 WHERE key=?", (key,))
    state = State(path)
    assert state.rows(max_attempts=2, attempt_stage="metadata") == []
    state.merge_metadata(key, VideoMetadata(identity=identify(url), access_url=url))
    state.update(key, "metadata_ready", stage="metadata")
    assert len(state.rows(max_attempts=1, attempt_stage="download")) == 1
    assert json.loads(state.get(key)["stage_attempts_json"]) == {"metadata": 2}


def test_legacy_download_errors_still_obey_retry_limit(tmp_path):
    from video_collect.models import VideoMetadata

    state = State(tmp_path / "state.sqlite")
    url = "https://youtu.be/aaaaaaaaaaa"
    key, _ = state.add(Candidate(platform="youtube", url=url), identify(url))
    state.merge_metadata(key, VideoMetadata(identity=identify(url), access_url=url))
    with state.connect() as db:
        db.execute(
            "UPDATE resources SET attempts=3,stage_attempts_json=NULL,stage_errors_json=? WHERE key=?",
            (json.dumps({"download": "timeout"}), key),
        )
    assert state.rows(max_attempts=3, attempt_stage="download") == []
