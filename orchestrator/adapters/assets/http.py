"""Strict bounded HTTPS transport for public asset catalogues."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from orchestrator.platform.errors import JobError, JobErrorKind

META = {"api.polyhaven.com", "api.sketchfab.com"}
RESOURCES = {"dl.polyhaven.org", "dl.polyhaven.com", "cdn.polyhaven.com", "sketchfab-prod-media.s3.amazonaws.com", "sketchfab-prod-media.s3.eu-west-1.amazonaws.com", "media.sketchfab.com"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AssetHTTP:
    def __init__(self, settings, *, opener=None, sleep=None) -> None:
        self.settings = settings
        self.sleep = sleep or __import__("time").sleep
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def get_json(self, url: str, *, authenticated: bool = False) -> dict:
        raw = self._get(url, max_bytes=4 * 1024 * 1024, authenticated=authenticated, json_mode=True)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider returned malformed JSON") from None
        if not isinstance(value, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider JSON response must be an object")
        return value

    def get_bytes(self, url: str, *, max_bytes: int, authenticated: bool = False) -> bytes:
        return self._get(url, max_bytes=max_bytes, authenticated=authenticated, json_mode=False)

    def _get(self, url: str, *, max_bytes: int, authenticated: bool, json_mode: bool) -> bytes:
        cap = self.settings.asset_providers.max_attempts
        for attempt in range(1, cap + 1):
            try:
                return self._get_once(url, max_bytes=max_bytes, authenticated=authenticated, json_mode=json_mode)
            except JobError as exc:
                policy = exc.kind.policy.retry
                if policy is None or attempt >= min(cap, policy.max_attempts):
                    raise
                delay = min(policy.delay_before_retry(attempt), 60.0)
                if exc.kind is JobErrorKind.PROVIDER_RATE_LIMIT:
                    retry_after = getattr(exc, "retry_after", None)
                    if retry_after is not None:
                        delay = min(retry_after, 60.0)
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _get_once(self, url: str, *, max_bytes: int, authenticated: bool, json_mode: bool) -> bytes:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        current = url
        initial = urllib.parse.urlsplit(url)
        if authenticated and not (initial.scheme == "https" and initial.hostname == "api.sketchfab.com" and initial.path.startswith("/v3/")):
            raise ValueError("authenticated requests are restricted to the Sketchfab API")
        auth = authenticated
        for hop in range(4):
            try:
                parsed = _validate_url(current, metadata=(hop == 0 and urllib.parse.urlsplit(current).hostname in META))
            except ValueError:
                raise JobError(JobErrorKind.INVALID_MANIFEST, "Provider returned a disallowed download URL") from None
            if auth and not (parsed.hostname == "api.sketchfab.com" and parsed.path.startswith("/v3/")):
                raise ValueError("authenticated requests are restricted to the Sketchfab API")
            headers = {"User-Agent": self.settings.asset_providers.user_agent, "Accept": "application/json" if json_mode else "*/*"}
            if auth:
                token = _sketchfab_token(self.settings)
                headers["Authorization"] = f"Token {token}"
            request = urllib.request.Request(current, headers=headers, method="GET")
            try:
                response = self.opener(request, timeout=self.settings.asset_providers.request_timeout_seconds)
            except urllib.error.HTTPError as exc:
                response = exc
            except (OSError, TimeoutError, urllib.error.URLError):
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Provider network request failed") from None
            with response:
                status = getattr(response, "status", getattr(response, "code", 200))
                if status in {301, 302, 303, 307, 308}:
                    if hop == 3:
                        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Provider redirect limit exceeded")
                    location = response.headers.get("Location")
                    if not location:
                        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Provider redirect omitted Location")
                    current = urllib.parse.urljoin(current, location)
                    try:
                        _validate_url(current, metadata=False)
                    except ValueError:
                        raise JobError(JobErrorKind.INVALID_MANIFEST, "Provider redirect target is disallowed") from None
                    auth = False
                    continue
                if status == 429:
                    error = JobError(JobErrorKind.PROVIDER_RATE_LIMIT, "Provider rate limit reached")
                    try:
                        parsed_retry_after = float(response.headers.get("Retry-After"))
                        error.retry_after = min(parsed_retry_after, 60) if math.isfinite(parsed_retry_after) and parsed_retry_after >= 0 else None
                    except (TypeError, ValueError):
                        error.retry_after = None
                    raise error
                if status >= 500:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Provider service is unavailable")
                if status >= 400:
                    raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, f"Provider rejected request (HTTP {status})")
                chunks = bytearray()
                while True:
                    try:
                        block = response.read(min(64 * 1024, max_bytes + 1 - len(chunks)))
                    except (OSError, TimeoutError, urllib.error.URLError):
                        raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Provider response ended during download") from None
                    if not block:
                        break
                    chunks.extend(block)
                    if len(chunks) > max_bytes:
                        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider response exceeds configured size limit")
                return bytes(chunks)
        raise AssertionError("unreachable")


def _validate_url(url: str, *, metadata: bool):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("provider URL is malformed") from None
    allowed = META if metadata else META | RESOURCES
    if parsed.scheme != "https" or host not in allowed or parsed.username or parsed.password or port not in (None, 443) or parsed.fragment:
        raise ValueError("provider URL is outside the HTTPS allowlist")
    if any(ord(c) < 32 for c in url):
        raise ValueError("provider URL contains control characters")
    return parsed


def _sketchfab_token(settings) -> str:
    cfg = settings.asset_providers
    if "sketchfab" in settings.credentials:
        try:
            token = settings.resolve_credential("sketchfab")
        except (KeyError, ValueError):
            token = ""
    elif cfg.sketchfab_token_file is not None:
        try:
            token = cfg.sketchfab_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            token = ""
    else:
        token = ""
    if not token or not token.isascii() or not token.isprintable() or token != token.strip() or any(c.isspace() for c in token):
        from .models import ProviderConfigurationError
        raise ProviderConfigurationError("sketchfab", "Sketchfab credential is missing or empty")
    settings.register_runtime_secret(token)
    return token
