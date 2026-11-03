"""Normal browser visits and bounded page-state reads; never solve login/CAPTCHA gates."""

import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from ...config import Runtime
from ...models import CollectionError

SEARCH_PATHS = (
    "/aweme/v1/web/general/search/single/",
    "/aweme/v1/web/search/item/",
)
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"


def gate_code(title: str, body: str, url: str = "") -> str | None:
    """A generic 'login' navbar is not evidence that the requested feature is gated."""
    if "验证码" in title or any(
        text in body
        for text in ("请完成下方验证", "拖动滑块完成拼图", "请完成验证后继续访问", "验证后继续访问")
    ):
        return "verification_required"
    if any(
        text in body
        for text in (
            "登录后搜索",
            "登录后查看搜索结果",
            "登录后查看更多搜索结果",
            "请登录后搜索",
            "登录后即可搜索",
            "登录后查看完整视频",
        )
    ):
        return "needs_login"
    host = urlsplit(url).hostname or ""
    if host in ("verify.douyin.com", "verify.snssdk.com"):
        return "verification_required"
    return None


def page_gate(active, stage: str):
    try:
        title = active.title()
        body = active.locator("body").inner_text(timeout=1500)
        code = gate_code(title, body, active.url)
    except Exception:
        return
    if code:
        raise CollectionError(code, stage)


def visit_home(active, runtime: Runtime, stage: str):
    # A new context lets Douyin's normal JavaScript issue a visitor session. Existing
    # personal browser profiles are never consulted by this platform module.
    active.goto("https://www.douyin.com/", wait_until="domcontentloaded")
    active.wait_for_timeout(min(2000, runtime.timeout_seconds * 150))
    page_gate(active, stage)


def wait_for(active, ready: Callable[[], bool], runtime: Runtime, stage: str):
    deadline = time.monotonic() + min(runtime.timeout_seconds, 20)
    while time.monotonic() < deadline:
        if ready():
            return
        page_gate(active, stage)
        active.wait_for_timeout(300)
    page_gate(active, stage)


def search_response_url(url: str, keyword: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.hostname in ("www.douyin.com", "www.iesdouyin.com")
        and parts.path in SEARCH_PATHS
        and parse_qs(parts.query).get("keyword", [""])[0] == keyword
    )


def detail_response_url(url: str, content_id: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.hostname in ("www.douyin.com", "www.iesdouyin.com")
        and parts.path == DETAIL_PATH
        and parse_qs(parts.query).get("aweme_id", [""])[0] == content_id
    )


def embedded_video(active, content_id: str) -> dict | None:
    """Read known hydration roots for the exact requested resource, never recommendations."""
    return active.evaluate(
        """(expected) => {
        const roots = [window._ROUTER_DATA, window.__INITIAL_STATE__];
        for (const id of ['RENDER_DATA', '__NEXT_DATA__']) {
            const text = document.getElementById(id)?.textContent;
            if (!text) continue;
            try { roots.push(JSON.parse(decodeURIComponent(text))); } catch (_) {}
        }
        const seen = new Set(); let count = 0;
        while (roots.length && count++ < 10000) {
            const value = roots.pop();
            if (!value || typeof value !== 'object' || seen.has(value)) continue;
            seen.add(value);
            if (String(value.aweme_id) === expected && value.video) return value;
            for (const item of Object.values(value)) {
                if (item && typeof item === 'object') roots.push(item);
            }
        }
        return null;
    }""",
        content_id,
    )
