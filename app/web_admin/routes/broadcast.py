"""Mass Broadcast launcher and job progress history routes."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.constants import SUPPORTED_LANGUAGES
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.dtos import BroadcastCreateDTO
from app.services.interfaces import BroadcastServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    get_broadcast_service,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
)

logger = get_logger(__name__)
router = APIRouter(tags=["admin-broadcast"])


def _validate_target_filter(target_filter: str) -> str:
    cleaned = target_filter.strip().lower()
    if cleaned in {"all", "has_balance", "admins"}:
        return cleaned
    if cleaned.startswith("lang:"):
        code = cleaned.split(":", 1)[1].strip()
        if code in SUPPORTED_LANGUAGES:
            return f"lang:{code}"
    if cleaned.startswith("user:"):
        uid_part = cleaned.split(":", 1)[1].strip()
        if uid_part.isdigit() and int(uid_part) > 0:
            return f"user:{int(uid_part)}"
    raise ValidationError(
        "target_filter must be 'all', 'has_balance', 'admins', 'lang:<code>', or 'user:<telegram_id>'"
    )


@router.get("/admin/broadcast")
@router.get("/admin/api/broadcast")
async def broadcast_overview(
    request: Request,
    limit: int = 30,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    broadcast_service: BroadcastServiceProtocol = Depends(get_broadcast_service),
) -> Response:
    """Render Mass Broadcast wizard and recent job execution table."""
    jobs = await broadcast_service.list_jobs(
        limit=max(1, min(limit, 100)),
        offset=max(0, offset),
    )

    if prefers_json(request):
        return JSONResponse({"jobs": [j.model_dump(mode="json") for j in jobs]})

    return render_admin_template(
        request,
        "broadcast.html",
        {
            "active_nav": "broadcast",
            "jobs": jobs,
        },
        admin=admin,
    )


@router.post("/admin/broadcast")
async def create_broadcast_job_route(
    request: Request,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    broadcast_service: BroadcastServiceProtocol = Depends(get_broadcast_service),
) -> Response:
    """Create and launch a rate-limited mass broadcast job."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    target_filter = _validate_target_filter(str(payload.get("target_filter") or "all"))

    if isinstance(payload.get("message_payload"), dict):
        msg_payload: dict[str, Any] = dict(payload["message_payload"])
        if not str(msg_payload.get("text") or "").strip() and not str(msg_payload.get("media_file_id") or "").strip():
            raise ValidationError("Broadcast message text or media_file_id is required")
        btn_url = str(msg_payload.get("button_url") or "").strip()
        if btn_url and not btn_url.startswith(("https://", "http://")):
            raise ValidationError("Inline button URL must start with https:// or http://")
    else:
        text = str(payload.get("message_text") or payload.get("text") or "").strip()
        button_text = str(payload.get("button_text") or "").strip()
        button_url = str(payload.get("button_url") or "").strip()
        media_file_id = str(payload.get("media_file_id") or "").strip()
        media_type = str(payload.get("media_type") or "").strip().lower()

        if not text and not media_file_id:
            raise ValidationError("Broadcast message text or media_file_id is required")
        if button_url and not button_url.startswith(("https://", "http://")):
            raise ValidationError("Inline button URL must start with https:// or http://")
        if button_text and not button_url:
            raise ValidationError("button_url is required when button_text is set")

        msg_payload = {"text": text}
        if button_text and button_url:
            msg_payload["button_text"] = button_text
            msg_payload["button_url"] = button_url
        if media_file_id:
            msg_payload["media_file_id"] = media_file_id
            msg_payload["media_type"] = media_type or "photo"

    job = await broadcast_service.create_job(
        BroadcastCreateDTO(
            admin_id=admin.admin_id,
            target_filter=target_filter,
            message_payload=msg_payload,
        )
    )

    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        for runner_name in ("execute_job", "run_job", "dispatch_job", "run_broadcast", "process_job", "start_job"):
            runner = getattr(broadcast_service, runner_name, None)
            if callable(runner):
                sig = inspect.signature(runner)
                call_kwargs: dict[str, Any] = {}
                if "bot" in sig.parameters:
                    call_kwargs["bot"] = bot
                coro = runner(job.id, **call_kwargs)
                if inspect.isawaitable(coro):
                    task = asyncio.create_task(coro, name=f"broadcast_job_{job.id}")
                    task.add_done_callback(
                        lambda t: logger.warning("Broadcast task failed: %s", t.exception())
                        if not t.cancelled() and t.exception()
                        else None
                    )
                break

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "job": job.model_dump(mode="json")}, status_code=201)

    q = urlencode({"msg": f"Broadcast job #{job.id} queued ({job.target_filter})"})
    return RedirectResponse(url=f"/admin/broadcast?{q}", status_code=303)


@router.get("/admin/broadcast/{job_id}")
@router.get("/admin/api/broadcast/{job_id}")
async def get_broadcast_job_status(
    job_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    broadcast_service: BroadcastServiceProtocol = Depends(get_broadcast_service),
) -> JSONResponse:
    """Return live progress metrics for a specific BroadcastJob."""
    job = await broadcast_service.get_job(job_id)
    return JSONResponse({"job": job.model_dump(mode="json")})
