"""The webhook receiver. It only verifies, enqueues and returns 202: GitHub gives a webhook ~10 seconds,
and a review takes longer, so all slow work happens in the worker."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from reviewer.api.events import to_review_job
from reviewer.api.security import verify_signature
from reviewer.config import Settings, get_settings
from reviewer.logging_setup import setup_logging
from reviewer.queue.redis_queue import JobQueue, create_redis

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, queue: JobQueue | None = None) -> FastAPI:
    """Both arguments exist so tests can inject a fake queue and settings."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        redis: Redis | None = None
        if queue is None:
            setup_logging()
            redis = create_redis(app.state.settings.redis_url)
            app.state.queue = JobQueue(redis)
        yield
        if redis is not None:
            await redis.aclose()

    app = FastAPI(title="AI Code Reviewer", description="GitHub webhook receiver", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.state.queue = queue

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        delivery_id = request.headers.get("x-github-delivery", "")
        signature = request.headers.get("x-hub-signature-256")
        if not verify_signature(raw, signature, request.app.state.settings.github_webhook_secret):
            log.warning(
                "webhook rejected: invalid signature (secret mismatch?)",
                extra={"delivery_id": delivery_id, "has_signature": bool(signature)},
            )
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as err:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from err

        event = request.headers.get("x-github-event")
        log.info(
            "webhook received",
            extra={"delivery_id": delivery_id, "event": event, "action": payload.get("action")},
        )

        queued = to_review_job(event, payload, delivery_id)
        if queued is None:
            return JSONResponse({"status": "ignored"}, status_code=202)

        created = await request.app.state.queue.enqueue(queued.job_id, queued.job.model_dump(mode="json"))
        log.info(
            "review queued" if created else "duplicate delivery ignored",
            extra={
                "delivery_id": delivery_id,
                "trigger": queued.job.trigger,
                "repo": queued.job.repo,
                "pr": queued.job.pr_number,
            },
        )
        return JSONResponse({"status": "queued" if created else "duplicate"}, status_code=202)

    return app
