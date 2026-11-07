# model-gateway

A small OpenAI-compatible gateway. Point any OpenAI client at it; it sends each
request to the first provider in your list and **automatically falls back** to
the next one on 429s, timeouts, or provider errors.

```
client -> gateway -> Gemini  (429) -> OpenRouter (error) -> Groq -> ...
```

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # edit providers / model routes
cp .env.example .env                    # add your API keys
uvicorn gateway.main:app --env-file .env --port 8000
```

Use it like OpenAI:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-flash","messages":[{"role":"user","content":"Hello"}]}'
```

Or in the OpenAI SDK: `base_url="http://localhost:8000/v1"`, `api_key=<GATEWAY_API_KEY>`.

Responses include `X-Gateway-Provider`, `X-Gateway-Model` and
`X-Gateway-Fallbacks` headers so you can see who answered.

## Config

- **providers**: any OpenAI-compatible `base_url` plus the env var holding its key.
  Providers without a key are skipped.
- **models**: client-facing aliases. Each has ordered `routes`; each route uses that
  provider's own model id (ids differ across providers, e.g. `gemini-2.0-flash` vs
  `google/gemini-2.0-flash-001`). Check each provider's current model list.
- **settings**: `timeout_seconds`, `cooldown_seconds`, `fallback_statuses`.

## Fallback rules

| Upstream result | Behavior |
|---|---|
| 2xx | Returned to client |
| 429 | Next provider; route is skipped for `Retry-After` (or `cooldown_seconds`) |
| 5xx, timeout, connection error | Next provider |
| 401/402/403/404/408/409/425 (configurable) | Next provider |
| 400/422 and other client errors | Returned as-is (your request is the problem) |
| Every route failed | `502` with a list of attempts |

## Endpoints

`POST /v1/chat/completions` (streaming supported), `POST /v1/embeddings`,
`GET /v1/models`, `GET /health`.

## Notes & limits

- Streaming can only fall back **before** the first byte. If a provider dies
  mid-stream, the stream ends.
- Cooldown state is in-memory (per process).
- Set `GATEWAY_API_KEY` if the gateway is reachable by anyone but you.

## Tests / Docker

```bash
pip install -r requirements-dev.txt && pytest
docker build -t model-gateway . && docker run -p 8000:8000 --env-file .env model-gateway
```
