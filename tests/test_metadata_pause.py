import json

import pytest

from video_curator import jobs, metadata, search
from video_curator.cli import main
from video_curator.identity import identify
from video_curator.models import CollectionError


def seed(job):
    search.import_urls(
        job,
        [
            "https://youtu.be/aaaaaaaaaaa",
            "https://youtu.be/bbbbbbbbbbb",
            "https://youtu.be/ccccccccccc",
            "https://youtu.be/ddddddddddd",
        ],
    )


@pytest.mark.parametrize(
    "code", ["rate_limited", "platform_access_blocked", "verification_required"]
)
def test_risk_stops_remaining_metadata_and_resumes(collection, monkeypatch, capsys, code):
    job, details, _ = collection
    seed(job)
    calls = []

    def fetch(candidate, runtime):
        calls.append(candidate.url)
        if len(calls) == 2:
            raise CollectionError(code, "metadata")
        return details.model_copy(
            update={"identity": identify(candidate.url), "access_url": candidate.url}
        )

    monkeypatch.setattr("video_curator.metadata.fetch", fetch)
    assert main(["metadata", "--input", str(job.root)]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["stopped"] == code and summary["status"] == "partial"
    assert summary["metadata_records"] == 1 and summary["pending_records"] == 3
    assert len(calls) == 2
    blocked = job.state.get(identify(calls[-1]).key)
    assert blocked["attempts"] == 0
    first = calls[0]
    assert main(["metadata", "--input", str(job.root)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "complete" and summary["metadata_records"] == 4
    assert calls.count(first) == 1


def test_interrupt_publishes_before_fifty_rows(collection, monkeypatch, capsys):
    job, details, _ = collection
    seed(job)
    calls = []

    def fetch(candidate, runtime):
        calls.append(candidate.url)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return details.model_copy(
            update={"identity": identify(candidate.url), "access_url": candidate.url}
        )

    monkeypatch.setattr("video_curator.metadata.fetch", fetch)
    assert main(["metadata", "--input", str(job.root)]) == 130
    summary = json.loads(capsys.readouterr().out)
    assert summary["stopped"] == "interrupted"
    assert summary["metadata_records"] == 1 and summary["status"] == "partial"
    records = [json.loads(line) for line in (job.root / "videos.jsonl").read_text().splitlines()]
    assert sum(row["metadata"]["status"] == "complete" for row in records) == 1


def test_three_transient_failures_stop_and_no_download_starts(collection, monkeypatch):
    job, _, calls = collection
    seed(job)
    requested = []

    def fetch(candidate, runtime):
        requested.append(candidate.url)
        raise CollectionError("timeout", "metadata")

    monkeypatch.setattr("video_curator.metadata.fetch", fetch)
    with (
        job.exclusive(),
        pytest.raises(CollectionError, match="consecutive_metadata_failures"),
    ):
        metadata.run(job, 4)
    assert len(requested) == 3 and calls["download"] == 0
    assert jobs.read(job.root)["stages"]["metadata"]["stopped"] == "consecutive_metadata_failures"


def test_request_spacing_is_also_passed_to_ytdlp():
    from video_curator.config import Runtime
    from video_curator.providers.ytdlp import options

    runtime = Runtime()
    assert options(runtime.platforms["bilibili"], runtime)["sleep_interval_requests"] == 2
