"""Task context, configuration, locking and shared resources; no stage business logic."""

import fcntl
import json
import shutil
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from . import export
from .config import Runtime, Topic
from .models import CollectionError
from .state import State, now

SCHEMA = "video_curator.task.v2"


@lru_cache(maxsize=1)
def _read_cached(path: Path, signature: tuple) -> dict:
    return json.loads(path.read_text())


def read(root: Path) -> dict:
    """Cache one parsed snapshot; atomic replacements invalidate it, callers own copies."""
    try:
        path = (root / "task.json").resolve()
        stat = path.stat()
        task = _read_cached(path, (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        if task["schema"] != SCHEMA:
            raise ValueError
        return deepcopy(task)
    except (OSError, ValueError, KeyError):
        raise CollectionError("invalid_task_directory", "job") from None


@contextmanager
def locked(root: Path):
    (root / ".state").mkdir(parents=True, exist_ok=True)
    with (root / ".state" / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectionError("topic_run_already_active", "job") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def initialize(root: Path, topic: Topic, runtime: Runtime):
    root = root.resolve()
    with locked(root):
        _initialize(root, topic, runtime)


def _initialize(root: Path, topic: Topic, runtime: Runtime):
    if (root / "task.json").exists():
        saved, _ = load(root)
        if saved.id != topic.id or saved.search != topic.search:
            raise CollectionError("job_topic_changed_use_new_output", "job")
        return
    if root.exists() and any(p.name != ".state" for p in root.iterdir()):
        raise CollectionError("job_output_not_empty", "job")
    root.mkdir(parents=True, exist_ok=True)
    snapshot = topic.model_dump(mode="json")
    references = []
    for index, source in enumerate(topic.screen.references):
        relative = Path("references") / f"{index}{source.suffix}"
        (root / relative).parent.mkdir(exist_ok=True)
        shutil.copyfile(source, root / relative)
        references.append(str(relative))
    snapshot["screen"]["references"] = references
    settings = runtime.model_dump(mode="json")
    settings["dataset_root"] = "."
    for platform in settings["platforms"].values():
        platform["cookie_file"] = None
        platform["browser_state"] = None
    export.write_json(
        root / "task.json",
        {
            "schema": SCHEMA,
            "task_id": uuid4().hex,
            "created_at": now(),
            "topic": snapshot,
            "runtime": settings,
            "search": {"plan": None, "checkpoints": []},
            "stages": {},
            "resume_records": {},
            "metadata_shared": {},
        },
    )
    export._atomic_write([], root / "videos.jsonl")


def load(root: Path, *, task: dict | None = None) -> tuple[Topic, Runtime]:
    root = root.resolve()
    task = task if task is not None else read(root)
    topic = Topic.model_validate(task["topic"])
    topic.screen.references = [(root / p).resolve() for p in topic.screen.references]
    if any(not p.is_relative_to(root) or not p.is_file() for p in topic.screen.references):
        raise CollectionError("task_reference_missing_or_invalid", "job")
    runtime = Runtime.model_validate(task["runtime"])
    runtime.dataset_root = root.parent
    return topic, runtime


def save_search_plan(root: Path, plan: dict):
    task = read(root)
    previous = task["search"]["plan"]
    if previous is not None and previous != plan:
        raise CollectionError("job_search_plan_changed_use_new_output", "job")
    task["search"]["plan"] = plan
    export.write_json(root / "task.json", task)


@contextmanager
def session(root: Path, state=None):
    """Restore execution state under the same lock used by all stages."""
    root = root.resolve()
    with locked(root):
        state = state or State(root / ".state" / "state.sqlite")
        export.restore(state, root)
        state.recover()
        yield state


class Job:
    """Shared task state, locking and platform throttling; stages own their work."""

    def __init__(
        self,
        topic: Topic,
        runtime: Runtime,
        *,
        root=None,
        progress: Callable[[str], None] | None = None,
        task: dict | None = None,
    ):
        self.topic, self.runtime = topic, runtime
        self.progress = progress
        self.root = (root or runtime.dataset_root / topic.id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "task.json").exists():
            initialize(self.root, topic, runtime)
        if task is None:
            task = read(self.root)
        self.topic.screen.references = load(self.root, task=task)[0].screen.references
        self.state = State(self.root / ".state" / "state.sqlite")
        self.platform_locks = {p: threading.Lock() for p in runtime.platforms}
        self.last_request = {p: 0.0 for p in runtime.platforms}

    def report(self, message: str):
        if self.progress:
            self.progress(message)

    @contextmanager
    def exclusive(self):
        with session(self.root, self.state):
            yield self

    @contextmanager
    def platform_slot(self, platform):
        if platform not in self.platform_locks:
            raise CollectionError("platform_not_configured", "config")
        with self.platform_locks[platform]:
            delay = self.runtime.platforms[platform].request_interval_seconds
            remaining = delay - (time.monotonic() - self.last_request[platform])
            if remaining > 0:
                time.sleep(remaining)
            try:
                yield
            finally:
                self.last_request[platform] = time.monotonic()
