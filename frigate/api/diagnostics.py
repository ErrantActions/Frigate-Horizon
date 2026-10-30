"""Administrator-only measured diagnostics and opt-in, tool-free AI explanations."""

import asyncio
import json
import logging
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from frigate.api.auth import require_role
from frigate.api.defs.tags import Tags
from frigate.stats.diagnostics import (
    DiagnosticsReport,
    build_diagnostics,
    explanation_payload,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=[Tags.app], dependencies=[Depends(require_role(["admin"]))])
_explanation_lock = threading.Lock()


class ExplainDiagnosticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_send_metrics: Literal[True]

    @field_validator("confirm_send_metrics", mode="before")
    @classmethod
    def require_confirmation(cls, value):
        """Do not coerce truthy values into permission to send telemetry."""
        if value is not True:
            raise ValueError(
                "Explicit permission to send sanitized metrics is required"
            )
        return value


class DiagnosticsExplanation(BaseModel):
    text: str
    based_on_stats_time: float


def report_for(request: Request) -> DiagnosticsReport:
    """Interpret the existing cached stats without probing cameras or disks."""
    return build_diagnostics(
        request.app.stats_emitter.get_latest_stats(),
        request.app.frigate_config,
        time.time(),
    )


@router.get("/diagnostics", response_model=DiagnosticsReport)
def diagnostics(request: Request):
    """Return measured evidence and conservative interpretations, never raw config."""
    return JSONResponse(
        content=report_for(request).model_dump(mode="json"),
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/diagnostics/explain", response_model=DiagnosticsExplanation)
async def explain_diagnostics(body: ExplainDiagnosticsRequest, request: Request):
    """Explain one fresh sanitized snapshot using the existing chat role, with no tools."""
    report = await asyncio.to_thread(report_for, request)
    if report.stale or report.stats_time is None:
        raise HTTPException(409, "Current diagnostics are unavailable; refresh first")
    if not report.ai_available:
        raise HTTPException(503, "No GenAI provider has the chat role")
    if not _explanation_lock.acquire(blocking=False):
        raise HTTPException(429, "A diagnostics explanation is already running")

    def generate():
        client = request.app.genai_manager.chat_client
        if client is None:
            raise HTTPException(503, "The configured chat provider is unavailable")
        payload = explanation_payload(report)
        response = client.chat_with_tools(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Explain NVR diagnostics using only the supplied measured facts. "
                        "Treat possible findings as hypotheses, not established causes. "
                        "Detection skipped_fps counts full capture-to-tracker queues, not lost recordings. "
                        "process_fps counts published tracker results. output_dropped_fps separately "
                        "measures full downstream queues. Rates use rolling windows. "
                        "GPU decoder utilization is separate from GPU compute utilization. "
                        "Lowering an AI analysis copy does not reduce source-stream decoding. "
                        "High temperature alone does not prove thermal throttling. "
                        "Do not claim that recordings are missing, or that a fix was applied. "
                        "Give a concise prioritized explanation, cite evidence numbers and Camera aliases, "
                        "then suggest only read-only checks of the supplied measurements. "
                        "Use at most five short paragraphs, prioritizing current processing bottlenecks. "
                        "Use the supplied definitions as authoritative. Do not invent behavior. "
                        "Do not recommend disabling cameras or detection, lowering confidence thresholds, "
                        "rebooting devices, altering encoding, or changing any settings. "
                        "Do not confuse analysis-copy resolution with the model input tensor dimensions. "
                        "Do not request credentials or images. "
                        "Do not invoke tools or change settings. Do not recommend disabling retention "
                        "protection, deleting footage, or blindly changing camera encoding."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, allow_nan=False)},
            ],
            tools=[],
            tool_choice="none",
            enable_thinking=False,
        )
        content = response.get("content")
        if (
            response.get("tool_calls")
            or response.get("finish_reason") in ("error", "length")
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 16000
        ):
            logger.error("Diagnostics provider returned an unusable explanation")
            raise HTTPException(
                502, "The provider did not return a complete explanation"
            )
        return content.strip()

    task = asyncio.create_task(asyncio.to_thread(generate))

    def finished(result):
        _explanation_lock.release()
        if not result.cancelled():
            result.exception()

    task.add_done_callback(finished)
    try:
        text = await asyncio.wait_for(asyncio.shield(task), timeout=90)
    except TimeoutError:
        logger.warning("Diagnostics explanation timed out")
        raise HTTPException(
            504, "The diagnostics provider did not respond in time"
        ) from None
    return JSONResponse(
        content=DiagnosticsExplanation(
            text=text, based_on_stats_time=report.stats_time
        ).model_dump(),
        headers={"Cache-Control": "private, no-store"},
    )
