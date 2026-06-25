"""Process-wide rate limiter + retry for live Anthropic API calls.

Why: the org-level rate limit (free/build tiers can be as low as 5 req/min,
enforced server-side per account — shared across every agent in this
pipeline, not per script) is a hard external constraint. A purely reactive
retry-after-429 approach burns through that already-thin budget on failed
attempts and converges slowly whenever concurrency exceeds the limit — e.g.
MappingAgent's default 10-way concurrency fires ~10 requests near-
simultaneously, instantly overshooting a 5 RPM cap regardless of how good
the per-call backoff is (every column's first attempt 429s together).

This module adds a proactive process-wide pacer — minimum spacing between
ANY two outbound calls, shared across mapping/critic/sql/tuner — so callers
naturally serialize instead of bursting, plus jittered exponential-backoff
retry as the correctness backstop for races and clock skew.

Configure via agent_config.yml's rate_limit.requests_per_minute (default 5,
matching the lowest published tier). Raise it there if your org's limit is
higher — see docs/self-tuning-mapper-agent-plan.md and the rate-limit error
message for how to request an increase from Anthropic.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time

_lock = threading.Lock()
_last_call_at = 0.0

DEFAULT_RPM = 5
MAX_RETRIES = 6
INITIAL_DELAY = 15.0
MAX_DELAY = 90.0
JITTER_FRACTION = 0.3

# Test-only escape hatch — unit tests inject a fake client that never makes a
# real network call, so the pacer's wall-clock sleep just wastes test time.
# Real runs must never set this.
_DISABLE_ENV_VAR = "SCHEMA_INFERENCE_DISABLE_THROTTLE"


def _requests_per_minute() -> float:
    try:
        from .orchestrator import load_agent_config
        rpm = load_agent_config().get("rate_limit", {}).get("requests_per_minute", DEFAULT_RPM)
    except Exception:
        rpm = DEFAULT_RPM
    return max(float(rpm), 1.0)


def _min_interval_seconds() -> float:
    return 60.0 / _requests_per_minute()


def throttle() -> None:
    """Block (synchronously) until it's safe to make another call, process-wide."""
    if os.environ.get(_DISABLE_ENV_VAR):
        return
    global _last_call_at
    with _lock:
        wait = _min_interval_seconds() - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


async def athrottle() -> None:
    """Async variant for mapping_agent.py's concurrent column loop — runs the
    blocking pacer in a thread so other coroutines aren't frozen by the sleep."""
    await asyncio.to_thread(throttle)


def _backoff_delay(attempt: int, delay: float) -> tuple[float, float]:
    """Returns (sleep_seconds_with_jitter, next_delay). Jitter spreads out
    retries that would otherwise all wake up at the same instant (the exact
    thundering-herd pattern that caused the original bug)."""
    jitter = random.uniform(0, delay * JITTER_FRACTION)
    next_delay = min(delay * 1.5, MAX_DELAY)
    return delay + jitter, next_delay


def call_with_retry(client, kwargs: dict, max_retries: int = MAX_RETRIES, initial_delay: float = INITIAL_DELAY):
    """Synchronous: throttle, call, retry with jittered backoff on 429.
    Use for any single-shot (non-asyncio) client.messages.create call —
    critic_agent.py, sql_agent.py, tools/tune_prompts.py."""
    import anthropic
    delay = initial_delay
    for attempt in range(max_retries):
        throttle()
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            sleep_for, delay = _backoff_delay(attempt, delay)
            time.sleep(sleep_for)


async def acall_with_retry(client, kwargs: dict, max_retries: int = MAX_RETRIES, initial_delay: float = INITIAL_DELAY):
    """Async: throttle, call (in a thread), retry with jittered backoff on
    429. Use inside asyncio code — mapping_agent.py's per-column loop."""
    import anthropic
    delay = initial_delay
    for attempt in range(max_retries):
        await athrottle()
        try:
            return await asyncio.to_thread(client.messages.create, **kwargs)
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            sleep_for, delay = _backoff_delay(attempt, delay)
            await asyncio.sleep(sleep_for)
