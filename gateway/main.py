from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import load_config
from .router import AllProvidersFailed, Router, UnknownModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.getenv("CONFIG_PATH", "config.yaml"))
    app.state.cfg = cfg
    app.state.router = Router(cfg)
    yield
    await app.state.router.aclose()


app = FastAPI(title="model-gateway", lifespan=lifespan)


def check_auth(request: Request) -> None:
    """If GATEWAY_API_KEY is set, require it as a Bearer token."""
    expected = os.getenv("GATEWAY_API_KEY")
    if not expected:
        return
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="invalid gateway API key")


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, **extra}}
    )


async def proxy(request: Request, path: str):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _err(400, "request body must be a JSON object")

    try:
        resp, route, attempts = await app.state.router.forward(path, payload)
    except UnknownModel as e:
        return _err(404, f"unknown model '{e}'. See GET /v1/models")
    except AllProvidersFailed as e:
        return _err(502, "all providers failed", attempts=e.attempts)

    headers = {
        "X-Gateway-Provider": route.provider,
        "X-Gateway-Model": route.model,
        "X-Gateway-Fallbacks": str(len(attempts)),
    }

    if payload.get("stream") and resp.status_code < 400:

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            gen(),
            status_code=resp.status_code,
            media_type="text/event-stream",
            headers=headers,
        )

    body = await resp.aread()
    await resp.aclose()
    return Response(
        content=body,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=headers,
    )


@app.post("/v1/chat/completions", dependencies=[Depends(check_auth)])
async def chat_completions(request: Request):
    return await proxy(request, "/chat/completions")


@app.post("/v1/embeddings", dependencies=[Depends(check_auth)])
async def embeddings(request: Request):
    return await proxy(request, "/embeddings")


@app.get("/v1/models", dependencies=[Depends(check_auth)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "owned_by": "model-gateway"}
            for alias in app.state.cfg.models
        ],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "providers": list(app.state.cfg.providers),
        "models": list(app.state.cfg.models),
        "cooling_down": app.state.router.cooldowns(),
    }
