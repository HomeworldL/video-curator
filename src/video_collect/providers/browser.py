"""Concrete browser lifecycle shared by platform-specific page functions."""

from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar

from ..config import PlatformSettings, Runtime
from ..models import CollectionError


@contextmanager
def page(settings: PlatformSettings, runtime: Runtime, *, stage: str):
    """Create a fresh context; only explicitly configured sessions may be imported."""
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        raise CollectionError("browser_dependency_missing", stage) from None
    if settings.browser_state and not settings.browser_state.is_file():
        raise CollectionError("browser_state_missing", stage)
    if settings.browser_executable and not settings.browser_executable.is_file():
        raise CollectionError("browser_executable_missing", stage)
    try:
        with sync_playwright() as driver:
            launch = {"headless": settings.browser_headless}
            if settings.browser_executable:
                launch["executable_path"] = str(settings.browser_executable)
            try:
                browser = driver.chromium.launch(**launch)
            except Error:
                raise CollectionError("browser_launch_failed_check_install", stage) from None
            try:
                context_options = {}
                if settings.browser_state:
                    context_options["storage_state"] = str(settings.browser_state)
                context = browser.new_context(**context_options)
                if settings.cookie_file:
                    jar = MozillaCookieJar(str(settings.cookie_file))
                    try:
                        jar.load(ignore_discard=True, ignore_expires=False)
                    except (OSError, ValueError):
                        raise CollectionError("cookie_file_unreadable", stage) from None
                    cookies = [
                        {
                            "name": cookie.name,
                            "value": cookie.value,
                            "domain": cookie.domain,
                            "path": cookie.path,
                            "secure": cookie.secure,
                            **({"expires": cookie.expires} if cookie.expires else {}),
                        }
                        for cookie in jar
                    ]
                    context.add_cookies(cookies)
                active = context.new_page()
                active.set_default_timeout(runtime.timeout_seconds * 1000)
                active.set_default_navigation_timeout(runtime.timeout_seconds * 1000)
                yield active
            finally:
                browser.close()
    except TimeoutError:
        raise CollectionError("browser_timeout", stage) from None
    except Error:
        raise CollectionError("browser_request_failed", stage) from None
