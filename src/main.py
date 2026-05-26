from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from src.models import ErrorResponse, ReplayRequest, RunSummary, VerifyRequest, VerifyResponse
from src.service import ServiceError, VerificationService

app = FastAPI(
    title="LarkGuard",
    description="Evidence-first bug verification service",
    version="0.1.0",
)
service = VerificationService()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/verify", response_model=VerifyResponse)
async def verify_issue(request: VerifyRequest) -> VerifyResponse:
    try:
        return await service.verify(request)
    except ServiceError as exc:
        raise _http_error(exc) from exc


@app.post("/replay", response_model=VerifyResponse)
async def replay_run(request: ReplayRequest) -> VerifyResponse:
    try:
        return await service.replay(request.run_id)
    except ServiceError as exc:
        raise _http_error(exc) from exc


@app.get("/runs", response_model=list[RunSummary])
async def list_runs(limit: int = Query(default=20, ge=1, le=100)) -> list[RunSummary]:
    return service.list_runs(limit=limit)


@app.exception_handler(ServiceError)
async def service_error_handler(_request, exc: ServiceError) -> JSONResponse:
    payload = ErrorResponse(
        detail=str(exc),
        error_type=exc.error_type,
        context=exc.context,
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


def _http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "detail": str(exc),
            "error_type": exc.error_type,
            "context": exc.context,
        },
    )
