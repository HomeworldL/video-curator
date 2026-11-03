import importlib.util
import json
from pathlib import Path

from video_collect import export, search

spec = importlib.util.spec_from_file_location(
    "dataset_viewer", Path(__file__).resolve().parents[1] / "scripts/build_dataset_viewer.py"
)
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


def test_report_handles_not_run_and_untrusted_titles_without_mutating_dataset(collection, tmp_path):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    details = details.model_copy(update={"title": '</script><script>alert("x")</script>'})
    job.state.merge_metadata(details.identity.key, details)
    export.write_stage(job.state, job.root, "metadata", {"status": "complete"})
    source = job.root / "videos.jsonl"
    before = source.read_bytes()
    output = tmp_path / "report/index.html"
    result = viewer.build(job.root, output)
    assert result["records"] == 1 and result["screen"] == {"not_run": 1}
    html = output.read_text()
    assert details.title not in html
    payload = html.split('<script type="application/json" id="dataset">')[1].split("</script>")[0]
    record = json.loads(payload)["records"][0]
    assert record["title"] == details.title
    assert record["preview"] is None
    assert source.read_bytes() == before
    assert viewer.web_url("javascript:alert(1)") is None
    assert viewer.local_url("../report/index.html", job.root, output) is None
    image = job.root / "evidence" / "a b.jpg"
    image.parent.mkdir()
    image.write_bytes(b"fixture")
    assert viewer.local_url(str(image), job.root, output).endswith("a%20b.jpg")
