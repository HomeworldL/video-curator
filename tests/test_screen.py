import json
import shutil

import pytest
from PIL import Image

from video_curator import download, export, filtering, jobs, preview, screening
from video_curator.cli import main
from video_curator.config import Runtime, Topic
from video_curator.identity import identify
from video_curator.models import Candidate, CollectionError, DownloadResult, VideoMetadata
from video_curator.providers import doubao
from video_curator.state import State


@pytest.fixture
def setup_screen(tmp_path, monkeypatch, tiny_video):
    root = tmp_path / "task"
    topic = Topic(
        id="test",
        screen={
            "prompt": "Classify by visible subject",
            "preview": {"tile_size": 160},
            "model": "test",
        },
    )
    runtime = Runtime()
    for settings in runtime.platforms.values():
        settings.request_interval_seconds = 0
    jobs.initialize(root, topic, runtime)
    state = State(root / ".state" / "state.sqlite")
    calls = {"download": 0, "vlm": 0}
    monkeypatch.setenv("ARK_API_KEY", "not-sent")
    monkeypatch.delenv("ARK_MODEL", raising=False)
    monkeypatch.setattr(preview, "_cover", lambda *a: (None, "cover_unavailable"))

    def download(request, settings):
        calls["download"] += 1
        assert settings.platforms["youtube"].format == "bestvideo/best"
        request.directory.mkdir(parents=True, exist_ok=True)
        path = request.directory / "source.mp4"
        shutil.copyfile(tiny_video, path)
        return DownloadResult(path=path, backend="fixture")

    def judge(system, context, evidence, settings, references):
        calls["vlm"] += 1
        assert all(p.path.is_file() for p in evidence)
        assert all(len(p["frames"]) == 8 for p in json.loads(context)["pages"])
        return {
            "result": {
                "pages": [
                    {
                        "page": p.page,
                        "grade": "priority",
                        "reason": "visible motion",
                        "evidence_frames": [1, 8],
                    }
                    for p in evidence
                ]
            },
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
        }

    monkeypatch.setattr("video_curator.download.obtain", download)
    monkeypatch.setattr("video_curator.providers.doubao.complete_screen", judge)

    def add(n=0, decision="keep"):
        url = f"https://www.youtube.com/watch?v={n:011d}"
        identity = identify(url)
        key, _ = state.add(Candidate(platform="youtube", url=url), identity)
        # Stable fixture IDs make scheduling deterministic.
        with state.connect() as db:
            db.execute("UPDATE resources SET record_id=? WHERE key=?", (f"{n:032d}", key))
        state.merge_metadata(
            key,
            VideoMetadata(
                identity=identity, access_url=url, duration_seconds=2, title=f"video {n}"
            ),
        )
        row = state.get(key)
        state.save_text_filters(
            {
                key: {
                    "status": "completed",
                    "decision": decision,
                    "error": None,
                    "input_hash": f"filter{n}",
                    "source_hash": filtering.digest(filtering.source(row)),
                    "result": {
                        "record_id": row["record_id"],
                        "decision": decision,
                        "reason": "test",
                    },
                }
            }
        )
        with jobs.session(root, state):
            export.write_stage(state, root, "filter", {"status": "complete"})
        return key

    return root, topic, runtime, state, calls, add


def test_screen_contact_cache_cleanup_and_portable_restore(setup_screen, tmp_path):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    add(1, "reject")
    result = screening.run(root, topic, runtime)
    assert result["processed"] == result["calls"] == 1
    value = state.screens()[key]
    image = Image.open(value["pages"][0]["evidence"]["path"])
    assert image.size == (480, 570)
    assert not value["pages"][0]["evidence"]["cover_present"]
    assert image.getpixel((50, 50)) == (22, 22, 22)
    assert [f["number"] for f in value["pages"][0]["evidence"]["frames"]] == list(range(1, 9))
    assert len(set(f["timestamp_seconds"] for f in value["pages"][0]["evidence"]["frames"])) == 8
    assert not list((root / ".state" / "cache").rglob("*.mp4"))
    assert state.artifact(key) is None
    records, _ = export.snapshot(state, root)
    record = next(r for r in records if r["identity"]["content_id"] == "00000000000")
    assert record["download"]["status"] == "not_run"
    assert record["screening"]["data"]["grade"] == "priority"
    assert record["screening"]["data"]["pages"][0]["evidence"]["path"].startswith("evidence/")
    assert screening.run(root, topic, runtime)["cached"] == 1
    assert calls == {"download": 1, "vlm": 1}
    moved = tmp_path / "moved"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns(".state"))
    assert screening.run(moved, topic, runtime)["cached"] == 1
    assert calls == {"download": 1, "vlm": 1}


