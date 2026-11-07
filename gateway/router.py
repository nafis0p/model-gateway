from __future__ import annotations

import logging
import time

import httpx

from .config import Config, Route

log = logging.getLogger("gateway.router")


class UnknownModel(Exception):
    pass


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all providers failed")
        self.attempts = attempts


class Router:
    """Tries each route for a model alias in order until one succeeds."""

    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout, connect=10.0)
        )
        # (provider, model) -> monotonic time until which the route is skipped
        self._cooldown: dict[tuple[str, str], float] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    def _cooling(self, route: Route) -> bool:
        return self._cooldown.get((route.provider, route.model), 0.0) > time.monotonic()

    def _should_fallback(self, status: int) -> bool:
        return status >= 500 or status in self.cfg.fallback_statuses

    def _start_cooldown(self, route: Route, resp: httpx.Response) -> None:
        try:
            secs = float(resp.headers.get("retry-after", ""))
        except ValueError:
            secs = self.cfg.cooldown_seconds
        secs = max(0.0, min(secs, 300.0))
        self._cooldown[(route.provider, route.model)] = time.monotonic() + secs

    def cooldowns(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            f"{p}/{m}": round(until - now, 1)
            for (p, m), until in self._cooldown.items()
            if until > now
        }

    async def forward(
        self, path: str, payload: dict
    ) -> tuple[httpx.Response, Route, list[dict]]:
        """POST payload to `path` on the first healthy provider.

        Returns an OPEN streaming httpx.Response (caller must read/close it),
        the route that served it, and the list of failed attempts before it.
        A non-retryable error (e.g. 400) is returned as-is so the client sees it.
        """
        alias = payload.get("model")
        routes = self.cfg.models.get(alias)
        if not routes:
            raise UnknownModel(str(alias))

        # Routes in a 429 cooldown go last instead of being dropped entirely.
        ordered = [r for r in routes if not self._cooling(r)] + [
            r for r in routes if self._cooling(r)
        ]

        attempts: list[dict] = []
        for route in ordered:
            prov = self.cfg.providers[route.provider]
            url = f"{prov.base_url}{path}"
            headers = {
                "Authorization": f"Bearer {prov.api_key}",
                "Content-Type": "application/json",
                **prov.headers,
            }
            body = {**payload, "model": route.model}

            try:
                req = self.client.build_request("POST", url, json=body, headers=headers)
                resp = await self.client.send(req, stream=True)
            except httpx.HTTPError as e:
                log.warning("%s/%s network error: %r", route.provider, route.model, e)
                attempts.append(
                    {"provider": route.provider, "model": route.model, "error": repr(e)}
                )
                continue

            if resp.status_code < 400:
                return resp, route, attempts

            if not self._should_fallback(resp.status_code):
                return resp, route, attempts

            detail = (await resp.aread())[:300].decode("utf-8", "replace")
            await resp.aclose()
            if resp.status_code == 429:
                self._start_cooldown(route, resp)
            log.warning(
                "%s/%s -> %s, falling back", route.provider, route.model, resp.status_code
            )
            attempts.append(
                {
                    "provider": route.provider,
                    "model": route.model,
                    "status": resp.status_code,
                    "detail": detail,
                }
            )

        raise AllProvidersFailed(attempts)
