import asyncio

import httpx
import pytest

from gateway.config import Config, Provider, Route
from gateway.router import AllProvidersFailed, Router


def make_router(handler):
    cfg = Config(
        providers={n: Provider(n, f"https://{n}.test/v1", "k") for n in ("a", "b", "c")},
        models={"m": [Route("a", "m-a"), Route("b", "m-b"), Route("c", "m-c")]},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Router(cfg, client)


PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def test_falls_back_on_429_then_500_and_cools_down():
    def handler(req):
        host = req.url.host
        if host == "a.test":
            return httpx.Response(429, headers={"retry-after": "5"}, json={})
        if host == "b.test":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"ok": True})

    async def run():
        r = make_router(handler)
        resp, route, attempts = await r.forward("/chat/completions", PAYLOAD)
        await resp.aclose()
        assert route.provider == "c"
        assert [a["status"] for a in attempts] == [429, 500]

        # 'a' is cooling down now, so it is skipped: b (500) -> c
        resp, route, attempts = await r.forward("/chat/completions", PAYLOAD)
        await resp.aclose()
        assert route.provider == "c"
        assert len(attempts) == 1
        await r.aclose()

    asyncio.run(run())


def test_all_providers_fail():
    async def run():
        r = make_router(lambda req: httpx.Response(429, json={}))
        with pytest.raises(AllProvidersFailed) as e:
            await r.forward("/chat/completions", PAYLOAD)
        assert len(e.value.attempts) == 3
        await r.aclose()

    asyncio.run(run())


def test_400_is_not_retried():
    async def run():
        r = make_router(lambda req: httpx.Response(400, json={"error": "bad"}))
        resp, route, attempts = await r.forward("/chat/completions", PAYLOAD)
        await resp.aclose()
        assert resp.status_code == 400
        assert route.provider == "a"
        assert attempts == []
        await r.aclose()

    asyncio.run(run())
