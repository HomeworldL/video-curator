"""Download a full XHS stream; common media verification remains in media.py."""

import httpx

from ...config import Runtime
from ...models import CollectionError, DownloadRequest, DownloadResult
from .detail import media_url


def download(request: DownloadRequest, runtime: Runtime) -> DownloadResult:
    formats = request.metadata.raw.get("formats") or []
    urls = list(dict.fromkeys(item.get("url") for item in formats if media_url(item.get("url"))))
    if not urls:
        raise CollectionError("media_formats_unavailable", "download")
    request.directory.mkdir(parents=True, exist_ok=True)
    target = request.directory / "source.mp4"
    partial = request.directory / "source.mp4.part"
    last_code = "media_download_failed"
    headers = {"Referer": "https://www.xiaohongshu.com/", "User-Agent": "Mozilla/5.0"}
    with httpx.Client(
        timeout=runtime.timeout_seconds, follow_redirects=False, headers=headers
    ) as client:
        for original in urls:
            url = original
            try:
                for _ in range(5):
                    with client.stream("GET", url) as response:
                        if response.is_redirect:
                            next_url = str(response.next_request.url)
                            if not media_url(next_url):
                                raise CollectionError("media_redirect_untrusted", "download")
                            url = next_url
                            continue
                        if response.status_code in (401, 403, 404, 410, 429):
                            last_code = (
                                "rate_limited"
                                if response.status_code == 429
                                else "media_url_unavailable"
                            )
                            break
                        if response.status_code != 200:
                            last_code = "media_http_error"
                            break
                        declared = int(response.headers.get("content-length") or 0)
                        if declared > request.max_bytes:
                            raise CollectionError("download_byte_budget", "download")
                        size = 0
                        with partial.open("wb") as file:
                            for chunk in response.iter_bytes(256 * 1024):
                                size += len(chunk)
                                if size > request.max_bytes:
                                    raise CollectionError("download_byte_budget", "download")
                                file.write(chunk)
                        if not size or (declared and size != declared):
                            last_code = "incomplete_media_response"
                            break
                        partial.replace(target)
                        return DownloadResult(path=target, backend="xiaohongshu-browser")
                else:
                    last_code = "media_redirect_limit"
            except httpx.HTTPError:
                last_code = "media_network_error"
    raise CollectionError(last_code, "download")
