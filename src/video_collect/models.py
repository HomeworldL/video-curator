"""Shared contracts. No network, database or filesystem operations."""

from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Platform(StrEnum):
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"


class CollectionError(Exception):
    """Stable public error code with optional sanitized diagnostic details."""

    def __init__(self, code: str, stage: str, *, diagnostics: dict | None = None):
        self.code, self.stage = code, stage
        self.diagnostics = diagnostics or {}
        super().__init__(f"{stage}: {code}")


class ResourceIdentity(Model):
    platform: Platform
    content_id: str
    part_id: str = ""
    canonical_url: str
    provisional: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.content_id}:{self.part_id}"


class Candidate(Model):
    platform: Platform
    url: str
    title: str | None = None
    author: str | None = None
    query: str | None = None
    mode: str = "import"
    position: str | None = None


class SearchRequest(Model):
    platform: Platform
    query: str
    mode: Literal["native", "web"] = "native"
    limit: int = Field(default=10, ge=1, le=200)
    page: int = Field(default=1, ge=1)


class SearchBatch(Model):
    request: SearchRequest
    candidates: list[Candidate]
    pagination: Literal["limit", "page"]
    next_page: int | None = None
    stop_reason: str
    raw: dict[str, Any] = Field(default_factory=dict)


class VideoDetails(Model):
    access_url: str
    title: str | None = None
    description: str | None = None
    author: str | None = None
    author_id: str | None = None
    published_at: str | None = None
    duration_seconds: float | None = None
    tags: list[str] = Field(default_factory=list)
    thumbnails: list[str] = Field(default_factory=list)
    media_type: str = "video"
    is_live: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class VideoMetadata(VideoDetails):
    identity: ResourceIdentity


Data = TypeVar("Data")


class Stage(Model, Generic[Data]):
    status: Literal["not_run", "complete", "failed", "stale"] = "not_run"
    error: str | None = None
    data: Data | None = None


class ManifestIdentity(ResourceIdentity):
    aliases: list[str] = Field(default_factory=list)


class SearchHit(Candidate):
    discovered_at: str


class FilterDecision(Model):
    decision: Literal["keep", "reject", "uncertain", "unclassified"]
    reason: str | None = None


class DownloadRequest(Model):
    metadata: VideoMetadata
    directory: Path
    max_bytes: int = Field(gt=0)


class DownloadResult(Model):
    path: Path
    backend: str


class MediaInfo(Model):
    path: Path
    bytes: int
    sha256: str
    duration_seconds: float
    width: int
    height: int
    has_audio: bool


class PreviewFrame(Model):
    number: int = Field(ge=1, le=32)
    timestamp_seconds: float = Field(ge=0)


class PreviewEvidence(Model):
    page: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    sampling: Literal["fixed_intervals", "legacy_whole_video"] = "fixed_intervals"
    kind: Literal["cover_and_sampled_frames"] = "cover_and_sampled_frames"
    path: Path
    sha256: str
    source: Literal["web_video", "local_original"]
    duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    cover_present: bool
    cover_error: str | None = None
    frames: list[PreviewFrame] = Field(min_length=1, max_length=32)


class PageJudgment(Model):
    page: int = Field(ge=1)
    grade: Literal["priority", "partial", "difficult", "low"]
    reason: str = Field(min_length=1, max_length=1500)
    evidence_frames: list[int] = Field(default_factory=list, max_length=32)


class ScreenResult(Model):
    pages: list[PageJudgment] = Field(min_length=1)


class ScreenPage(Model):
    page: int = Field(ge=1)
    status: Literal["not_run", "complete", "failed"] = "not_run"
    error: str | None = None
    grade: Literal["priority", "partial", "difficult", "low"] | None = None
    reason: str | None = None
    evidence_frames: list[int] = Field(default_factory=list)
    evidence: PreviewEvidence


class ScreenData(Model):
    grade: Literal["priority", "partial", "difficult", "low"] | None = None
    reason: str | None = None
    pages: list[ScreenPage] = Field(default_factory=list)


class VideoRecord(Model):
    schema_version: Literal["video_collect.video.v3"] = Field(
        default="video_collect.video.v3", alias="schema"
    )
    record_id: str
    identity: ManifestIdentity
    search: Stage[list[SearchHit]] = Field(default_factory=Stage)
    metadata: Stage[VideoDetails] = Field(default_factory=Stage)
    filter: Stage[FilterDecision] = Field(default_factory=Stage)
    download: Stage[MediaInfo] = Field(default_factory=Stage)
    screening: Stage[ScreenData] = Field(default_factory=Stage)


class BatchTextFilterResult(Model):
    id: int = Field(ge=1, le=200)
    decision: Literal["keep", "reject", "uncertain"]
    reason: str = Field(min_length=1, max_length=500)


class TextFilterResponse(Model):
    results: list[BatchTextFilterResult] = Field(min_length=1, max_length=200)
