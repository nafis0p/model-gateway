from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("gateway.config")

# Client-ish errors that usually mean "this provider can't serve it right now".
# Any 5xx is always treated as retryable too. 400/422 are NOT retried by default.
DEFAULT_FALLBACK_STATUSES = [401, 402, 403, 404, 408, 409, 425, 429]


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Route:
    provider: str
    model: str  # the provider's own model id


@dataclass
class Config:
    providers: dict[str, Provider]
    models: dict[str, list[Route]]  # alias -> ordered routes
    timeout: float = 60.0
    cooldown_seconds: float = 30.0
    fallback_statuses: set[int] = field(
        default_factory=lambda: set(DEFAULT_FALLBACK_STATUSES)
    )


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    providers: dict[str, Provider] = {}
    for name, p in (raw.get("providers") or {}).items():
        key = p.get("api_key") or os.getenv(p.get("api_key_env", ""), "")
        if not key:
            log.warning(
                "provider '%s' skipped: no API key (set env %s)",
                name,
                p.get("api_key_env"),
            )
            continue
        providers[name] = Provider(
            name=name,
            base_url=p["base_url"].rstrip("/"),
            api_key=key,
            headers=p.get("headers") or {},
        )

    models: dict[str, list[Route]] = {}
    for alias, m in (raw.get("models") or {}).items():
        routes: list[Route] = []
        for r in m.get("routes", []):
            if r["provider"] not in providers:
                log.warning(
                    "model '%s': route to '%s' dropped (provider unavailable)",
                    alias,
                    r["provider"],
                )
                continue
            routes.append(Route(provider=r["provider"], model=r["model"]))
        if routes:
            models[alias] = routes
        else:
            log.warning("model '%s' has no usable routes; skipped", alias)

    s = raw.get("settings") or {}
    cfg = Config(providers=providers, models=models)
    if "timeout_seconds" in s:
        cfg.timeout = float(s["timeout_seconds"])
    if "cooldown_seconds" in s:
        cfg.cooldown_seconds = float(s["cooldown_seconds"])
    if "fallback_statuses" in s:
        cfg.fallback_statuses = {int(x) for x in s["fallback_statuses"]}
    return cfg
