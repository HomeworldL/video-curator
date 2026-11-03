"""Canonical resource identities for same-platform deduplication; no content judgment."""

import hashlib
import re
from urllib.parse import urlsplit

from .models import CollectionError, Platform, ResourceIdentity
from .platforms.bilibili.urls import parse as parse_bilibili
from .platforms.douyin.urls import parse as parse_douyin
from .platforms.xiaohongshu.urls import parse as parse_xiaohongshu
from .platforms.youtube.urls import parse as parse_youtube

DOMAINS = {
    Platform.YOUTUBE: ("youtube.com", "youtu.be"),
    Platform.BILIBILI: ("bilibili.com", "b23.tv"),
    Platform.DOUYIN: ("douyin.com", "iesdouyin.com"),
    Platform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
}
PARSERS = {
    Platform.YOUTUBE: parse_youtube,
    Platform.BILIBILI: parse_bilibili,
    Platform.DOUYIN: parse_douyin,
    Platform.XIAOHONGSHU: parse_xiaohongshu,
}


def platform_for_url(url: str) -> Platform:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or parts.username or parts.password:
        raise CollectionError("unsupported_url", "identity")
    for platform, domains in DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    raise CollectionError("unsupported_platform", "identity")


def identify(url: str, raw: dict | None = None) -> ResourceIdentity:
    platform = platform_for_url(url)
    result = PARSERS[platform](url, raw or {})
    if result is not None:
        content_id, part_id, canonical_url = result
        if not re.fullmatch(r"[A-Za-z0-9_-]+", content_id):
            raise CollectionError("invalid_resource_id", "identity")
        return ResourceIdentity(
            platform=platform, content_id=content_id, part_id=part_id, canonical_url=canonical_url
        )
    return ResourceIdentity(
        platform=platform,
        content_id="url_" + hashlib.sha256(url.encode()).hexdigest(),
        canonical_url=url,
        provisional=True,
    )


def file_stem(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]
