"""Doubao transport only. Semantic validation is performed by screening.py."""

import base64
import json
import mimetypes
import os
import re
import time

import httpx

from ..config import FilterSettings
from ..models import CollectionError


def _image(path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def _response_diagnostics(response, payload, key: str, seconds: float) -> dict:
    def clean(value, limit=2048):
        text = str(value).replace(key, "[redacted]") if key else str(value)
        text = re.sub(r"(?i)bearer\s+[^\s\"'<>]+", "Bearer [redacted]", text)
        return text[:limit]

    headers = {
        name: clean(value, 512)
        for name, value in response.headers.items()
        if name
        in {
            "x-request-id",
            "x-tt-logid",
            "x-ark-request-id",
            "retry-after",
            "x-error-code",
            "x-ratelimit-reset",
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-remaining-tokens",
        }
    }
    result = {"http_status": response.status_code, "elapsed_seconds": seconds, "headers": headers}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            result["provider_error"] = {
                k: clean(error[k])
                for k in ("code", "type", "message", "param")
                if error.get(k) is not None
            }
        elif error is not None:
            result["provider_error"] = {"message": clean(error)}
        metadata = payload.get("ResponseMetadata")
        request_id = (
            headers.get("x-request-id")
            or headers.get("x-tt-logid")
            or headers.get("x-ark-request-id")
            or payload.get("request_id")
            or (metadata.get("RequestId") if isinstance(metadata, dict) else None)
        )
        if payload.get("id") is not None:
            result["completion_id"] = clean(payload["id"], 512)
    else:
        request_id = (
            headers.get("x-request-id")
            or headers.get("x-tt-logid")
            or headers.get("x-ark-request-id")
        )
        result["body_preview"] = clean(response.text, 1024)
    if request_id is not None:
        result["request_id"] = clean(request_id, 512)
    if "retry-after" in headers:
        result["retry_after"] = headers["retry-after"]
    return result


def _request(body: dict, base_url: str, settings, stage: str) -> dict:
    key = os.environ.get(settings.api_key_env, "").strip()
    if not key:
        raise CollectionError("api_key_missing", stage)
    started = time.monotonic()
    try:
        # A timeout may already have consumed inference: never retry implicitly.
        response = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=settings.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise CollectionError(
            "outcome_unknown",
            stage,
            diagnostics={
                "transport_error": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
        ) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    diagnostic = _response_diagnostics(response, payload, key, round(time.monotonic() - started, 3))
    if response.status_code in (401, 403):
        code = "authentication_failed"
    elif response.status_code == 429:
        code = "rate_limited"
    elif response.status_code >= 500:
        code = "outcome_unknown"
    elif response.status_code != 200:
        code = "request_rejected"
    elif not isinstance(payload, dict):
        code = "invalid_model_json"
    else:
        return {**payload, "_transport": diagnostic}
    raise CollectionError(code, stage, diagnostics=diagnostic)


def complete_text(system: str, items: list[dict], settings: FilterSettings, model: str) -> dict:
    """One text-only request for a batch. No thinking, browsing or tools."""
    if not model:
        raise CollectionError("model_missing", "filter")
    return _request(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"videos": items}, ensure_ascii=False)},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": settings.max_tokens,
        },
        settings.base_url,
        settings,
        "filter",
    )


def token_usage(payload: dict) -> dict | None:
    """Keep measured token counts; a missing usage block is not zero usage."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    result = {}
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            return None
        result[name] = value
    total = usage.get("total_tokens")
    result["total_tokens"] = total if type(total) is int and total >= 0 else sum(result.values())
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if type(cached) is int and cached >= 0:
        result["cached_tokens"] = cached
    return result


def complete_screen(system: str, context: str, pages, settings, references=()) -> dict:
    """One video per request, with one or more numbered contact sheets."""
    model = os.environ.get(settings.model_env) or settings.model
    content = [{"type": "text", "text": context}]
    for path in references:
        content.extend([{"type": "text", "text": "身份参考图，不是候选视频帧。"}, _image(path)])
    for page in pages:
        content.extend([{"type": "text", "text": f"Page {page.page}"}, _image(page.path)])
    payload = _request(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": settings.max_tokens,
            "thinking": {"type": "disabled"},
        },
        settings.base_url,
        settings,
        "screen",
    )
    try:
        choice = payload["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("incomplete output")
        result = json.loads(choice["message"]["content"])
        if not isinstance(result, dict):
            raise ValueError("object required")
        return {"result": result, "usage": token_usage(payload)}
    except (ValueError, KeyError, IndexError, TypeError):
        raise CollectionError(
            "invalid_model_json",
            "screen",
            diagnostics={**payload.get("_transport", {}), "usage": token_usage(payload)},
        ) from None
