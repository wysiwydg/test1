"""A small HTTP client, on the standard library.

There is no ``requests`` here, and that is a deliberate call rather than
asceticism. This package is deployed into insurance compliance units where
adding a dependency means a security review, and where the machine that talks
to the AMLC portal is often the most locked-down one in the building. Standard
library only means the tool installs where it has to run.

What it does add over ``urllib`` directly, because every one of these has bitten
somebody filing a report:

*   **Retries with backoff on the failures that are worth retrying** — 429, 5xx
    and transport errors — and never on a 4xx that will fail identically the
    second time.
*   **``Retry-After`` is honoured.** A vendor telling you when to come back is
    more informative than any backoff curve.
*   **Timeouts always.** A submission that hangs forever is a submission that
    misses a deadline while looking healthy.
*   **Secrets never reach the logs.** Authorization headers are redacted at the
    boundary, not by remembering to be careful at each call site.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["HttpError", "HttpResponse", "HttpClient"]

log = logging.getLogger("aml.http")

_REDACTED = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


class HttpError(RuntimeError):
    """A request failed after exhausting retries."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: ("<redacted>" if key.lower() in _REDACTED else value)
        for key, value in headers.items()
    }


class HttpClient:
    """Minimal client with retries, timeouts and optional mutual TLS."""

    def __init__(
        self,
        *,
        timeout: int = 30,
        max_retries: int = 3,
        verify_tls: bool = True,
        client_cert: str = "",
        client_key: str = "",
        user_agent: str = "aml-monitor/1.0 (+stdlib)",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        context = ssl.create_default_context()
        if not verify_tls:
            # Available because some institutions terminate TLS at an inspection
            # proxy with a private CA they cannot install. Loud on purpose.
            log.warning("TLS verification disabled — certificates will not be checked")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if client_cert:
            context.load_cert_chain(client_cert, client_key or None)
        self._context = context

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        retry_on: tuple[int, ...] = (429, 500, 502, 503, 504),
    ) -> HttpResponse:
        sent = {"User-Agent": self.user_agent, **(dict(headers) if headers else {})}
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(url, data=body, method=method.upper())
            for key, value in sent.items():
                request.add_header(key, value)
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self._context
                ) as response:
                    return HttpResponse(
                        status=response.status,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        body=response.read(),
                    )
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                status = exc.code
                if status not in retry_on or attempt > self.max_retries:
                    raise HttpError(
                        f"{method.upper()} {url} failed with HTTP {status}",
                        status=status,
                        body=payload.decode("utf-8", errors="replace")[:2000],
                    ) from exc
                wait = self._backoff(attempt, exc.headers.get("Retry-After"))
                log.warning(
                    "HTTP %s from %s (attempt %d/%d); retrying in %.1fs; headers=%s",
                    status, url, attempt, self.max_retries + 1, wait, _safe_headers(sent),
                )
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                if attempt > self.max_retries:
                    raise HttpError(f"{method.upper()} {url} failed: {exc}") from exc
                wait = self._backoff(attempt, None)
                log.warning(
                    "transport error on %s (attempt %d/%d): %s; retrying in %.1fs",
                    url, attempt, self.max_retries + 1, exc, wait,
                )
                time.sleep(wait)

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post_json(
        self, url: str, payload: Any, *, headers: Mapping[str, str] | None = None
    ) -> HttpResponse:
        body = json.dumps(payload).encode("utf-8")
        sent = {"Content-Type": "application/json", "Accept": "application/json"}
        sent.update(dict(headers) if headers else {})
        sent.setdefault("Content-Length", str(len(body)))
        return self.request("POST", url, headers=sent, body=body)

    def post_form(
        self, url: str, fields: Mapping[str, str], *, headers: Mapping[str, str] | None = None
    ) -> HttpResponse:
        body = urllib.parse.urlencode(dict(fields)).encode("utf-8")
        sent = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        sent.update(dict(headers) if headers else {})
        return self.request("POST", url, headers=sent, body=body)

    @staticmethod
    def basic_auth(username: str, password: str) -> str:
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return f"Basic {token}"

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
        # Jittered exponential. The jitter matters when a nightly batch screens
        # thousands of customers: without it every retry lands at the same
        # moment and the vendor throttles the lot again.
        return min(2.0 ** attempt, 60.0) * (0.5 + random.random() / 2)
