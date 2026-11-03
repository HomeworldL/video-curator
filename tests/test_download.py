import json

import pytest

from video_collect import download, export, media, metadata, search
from video_collect.jobs import Job
from video_collect.models import CollectionError


def test_download_without_model_prompt_key_or_frame_extraction(collection, monkeypatch):
    job, details, calls = collection
    job.topic.screen.prompt = None
    monkeypatch.delenv("ARK_API_KEY")
    monkeypatch.delenv("ARK_MODEL", raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("Collection must not prepare visual evidence or call the VLM")

    monkeypatch.setattr("video_collect.preview.obtain", unexpected)
    monkeypatch.setattr("video_collect.providers.doubao.complete_screen", unexpected)
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        report = download.run(job, 10)
    assert report["outcomes"] == {"downloaded": 1}
    assert report["downloaded_records"] == 1
    assert calls["download"] == 1
    record = json.loads((job.root / "videos.jsonl").read_text())
    assert record["screening"]["status"] == "not_run"
    assert (record["screening"]["data"] or {}).get("decision") is None
    assert record["download"]["data"]["path"].startswith("raw/youtube/")
    assert (
        media.sha256(job.root / record["download"]["data"]["path"])
        == record["download"]["data"]["sha256"]
    )
    assert not (job.root / "evidence").exists()
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        assert download.run(job, 10)["processed"] == 0
    assert calls["download"] == 1


def test_download_crash_after_archive_resumes_without_duplicate_download(collection, monkeypatch):
    job, details, calls = collection
    search.import_urls(job, [details.access_url])
    archive = media.archive

    def archive_then_interrupt(*args):
        archive(*args)
        raise CollectionError("simulated_interruption", "media")

    monkeypatch.setattr("video_collect.media.archive", archive_then_interrupt)
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        assert download.run(job, 10)["outcomes"] == {"technical_error": 1}
    monkeypatch.setattr("video_collect.media.archive", archive)
    restarted = Job(job.topic, job.runtime)
    with restarted.exclusive():
        metadata.run(restarted, 10**9, missing_only=True)
        assert download.run(restarted, 10)["downloaded_records"] == 1
    assert calls["download"] == 1 and calls["screen"] == 0


def test_inventory_export_rejects_changed_media(collection):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 10)
    inventory = json.loads((job.root / "videos.jsonl").read_text())
    (job.root / inventory["download"]["data"]["path"]).write_bytes(b"changed")
    with pytest.raises(CollectionError, match="downloaded_artifact_inconsistent"):
        export.validate_downloaded(
            job.state.downloaded_records(),
            job.root,
        )


def test_cli_download_works_without_prompt_or_api_key(collection, monkeypatch, capsys):
    import yaml

    from video_collect.cli import main

    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10, missing_only=True)
    topic_file = job.root.parent / "topic.yaml"
    topic_data = job.topic.model_dump(mode="json")
    topic_data["screen"].pop("prompt")
    topic_file.write_text(yaml.safe_dump(topic_data))
    runtime_file = job.root.parent / "runtime.yaml"
    runtime_data = job.runtime.model_dump(mode="json")
    runtime_file.write_text(yaml.safe_dump(runtime_data))
    monkeypatch.delenv("ARK_API_KEY")
    assert (
        main(
            [
                "--topic",
                str(topic_file),
                "--runtime",
                str(runtime_file),
                "download",
                "--input",
                str(job.root),
                "--selection",
                "non-rejected",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["downloaded_records"] == 1


def test_download_skips_missing_details_before_limit_without_fetching(collection, monkeypatch):
    job, details, calls = collection
    search.import_urls(job, ["https://youtu.be/aaaaaaaaaaa", details.access_url])
    job.state.merge_metadata(details.identity.key, details)

    def forbidden(*args, **kwargs):
        pytest.fail("download must not fetch metadata implicitly")

    monkeypatch.setattr("video_collect.metadata.fetch", forbidden)
    with job.exclusive():
        result = download.run(job, 1)
    assert result["outcomes"] == {"downloaded": 1}
    assert result["missing_metadata"] == 1
    assert calls == {"metadata": 0, "download": 1, "screen": 0}
    with job.exclusive():
        assert download.run(job, 1)["processed"] == 0
    assert calls["download"] == 1


def test_download_rejects_non_video_without_downloading(collection):
    job, details, calls = collection
    search.import_urls(job, [details.access_url])
    job.state.merge_metadata(details.identity.key, details.model_copy(update={"is_live": True}))
    with job.exclusive():
        result = download.run(job, 1)
    assert result["outcomes"] == {"rejected": 1}
    assert calls == {"metadata": 0, "download": 0, "screen": 0}


def test_budget_pause_does_not_become_content_rejection(collection):
    job, details, calls = collection
    search.import_urls(job, [details.access_url])
    job.topic.download.cache_budget_bytes = 1
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        result = download.run(job, 10)
    assert result["outcomes"] == {"budget_paused": 1}
    assert calls["download"] == calls["screen"] == 0


def test_exclusive_topic_lock_prevents_two_writers(collection):
    job, _, _ = collection
    other = Job(job.topic, job.runtime)
    with job.exclusive(), pytest.raises(CollectionError, match="topic_run_already_active"):
        with other.exclusive():
            pass


def test_preexisting_cache_cannot_bypass_duration_check(collection):
    job, details, calls = collection
    details.duration_seconds = 30
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        first = download.run(job, 1)
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        again = download.run(job, 1)
    assert first["outcomes"] == again["outcomes"] == {"technical_error": 1}
    assert calls["download"] == 1 and calls["screen"] == 0
