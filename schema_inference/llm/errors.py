"""Normalized LLM exceptions.

Each provider adapter maps its native SDK exceptions onto these at the call
boundary (see providers/anthropic_provider.py, providers/openai_provider.py),
so callers — throttle.py's rate-limit retry loop, agent code — never need to
import a specific provider's SDK to catch its errors. This is what lets
throttle.py's pacer logic stay unchanged while only the exception type it
catches becomes provider-neutral (`except anthropic.RateLimitError` ->
`except LLMRateLimitError`).
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all normalized LLM errors."""


class LLMRateLimitError(LLMError):
    """The provider rejected the request due to rate limiting (HTTP 429)."""


class LLMAuthError(LLMError):
    """The provider rejected the request due to invalid/missing credentials."""


class LLMAPIError(LLMError):
    """Any other provider-side API error (5xx, malformed request, timeouts, ...)."""
