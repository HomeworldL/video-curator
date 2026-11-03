import shutil

import pytest

from video_curator import export, jobs, metadata, search
from video_curator.config import Runtime, Topic
from video_curator.jobs import Job
from video_curator.models import Candidate, CollectionError


@pytest.mark.parametrize(
    "platform,url",
    [
        ("youtube", "https://www.youtube.com/watch?v=BaW_jenozKc"),
        ("bilibili", "https://www.bilibili.com/video/BV17x411w7KC?p=1"),
        ("douyin", "https://www.douyin.com/video/7123456789012345678"),
        (
            "xiaohongshu",
            "https://www.xiaohongshu.com/explore/64a123456789abcdef012345?xsec_token=signed&xsec_source=pc_search",
        ),
    ],
)
def test_common_compaction_preserves_download_and_extra_information(platform, url):
    candidate = Candidate(platform=platform, url=url)
    formats = [{"url": "https://video.xhscdn.com/signed?token=abc", "height": 1080}]
    raw = {
        "title": "title",
        "description": "description",
        "uploader": "author",
        "uploader_id": 123,
        "timestamp": 1700000000,
        "duration": 2,
        "tags": ["tag"],
        "thumbnail": "https://example.com/cover.jpg",
        "webpage_url": url,
        "formats": formats,
        "_backend": platform + "-browser",
        "channel": "different channel",
        "channel_id": "channel-id",
        "thumbnails": [{"url": "https://example.com/cover.jpg", "width": 1280}],
        "custom_field": {"value": 42},
    }
    result = metadata.normalize(candidate, raw)
    assert result.access_url == url
    assert not set(result.raw) & {
        "title",
        "description",
        "uploader",
        "uploader_id",
        "timestamp",
        "duration",
        "tags",
        "thumbnail",
        "webpage_url",
    }
    assert result.raw["formats"] == formats
    assert result.raw["_backend"] == raw["_backend"]
    assert result.raw["channel"] == "different channel"
    assert result.raw["thumbnails"][0]["width"] == 1280
    assert result.raw["custom_field"] == {"value": 42}
    assert metadata.compact_common(result) == result
    assert raw["title"] == "title"  # Provider result was not mutated.


def test_invalid_source_values_are_not_silently_discarded():
    result = metadata.normalize(
        Candidate(platform="youtube", url="https://youtu.be/BaW_jenozKc"),
        {"duration": "unknown", "timestamp": "unknown"},
    )
    assert result.duration_seconds is None and result.published_at is None
    assert result.raw == {"duration": "unknown", "timestamp": "unknown"}


def test_parts_are_shared_versioned_and_portable(tmp_path):
    job = Job(Topic(id="parts"), Runtime(dataset_root=tmp_path))
    base = "https://www.bilibili.com/video/BV17x411w7KC"
    pages = [{"page": 1, "cid": 101, "duration": 10}, {"page": 2, "cid": 102, "duration": 20}]
    references = []
    with job.exclusive():
        for part in (1, 2):
            candidate = Candidate(platform="bilibili", url=f"{base}?p={part}")
            details = metadata.normalize(
                candidate, {"pages": pages, "cid": 100 + part, "duration": 10 * part}
            )
            search.import_urls(job, [candidate.url])
            job.state.merge_metadata(details.identity.key, details)
        exported = export.write_stage(job.state, job.root, "metadata", {})
        assert exported["metadata_records"] == 2
    records, resume = export.snapshot(job.state, job.root)
    assert len(resume["metadata_shared"]) == 1
    for record in records:
        raw = record["metadata"]["data"]["raw"]
        assert "pages" not in raw and "cid" not in raw
        assert raw["part"]["page"] == int(record["identity"]["part_id"])
        assert raw["part_count"] == 2
        references.append(raw["parts_ref"])
    assert len(set(references)) == 1
    changed = details.model_copy(update={"raw": {"pages": [*pages, {"page": 3, "cid": 103}]}})
    _, shared = metadata.compact_storage(changed)
    assert next(iter(shared)) != references[0]
    assert pages == [
        {"page": 1, "cid": 101, "duration": 10},
        {"page": 2, "cid": 102, "duration": 20},
    ]
    moved = tmp_path / "handoff"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    restored = Job(*jobs.load(moved), root=moved)
    with restored.exclusive():
        restored_records, restored_resume = export.snapshot(restored.state, moved)
        assert restored_records == records
        assert restored_resume["metadata_shared"] == resume["metadata_shared"]
    shutil.rmtree(moved / ".state")
    task = jobs.read(moved)
    task["metadata_shared"][references[0]]["pages"][0]["cid"] = 999
    export.write_json(moved / "task.json", task)
    broken = Job(*jobs.load(moved), root=moved)
    with pytest.raises(CollectionError, match="shared_parts_missing_or_invalid"):
        with broken.exclusive():
            pytest.fail("A corrupted shared list must never be restored")
    assert broken.state.rows() == []
