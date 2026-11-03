"""Load topic and machine settings; credentials stay in environment variables."""

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator

from .models import Model, Platform


class PreviewSettings(Model):
    segment_seconds: int = Field(default=300, ge=1)
    frame_count: int = Field(default=8, ge=1, le=32)
    resolution: int = Field(default=360, ge=144, le=1080)
    tile_size: int = Field(default=384, ge=160, le=768)
    max_bytes: int = Field(default=256 * 1024**2, gt=0)
    delete_temporary_video: bool = True

    cache_budget_bytes: int = Field(default=2 * 1024**3, gt=0)


class ScreenSettings(Model):
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    api_key_env: str = "ARK_API_KEY"
    model_env: str = "ARK_MODEL"
    model: str = ""
    max_tokens: int = Field(default=6000, ge=128, le=16000)
    timeout_seconds: float = Field(default=90, gt=0)
    workers: int = Field(default=1, ge=1, le=8)
    max_calls: int = Field(default=1500, ge=0)
    pages_per_request: int = Field(default=6, ge=1, le=24)

    prompt: str | None = Field(default=None, min_length=1)
    references: list[Path] = Field(default_factory=list)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)
    limit: int | None = Field(default=None, ge=1)


class FilterSettings(Model):
    batch_size: int = Field(default=50, ge=1, le=200)
    workers: int = Field(default=1, ge=1, le=8)
    max_batch_chars: int = Field(default=60000, ge=1000, le=500000)
    max_tokens: int = Field(default=12000, ge=256, le=32000)
    max_calls: int = Field(default=300, ge=1, le=10000)
    timeout_seconds: float = Field(default=180, gt=0)
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    api_key_env: str = "ARK_API_KEY"
    model_env: str = "ARK_FILTER_MODEL"
    model: str = "doubao-seed-2-1-lite-260915"

    prompt: str | None = Field(default=None, min_length=1)
    limit: int | None = Field(default=None, ge=1)


class SearchSettings(Model):
    shared_queries: list[str] = Field(default_factory=list)
    queries: dict[Platform, list[str]] = Field(default_factory=dict)
    seeds: list[str] = Field(default_factory=list)
    page_size: int = Field(default=10, ge=1, le=200)
    mode: Literal["native", "web"] | None = None
    all_pages: bool = False
    max_pages: int | None = Field(default=None, ge=1)

    def queries_for(self, platform: Platform) -> list[str]:
        return list(dict.fromkeys([*self.shared_queries, *self.queries.get(platform, [])]))


class MetadataSettings(Model):
    limit: int | None = Field(default=None, ge=1)
    max_attempts: int = Field(default=3, ge=1)


class DownloadSettings(Model):
    workers: int = Field(default=1, ge=1, le=16)
    selection: Literal["screened", "keep", "non-rejected"] = "screened"
    grades: list[Literal["priority", "partial", "difficult", "low"]] = Field(
        default_factory=lambda: ["priority"]
    )
    resolution: int | None = Field(default=None, ge=1)
    max_duration_seconds: float | None = Field(default=None, gt=0)
    limit: int = Field(default=10, ge=1)
    max_bytes: int = Field(default=256 * 1024**2, gt=0)
    cache_budget_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_attempts: int = Field(default=3, ge=1)


class Topic(Model):
    id: str
    search: SearchSettings = Field(default_factory=SearchSettings)
    metadata: MetadataSettings = Field(default_factory=MetadataSettings)
    filter: FilterSettings = Field(default_factory=FilterSettings)
    screen: ScreenSettings = Field(default_factory=ScreenSettings)
    download: DownloadSettings = Field(default_factory=DownloadSettings)

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
            raise ValueError("topic id must be a safe directory name")
        return value


class PlatformSettings(Model):
    search_mode: Literal["native", "web"] = "web"
    backend: Literal["auto", "yt-dlp", "browser"] = "auto"
    cookie_file: Path | None = None
    browser_executable: Path | None = None
    browser_headless: bool = True
    browser_state: Path | None = None
    request_interval_seconds: float = Field(default=2, ge=0)
    format_sort: list[str] = Field(default_factory=list)
    format: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"


class Runtime(Model):
    dataset_root: Path = Path("datasets")
    platforms: dict[Platform, PlatformSettings] = Field(
        default_factory=lambda: {
            p: PlatformSettings(
                search_mode="native" if p in (Platform.YOUTUBE, Platform.BILIBILI) else "web"
            )
            for p in Platform
        }
    )
    retries: int = Field(default=1, ge=0, le=3)
    timeout_seconds: float = Field(default=30, gt=0)


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError(f"expected a YAML mapping: {path}")
    return data


def _relative(path: Path, parent: Path) -> Path:
    return (parent / path).resolve() if not path.is_absolute() else path.resolve()


def load_topic(path: Path) -> Topic:
    path = path.resolve()
    topic = Topic.model_validate(_read(path))
    topic.screen.references = [_relative(p, path.parent) for p in topic.screen.references]
    if any(not p.is_file() for p in topic.screen.references):
        raise ValueError("reference file does not exist")
    return topic


def resolve_topic(value: str | Path | None) -> Topic:
    """A YAML file defines a full topic; other text is one literal test query."""
    if value is None:
        return load_topic(Path("configs/topics/labubu.yaml"))
    text = str(value).strip()
    if not text:
        raise ValueError("topic must not be empty")
    path = Path(text).expanduser()
    if path.suffix.casefold() in (".yaml", ".yml") or path.is_file():
        if not path.is_file():
            from .models import CollectionError

            raise CollectionError("topic_file_not_found", "config")
        return load_topic(path)
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return Topic(
        id="test-" + digest,
        search={"shared_queries": [text]},
    )


def load_runtime(path: Path | None = None) -> Runtime:
    runtime = Runtime.model_validate(_read(path)) if path else Runtime()
    parent = path.resolve().parent if path else Path.cwd()
    runtime.dataset_root = _relative(runtime.dataset_root, parent)
    for settings in runtime.platforms.values():
        for field in ("cookie_file", "browser_executable", "browser_state"):
            value = getattr(settings, field)
            if value is not None:
                setattr(settings, field, _relative(value, parent))
    return runtime
