"""Provider-neutral LLM abstraction (MAP-8).

The mapping pipeline's agents (MappingAgent, CriticAgent, SQLAgent) and
tools/tune_prompts.py talk to this package instead of importing the
`anthropic` SDK directly. `get_provider()` (registry.py) resolves which
backend to use from `agent_config.yml`'s `llm:` section — Claude/Anthropic
is the default, and the same call sites work unchanged against an
OpenAI-compatible backend (OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio,
...) by pointing `llm.provider` at `openai` and setting `base_url`.

See docs/llm-provider-abstraction-and-tool-tuning-plan.md for the design
rationale (MAP-8).
"""

from .errors import LLMAPIError, LLMAuthError, LLMError, LLMRateLimitError
from .provider import LLMProvider
from .registry import get_provider, model_for
from .types import LLMMessage, LLMResponse, LLMToolCall, LLMToolDef

__all__ = [
    "LLMAPIError",
    "LLMAuthError",
    "LLMError",
    "LLMRateLimitError",
    "LLMProvider",
    "LLMMessage",
    "LLMResponse",
    "LLMToolCall",
    "LLMToolDef",
    "get_provider",
    "model_for",
]
