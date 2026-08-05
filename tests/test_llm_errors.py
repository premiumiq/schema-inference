from schema_inference.llm.errors import LLMAPIError, LLMAuthError, LLMError, LLMRateLimitError


def test_all_normalized_errors_subclass_llmerror():
    assert issubclass(LLMRateLimitError, LLMError)
    assert issubclass(LLMAuthError, LLMError)
    assert issubclass(LLMAPIError, LLMError)


def test_llmerror_is_a_plain_exception():
    assert issubclass(LLMError, Exception)
    # Catching the base class must catch every normalized subtype -- this is
    # what lets throttle.py's `except LLMRateLimitError` stay narrow while
    # other call sites can catch LLMError broadly if they want to.
    for cls in (LLMRateLimitError, LLMAuthError, LLMAPIError):
        try:
            raise cls("boom")
        except LLMError as e:
            assert str(e) == "boom"
