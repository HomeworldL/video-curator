"""Explicit, isolated browser access; anonymous unless state is explicitly supplied."""

from urllib.parse import urlsplit

from ...models import CollectionError


def navigate(page, url: str, stage: str):
    host = (urlsplit(url).hostname or "").lower()
    if not (
        host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com") or host == "xhslink.com"
    ):
        raise CollectionError("unsupported_url", stage)
    response = page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status in (403, 412, 461, 471):
        raise CollectionError("platform_access_blocked", stage)
    if response is not None and response.status == 429:
        raise CollectionError("rate_limited", stage)


def access_issue(page) -> str | None:
    """Only report login from a visible wall; a login button alone is not a wall."""
    text = page.locator("body").inner_text(timeout=5000)
    if any(marker in text for marker in ("安全限制", "IP存在风险", "300012", "访问频次异常")):
        return "platform_access_blocked"
    if (
        page.locator(".login-container, .login-modal, .login-box, .login-mask")
        .filter(visible=True)
        .count()
    ):
        return "needs_login"
    if any(
        marker in text for marker in ("登录后查看搜索结果", "登录后查看更多", "登录后查看完整内容")
    ):
        return "needs_login"
    if any(marker in text for marker in ("请完成验证", "安全验证", "访问频次异常", "滑动验证")):
        return "platform_verification_required"
    return None
