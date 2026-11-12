"""HTTP receiver for Invision Community webhooks.

Routes:
    POST /ips/{secret}            event name taken from headers/body
    POST /ips/{secret}/{event}    event name taken from the URL (always reliable)
    GET  /health                  liveness probe

The handler answers 200 straight away and hands the event to ``on_event``,
so slow Discord API calls never make IPS time out and retry.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

EventCallback = Callable[[str, Any], None]

_REDACTED_HEADERS = {"authorization", "cookie", "x-api-key"}
_EVENT_HEADER_HINTS = ("event",)
_EVENT_BODY_KEYS = ("event", "eventKey", "event_key", "webhook", "type")


# Member personal data IPS includes in author objects; never written to captures
_REDACTED_BODY_KEYS = {"email", "registrationipaddress", "ipaddress", "ip_address", "birthday"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.lower() in _REDACTED_BODY_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def detect_event(headers: dict[str, str], body: Any) -> str | None:
    for name, value in headers.items():
        lname = name.lower()
        if lname.startswith("x-") and any(h in lname for h in _EVENT_HEADER_HINTS) and value:
            return value
    if isinstance(body, dict):
        for key in _EVENT_BODY_KEYS:
            if isinstance(body.get(key), str) and body[key]:
                return body[key]
    return None


class WebhookServer:
    def __init__(
        self,
        secret: str,
        on_event: EventCallback,
        capture_dir: Path | None = None,
        capture_keep: int = 50,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("WEBHOOK_SECRET must be at least 16 characters")
        self._secret = secret.encode()
        self._on_event = on_event
        self._capture_dir = capture_dir
        self._capture_keep = capture_keep
        self._runner: web.AppRunner | None = None
        self.app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._log_requests])
        app.router.add_get("/health", self._health)
        # Accept an optional trailing slash; IPS (or a proxy) may add one
        for path in ("/ips/{secret}", "/ips/{secret}/", "/ips/{secret}/{event}", "/ips/{secret}/{event}/"):
            app.router.add_post(path, self._receive)
        return app

    def _mask(self, path: str) -> str:
        return path.replace(self._secret.decode(), "<secret>")

    @web.middleware
    async def _log_requests(self, request: web.Request, handler) -> web.StreamResponse:
        """Log every request (secret masked), including ones no route matches."""
        status = 500
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            if request.path != "/health":
                log.info(
                    "Webhook HTTP %s %s from %s -> %s (UA: %s)",
                    request.method,
                    self._mask(request.path_qs),
                    request.remote,
                    status,
                    request.headers.get("User-Agent", "-"),
                )

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        log.info("Webhook server listening on %s:%s", host, port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _receive(self, request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.match_info["secret"].encode(), self._secret):
            log.warning("Rejected webhook with invalid secret from %s", request.remote)
            raise web.HTTPNotFound()

        raw = await request.read()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            # IPS might send form-encoded data; keep it inspectable
            body = dict(await request.post()) if request.content_type.endswith("form-urlencoded") else None
        headers = {k: v for k, v in request.headers.items()}

        event = request.match_info.get("event") or detect_event(headers, body)
        self._capture(event, headers, body, raw)

        if body is None:
            log.warning("Webhook %s had an undecodable body (%d bytes)", event, len(raw))
            return web.json_response({"status": "ignored"}, status=400)
        if not event:
            log.warning("Webhook received without a recognisable event name; see captures")
            return web.json_response({"status": "ignored"})

        log.info("Webhook received: %s", event)
        try:
            self._on_event(event, body)
        except Exception:
            log.exception("Failed to dispatch webhook %s", event)
        return web.json_response({"status": "ok"})

    def _capture(self, event: str | None, headers: dict[str, str], body: Any, raw: bytes) -> None:
        if not self._capture_dir:
            return
        try:
            self._capture_dir.mkdir(parents=True, exist_ok=True)
            safe_headers = {k: ("<redacted>" if k.lower() in _REDACTED_HEADERS else v) for k, v in headers.items()}
            record = {
                "event": event,
                "headers": safe_headers,
                "body": redact(body) if body is not None else "<undecodable body omitted>",
            }
            name = f"{int(time.time() * 1000)}-{event or 'unknown'}.json"
            name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
            (self._capture_dir / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
            captures = sorted(self._capture_dir.glob("*.json"))
            for old in captures[: max(0, len(captures) - self._capture_keep)]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("Failed to write webhook capture")
