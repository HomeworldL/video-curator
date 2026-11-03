import json

import httpx
import pytest

from video_curator.config import FilterSettings
from video_curator.models import CollectionError
from video_curator.providers import doubao


def test_429_preserves_provider_code_request_id_and_retry_after_without_secrets(monkeypatch):
    key = "test-private-api-key"
    monkeypatch.setenv("ARK_API_KEY", key)
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": "RateLimitExceeded.EndpointTPMExceeded",
                    "type": "TooManyRequests",
                    "message": "Token limit reached: " + key + " Bearer another-private-token",
                    "unexpected_secret_field": "not-for-logs",
                },
            },
            headers={
                "x-request-id": "request-123",
                "x-tt-logid": "log-123",
                "retry-after": "30",
                "x-ratelimit-remaining-tokens": "0",
                "set-cookie": "private-cookie",
                "authorization": "Bearer " + key,
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CollectionError) as caught:
        doubao.complete_text("prompt", [], FilterSettings(), "model")
    error = caught.value
    assert error.code == "rate_limited"
    detail = error.diagnostics
    assert detail["http_status"] == 429
    assert detail["provider_error"]["code"] == "RateLimitExceeded.EndpointTPMExceeded"
    assert detail["request_id"] == "request-123"
    assert detail["retry_after"] == "30"
    assert detail["headers"]["x-ratelimit-remaining-tokens"] == "0"
    saved = json.dumps(detail)
    for secret in (key, "another-private-token", "private-cookie", "not-for-logs"):
        assert secret not in saved
    assert len(calls) == 1  # Diagnostics must not introduce retries.


@pytest.mark.parametrize(
    "status,code",
    [(401, "authentication_failed"), (400, "request_rejected"), (503, "outcome_unknown")],
)
def test_http_failures_keep_status_and_provider_message(monkeypatch, status, code):
    monkeypatch.setenv("ARK_API_KEY", "secret-key")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            status,
            json={
                "error": {"code": "SpecificProviderCode", "message": "specific cause"},
                "request_id": "body-request-id",
            },
        ),
    )
    with pytest.raises(CollectionError) as caught:
        doubao.complete_text("prompt", [], FilterSettings(), "model")
    assert caught.value.code == code
    assert caught.value.diagnostics["http_status"] == status
    assert caught.value.diagnostics["request_id"] == "body-request-id"


def test_timeout_records_exception_class_without_request_credentials(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "secret-key")

    def post(*a, **k):
        raise httpx.ReadTimeout("secret-key should not appear in diagnostics")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CollectionError) as caught:
        doubao.complete_text("prompt", [], FilterSettings(), "model")
    assert caught.value.diagnostics["transport_error"] == "ReadTimeout"
    assert "secret-key" not in json.dumps(caught.value.diagnostics)


def test_non_json_response_has_bounded_redacted_preview(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "secret-key")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: httpx.Response(200, text="secret-key " + "x" * 3000)
    )
    with pytest.raises(CollectionError) as caught:
        doubao.complete_text("prompt", [], FilterSettings(), "model")
    assert caught.value.code == "invalid_model_json"
    preview = caught.value.diagnostics["body_preview"]
    assert len(preview) <= 1024 and "secret-key" not in preview


def test_success_keeps_trace_without_changing_model_output(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "secret-key")
    payload = {
        "id": "completion-id",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"results":[]}'}}],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=payload))
    result = doubao.complete_text("prompt", [], FilterSettings(), "model")
    assert result["choices"] == payload["choices"]
    assert result["_transport"]["completion_id"] == "completion-id"
    assert "request_id" not in result["_transport"]
    assert result["_transport"]["http_status"] == 200


@pytest.mark.parametrize(
    "content,finish", [("not json", "stop"), ("{}", "length"), ('{"grade":"low"}', "stop")]
)
def test_screen_retains_usage_for_success_and_invalid_output(
    monkeypatch, tmp_path, content, finish
):
    from types import SimpleNamespace

    from video_curator.config import ScreenSettings

    image = tmp_path / "contact.jpg"
    image.write_bytes(b"fixture")
    usage = {
        "prompt_tokens": 3000,
        "completion_tokens": 200,
        "total_tokens": 3200,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    monkeypatch.setattr(
        doubao,
        "_request",
        lambda *a: {
            "usage": usage,
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "_transport": {"request_id": "request-test"},
        },
    )
    if content == "not json" or finish == "length":
        with pytest.raises(CollectionError) as caught:
            doubao.complete_screen(
                "policy", "context", [SimpleNamespace(path=image, page=1)], ScreenSettings()
            )
        result = caught.value.diagnostics
        assert result["request_id"] == "request-test"
    else:
        result = doubao.complete_screen(
            "policy", "context", [SimpleNamespace(path=image, page=1)], ScreenSettings()
        )
        assert result["result"] == {"grade": "low"}
    assert result["usage"] == {
        "prompt_tokens": 3000,
        "completion_tokens": 200,
        "total_tokens": 3200,
        "cached_tokens": 100,
    }
    assert doubao.token_usage({}) is None


def test_screen_transport_sends_all_numbered_images(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from video_curator.config import ScreenSettings

    image = tmp_path / "image.jpg"
    image.write_bytes(b"test")
    sent = []

    def request(body, *args):
        sent.append(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"pages":[]}'}}]}

    monkeypatch.setattr(doubao, "_request", request)
    doubao.complete_screen(
        "policy", "context", [SimpleNamespace(page=n, path=image) for n in (1, 2)], ScreenSettings()
    )
    content = sent[0]["messages"][1]["content"]
    assert sum(x["type"] == "image_url" for x in content) == 2
    assert [x["text"] for x in content if x["type"] == "text"] == ["context", "Page 1", "Page 2"]
