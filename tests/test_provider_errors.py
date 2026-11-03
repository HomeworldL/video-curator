from video_curator.providers.ytdlp import _failure


def test_fresh_visitor_cookies_do_not_imply_login_required():
    error = _failure(Exception("Fresh cookies (not necessarily logged in) are needed"), "metadata")
    assert error.code == "needs_fresh_cookies"


def test_actual_login_request_is_distinct_from_cookie_error():
    assert (
        _failure(Exception("Please sign in to view this video"), "metadata").code == "needs_login"
    )
    assert _failure(Exception("Failed to load cookies"), "metadata").code == "session_cookie_error"


def test_missing_formats_does_not_imply_login_or_irrelevance():
    assert (
        _failure(Exception("No video formats found!"), "metadata").code
        == "media_formats_unavailable"
    )


def test_trial_720p_preference_handles_both_orientations():
    from yt_dlp import YoutubeDL

    from video_curator.config import Runtime
    from video_curator.providers.ytdlp import options

    runtime = Runtime()
    runtime.platforms["bilibili"].format = "bestvideo+bestaudio/best"
    runtime.platforms["bilibili"].format_sort = ["res:720"]
    for portrait in (False, True):
        formats = []
        for width, height in ((640, 360), (1280, 720), (1920, 1080)):
            if portrait:
                width, height = height, width
            formats.append(
                {
                    "format_id": str(min(width, height)),
                    "url": "https://example.com/video.mp4",
                    "width": width,
                    "height": height,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "ext": "mp4",
                }
            )
        with YoutubeDL(options(runtime.platforms["bilibili"], runtime)) as ydl:
            result = ydl.process_ie_result(
                {"id": "offline-test", "title": "test", "formats": formats}, download=False
            )
        assert min(result["width"], result["height"]) == 720
