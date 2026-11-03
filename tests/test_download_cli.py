import pytest

from video_curator import download, jobs
from video_curator.cli import main
from video_curator.config import Runtime, Topic


def test_download_overrides_are_optional_and_not_persisted(tmp_path, monkeypatch):
    root = tmp_path / "task"
    jobs.initialize(root, Topic(id="test"), Runtime())
    captured = []

    def capture_download(job, limit):
        captured.append((job.topic.download, limit))
        return {"processed": 0}

    monkeypatch.setattr(download, "run", capture_download)
    assert (
        main(
            [
                "download",
                "--input",
                str(root),
                "--limit",
                "40",
                "--workers",
                "2",
                "--resolution",
                "720",
                "--max-duration-seconds",
                "120",
            ]
        )
        == 0
    )
    runtime, limit = captured[-1]
    assert limit == 40 and runtime.selection == "screened"
    assert runtime.max_duration_seconds == 120
    assert runtime.resolution == 720
    assert runtime.workers == 2
    assert main(["download", "--input", str(root), "--limit", "40"]) == 0
    runtime, _ = captured[-1]
    assert runtime.max_duration_seconds is None
    assert runtime.resolution is None
    assert runtime.workers == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_max_duration_does_not_start_download(tmp_path, monkeypatch, value):
    root = tmp_path / "task"
    jobs.initialize(root, Topic(id="test"), Runtime())
    monkeypatch.setattr(download, "run", lambda *a: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as error:
        main(["download", "--input", str(root), "--max-duration-seconds", value])
    assert error.value.code == 2