def test_failed_response_explicit_retry_reuses_preview(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    monkeypatch.setattr(
        "video_curator.providers.doubao.complete_screen",
        lambda *a: {"result": {"grade": "nonsense"}},
    )
    assert screening.run(root, topic, runtime)["outcomes"] == {"error": 1}
    assert state.screens()[key]["error"] == "invalid_screen_response"
    assert screening.run(root, topic, runtime)["error_records_skipped"] == 1
    assert calls["download"] == 1
    monkeypatch.setattr(
        "video_curator.providers.doubao.complete_screen",
        lambda *a: {
            "result": {
                "pages": [
                    {"page": 1, "grade": "partial", "reason": "mixed", "evidence_frames": [2]}
                ]
            }
        },
    )
    assert screening.run(root, topic, runtime, retry_failed=True)["outcomes"] == {"partial": 1}
    assert calls["download"] == 1


def test_cover_is_first_cell_and_not_a_video_frame(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    monkeypatch.setattr(preview, "_cover", lambda *a: (Image.new("RGB", (50, 100), "red"), None))
    screening.run(root, topic, runtime)
    evidence = state.screens()[key]["pages"][0]["evidence"]
    assert evidence["cover_present"]
    image = Image.open(evidence["path"])
    assert image.getpixel((80, 80))[0] > 240
    assert len(evidence["frames"]) == 8


def test_prompt_change_requires_version_but_reuses_frames(setup_screen):
    root, topic, runtime, state, calls, add = setup_screen
    add()
    screening.run(root, topic, runtime)
    topic.screen.prompt = "A different purpose"
    with pytest.raises(CollectionError, match="screen_config_changed"):
        screening.run(root, topic, runtime)
    screening.run(root, topic, runtime, new_version=True)
    assert calls == {"download": 1, "vlm": 2}


def test_screen_cli_uses_its_own_session(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    add()
    monkeypatch.setattr(
        "video_curator.cli.Job", lambda *a, **kw: pytest.fail("Generic Job constructed for screen")
    )
    assert main(["screen", "--input", str(root)]) == 0
    assert calls["vlm"] == 1


def test_download_gate_before_limit_and_stale_metadata(setup_screen):
    root, topic, runtime, state, calls, add = setup_screen
    keys = [add(n) for n in range(3)]
    screening.run(root, topic, runtime)
    value = state.screens()[keys[0]]
    value["result"]["grade"] = "low"
    state.save_screen(keys[0], value)
    assert [
        r["key"] for r in state.rows(keep_only=True, screened_grades=["priority"], limit=1)
    ] == [keys[1]]
    with state.connect() as db:
        row = state.get(keys[1])
        data = json.loads(row["metadata_json"])
        data["duration_seconds"] = 3
        db.execute("UPDATE resources SET metadata_json=? WHERE key=?", (json.dumps(data), keys[1]))
    assert [
        r["key"] for r in state.rows(keep_only=True, screened_grades=["priority"], limit=1)
    ] == [keys[2]]


def test_rate_limit_stops_new_requests(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    for n in range(3):
        add(n)

    def fail(*a):
        raise CollectionError("rate_limited", "screen", diagnostics={"http_status": 429})

    monkeypatch.setattr("video_curator.providers.doubao.complete_screen", fail)
    result = screening.run(root, topic, runtime)
    assert result["calls"] == result["processed"] == 1
    assert result["stopped"] == "rate_limited"
    assert calls["download"] == 1


def test_frame_failure_cannot_become_low_grade(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()

    def fail(*a, **kw):
        raise CollectionError("preview_frame_extraction_failed", "preview")

    monkeypatch.setattr(preview, "obtain", fail)
    result = screening.run(root, topic, runtime)
    assert result["calls"] == 0
    assert state.screens()[key]["result"]["grade"] is None
    records, _ = export.snapshot(state, root)
    assert records[0]["screening"]["status"] == "failed"
    assert records[0]["screening"]["data"]["grade"] is None


def test_default_download_downloads_only_screen_priority(setup_screen, monkeypatch):
    from video_curator.jobs import Job

    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    add(1)
    job = Job(topic, runtime, root=root)
    with job.exclusive():
        assert download.run(job, 2)["processed"] == 0
    screening.run(root, topic, runtime)
    value = state.screens()[key]
    value["result"]["grade"] = "low"
    state.save_screen(key, value)
    # Formal downloads use their own format and cannot reuse the discarded preview.
    original_obtain = preview.download.obtain

    def formal(request, cfg):
        adjusted = cfg.model_copy(deep=True)
        adjusted.platforms["youtube"].format = "bestvideo/best"
        return original_obtain(request, adjusted)

    monkeypatch.setattr(preview.download, "obtain", formal)
    with job.exclusive():
        assert download.run(job, 1)["outcomes"] == {"downloaded": 1}
    assert calls["download"] == 3  # two previews and one formal video
    import csv

    csv_path = root / "exports" / "screen-results.csv"
    export.selected(state, root, csv_path, "csv", "all")
    with csv_path.open(encoding="utf-8-sig") as file:
        csv_rows = list(csv.DictReader(file))
    assert {r["screen_grade"] for r in csv_rows} == {"priority", "low"}
    assert {r["filter_decision"] for r in csv_rows} == {"keep"}
    records, _ = export.snapshot(state, root)
    assert sum(r["download"]["status"] == "complete" for r in records) == 1
    assert all(r["screening"]["status"] == "complete" for r in records)
    assert (
        export.selected(state, root, root / "exports" / "accepted.jsonl", "jsonl", "accepted")[
            "exported_records"
        ]
        == 1
    )


def test_cached_sheet_retry_works_when_media_cache_budget_full(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    add()
    screening.run(root, topic, runtime)
    topic.screen.prompt += " changed"
    topic.screen.preview.cache_budget_bytes = 1
    # Rejudging persistent evidence needs no temporary media reservation.
    assert screening.run(root, topic, runtime, new_version=True)["calls"] == 1
    assert calls["download"] == 1


def test_missing_model_does_not_download_or_change_dataset(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    add()
    before = (root / "videos.jsonl").read_bytes()
    topic.screen.model = ""
    with pytest.raises(CollectionError, match="configure_vlm"):
        screening.run(root, topic, runtime)
    assert calls == {"download": 0, "vlm": 0}
    assert (root / "videos.jsonl").read_bytes() == before


def test_unknown_visual_request_survives_handoff_without_resend(
    setup_screen, monkeypatch, tmp_path
):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()

    def unknown(*args):
        calls["vlm"] += 1
        raise CollectionError("outcome_unknown", "vlm")

    monkeypatch.setattr("video_curator.providers.doubao.complete_screen", unknown)
    screening.run(root, topic, runtime)
    assert state.screens()[key]["error"] == "outcome_unknown"
    moved = tmp_path / "handoff"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns(".state"))
    screening.run(moved, topic, runtime)
    assert calls == {"download": 1, "vlm": 1}
    restored = State(moved / ".state" / "state.sqlite")
    assert restored.screens()[key]["error"] == "outcome_unknown"


@pytest.mark.parametrize("frame_count", [1, 4, 12])
def test_configurable_frames_layout_and_model_protocol(setup_screen, monkeypatch, frame_count):
    import math

    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    topic.screen.preview.frame_count = frame_count

    def judge(system, context, evidence, settings, references):
        evidence = evidence[0]
        assert len(evidence.frames) == frame_count
        assert [f.number for f in evidence.frames] == list(range(1, frame_count + 1))
        assert f"其余{frame_count}格" in system
        columns = math.ceil(math.sqrt(frame_count + 1))
        rows = math.ceil((frame_count + 1) / columns)
        with Image.open(evidence.path) as sheet:
            assert sheet.size == (160 * columns, 190 * rows)
        return {
            "result": {
                "pages": [
                    {
                        "page": 1,
                        "grade": "priority",
                        "reason": "visible",
                        "evidence_frames": [frame_count],
                    }
                ]
            }
        }

    monkeypatch.setattr("video_curator.providers.doubao.complete_screen", judge)
    assert screening.run(root, topic, runtime)["calls"] == 1
    assert state.screens()[key]["status"] == "complete"
    assert screening.run(root, topic, runtime)["cached"] == 1


def test_screen_usage_survives_cache_retry_and_portable_restore(
    setup_screen, monkeypatch, tmp_path
):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    measured = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}

    def invalid(*args):
        raise CollectionError("invalid_model_json", "screen", diagnostics={"usage": measured})

    monkeypatch.setattr(doubao, "complete_screen", invalid)
    first = screening.run(root, topic, runtime)
    assert first["usage"] == measured
    assert first["usage_reported_calls"] == 1
    assert state.screens()[key]["usage_total"] == measured
    monkeypatch.setattr(
        doubao,
        "complete_screen",
        lambda *a: {
            "result": {
                "pages": [
                    {"page": 1, "grade": "priority", "reason": "visible", "evidence_frames": [1]}
                ]
            },
            "usage": measured,
        },
    )
    retried = screening.run(root, topic, runtime, retry_failed=True)
    assert retried["usage"] == measured
    assert retried["cumulative"]["usage"]["total_tokens"] == 2200
    assert retried["cumulative"]["calls"] == 2
    moved = tmp_path / "portable"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns(".state"))
    cached = screening.run(moved, topic, runtime)
    assert cached["calls"] == 0 and cached["usage"] == {}
    assert cached["cumulative"] == retried["cumulative"]


def test_historical_screen_usage_is_unknown():
    result = screening.usage_totals([{"status": "complete"}])
    assert result == {"usage": {}, "calls": 1, "usage_reported_calls": 0, "usage_missing_calls": 1}


def test_multipage_sampling_and_max_grade(setup_screen, monkeypatch):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    topic.screen.preview.segment_seconds = 1
    received = []

    def judge(system, context, pages, settings, references):
        received.extend(pages)
        assert len(pages) == 2
        return {
            "result": {
                "pages": [
                    {"page": 2, "grade": "priority", "reason": "motion", "evidence_frames": [1, 8]},
                    {"page": 1, "grade": "low", "reason": "static", "evidence_frames": [2]},
                ]
            }
        }

    monkeypatch.setattr(doubao, "complete_screen", judge)
    result = screening.run(root, topic, runtime)
    assert result["calls"] == 1 and result["outcomes"] == {"priority": 1}
    assert [(p.start_seconds, p.end_seconds) for p in received] == [(0, 1), (1, 2)]
    for p in received:
        assert all(p.start_seconds <= f.timestamp_seconds < p.end_seconds for f in p.frames)
    value = state.screens()[key]
    assert [p["grade"] for p in value["pages"]] == ["low", "priority"]
    assert value["result"]["grade"] == "priority"
    assert not list((root / ".state/cache").rglob("*.mp4"))
    assert preview.intervals(601, 300) == [(0, 300), (300, 600), (600, 601)]
    assert preview.intervals(600, 300) == [(0, 300), (300, 600)]


def test_page_retry_preserves_successful_pages_and_usage(setup_screen, monkeypatch, tmp_path):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    topic.screen.preview.segment_seconds = 1
    topic.screen.pages_per_request = 1
    seen = []
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}

    def judge(system, context, pages, settings, references):
        number = pages[0].page
        seen.append(number)
        if len(seen) == 2:
            raise CollectionError("invalid_model_json", "screen", diagnostics={"usage": usage})
        return {
            "result": {
                "pages": [
                    {
                        "page": number,
                        "grade": "priority" if number == 2 else "low",
                        "reason": "visible",
                        "evidence_frames": [1],
                    }
                ]
            },
            "usage": usage,
        }

    monkeypatch.setattr(doubao, "complete_screen", judge)
    first = screening.run(root, topic, runtime)
    assert first["calls"] == 2 and first["usage"]["total_tokens"] == 220
    assert [p["status"] for p in state.screens()[key]["pages"]] == ["complete", "failed"]
    with monkeypatch.context() as patch:

        def fail_preview(*args, **kwargs):
            raise CollectionError("preview_frame_extraction_failed", "preview")

        patch.setattr(preview, "obtain", fail_preview)
        failed = screening.run(root, topic, runtime, retry_failed=True)
        assert failed["calls"] == 0
        assert state.screens()[key]["pages"][0]["status"] == "complete"
        assert state.screens()[key]["usage_total"]["total_tokens"] == 220
    moved = tmp_path / "multipage-portable"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns(".state"))
    retried = screening.run(moved, topic, runtime, retry_failed=True)
    assert seen == [1, 2, 2]
    assert retried["calls"] == 1 and retried["cumulative"]["usage"]["total_tokens"] == 330
    assert retried["outcomes"] == {"priority": 1}
    assert calls["download"] == 1


def test_max_calls_is_request_budget_and_resumes_pages(setup_screen):
    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    topic.screen.preview.segment_seconds = 1
    topic.screen.pages_per_request = 1
    topic.screen.max_calls = 1
    first = screening.run(root, topic, runtime)
    assert first["calls"] == 1 and first["stopped"] == "max_calls_reached"
    assert state.screens()[key]["status"] == "pending"
    second = screening.run(root, topic, runtime)
    assert second["calls"] == 1 and second["status"] == "complete"
    assert second["cumulative"]["calls"] == 2
    assert calls == {"download": 1, "vlm": 2}


@pytest.mark.parametrize("numbers", [[1, 1], [1], [1, 3]])
def test_page_mapping_rejects_duplicate_missing_unknown(numbers):
    from types import SimpleNamespace

    pages = [SimpleNamespace(page=n, frames=[1, 2]) for n in [1, 2]]
    response = {
        "pages": [
            {"page": n, "grade": "low", "reason": "test", "evidence_frames": []} for n in numbers
        ]
    }
    with pytest.raises(CollectionError, match="invalid_page_mapping"):
        screening.validate_pages(response, pages)


def test_legacy_migration_reuses_results_until_explicit_resampling(setup_screen, load_script):

    root, topic, runtime, state, calls, add = setup_screen
    key = add()
    screening.run(root, topic, runtime)
    value = state.screens()[key]
    page = value.pop("pages")[0]
    value["evidence"] = {
        k: v
        for k, v in page["evidence"].items()
        if k not in {"page", "start_seconds", "end_seconds", "sampling"}
    }
    value["result"] = {k: page[k] for k in ("grade", "reason", "evidence_frames")}
    topic.screen.preview.segment_seconds = 1
    config = screening.screen_descriptor(topic)
    migration = load_script("migrate_screen_pages")
    converted = migration.convert(value, config["hash"])
    assert converted["config_hash"] != config["hash"]
    assert converted["pages"][0]["reason"] == page["reason"]
    state.save_screen(key, converted)
    state.setting("screen_config", json.dumps(config))
    cached = screening.run(root, topic, runtime)
    assert cached["cached"] == 1 and cached["calls"] == 0
    assert state.screens()[key]["pages"][0]["evidence"]["sampling"] == "legacy_whole_video"
    updated = screening.run(root, topic, runtime, resample_legacy=True)
    assert updated["calls"] == 1
    assert len(state.screens()[key]["pages"]) == 2
    assert all(
        p["evidence"]["sampling"] == "fixed_intervals" for p in state.screens()[key]["pages"]
    )
