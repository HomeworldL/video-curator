import importlib.util
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from video_curator.config import Runtime, Topic
from video_curator.identity import identify
from video_curator.jobs import Job
from video_curator.models import DownloadResult, VideoMetadata

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.fixture(scope="session")
def tiny_video(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for media integration tests")
    path = tmp_path_factory.mktemp("media") / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def collection(tmp_path, monkeypatch, tiny_video):
    prompt = "Judge supplied rules and frames. Output JSON."
    topic = Topic(
        id="test", search={"queries": {"youtube": ["robot performance"]}}, screen={"prompt": prompt}
    )
    topic.download.selection = "non-rejected"
    runtime = Runtime(
        dataset_root=tmp_path / "datasets",
    )
    for settings in runtime.platforms.values():
        settings.request_interval_seconds = 0
    monkeypatch.setenv("ARK_API_KEY", "test-key-never-sent")
    monkeypatch.delenv("ARK_MODEL", raising=False)
    details = VideoMetadata(
        identity=identify(URL), access_url=URL, title="robot performance", duration_seconds=2
    )
    calls = {"metadata": 0, "download": 0, "screen": 0}

    def fetch(candidate, _runtime):
        calls["metadata"] += 1
        return details

    def obtain(request, _runtime):
        calls["download"] += 1
        request.directory.mkdir(parents=True, exist_ok=True)
        path = request.directory / "source.mp4"
        shutil.copyfile(tiny_video, path)
        return DownloadResult(path=path, backend="fixture")

    monkeypatch.setattr("video_curator.metadata.fetch", fetch)
    monkeypatch.setattr("video_curator.download.obtain", obtain)
    job = Job(topic, runtime)
    return job, details, calls


@pytest.fixture(scope="session")
def load_script():
    """Load repository scripts without changing sys.path or repeating import boilerplate."""

    @lru_cache
    def load(name):
        path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load
