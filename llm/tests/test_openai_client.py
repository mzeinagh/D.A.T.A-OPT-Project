"""Unit tests for OpenAIResponsesClient's retry/timeout/error-classification
logic, exercised against the real `openai` SDK's exception classes (built
with real `httpx.Request`/`httpx.Response` objects — no live network call).

Why mocked rather than live: `api.openai.com` is blocked by this
environment's egress proxy (confirmed via a direct `curl` — see the PR/
migration-plan notes), so a genuine end-to-end call against OpenAI cannot be
made from here. These tests instead verify that *our* code — the
retryable/non-retryable classification, the bounded backoff loop, the
"incomplete" response-status handling, and the on_call hook — behaves
correctly against the SDK's real types, which is everything this module
controls. The actual live call and the manual output-parity check against
the legacy Ollama pipeline (per the migration plan's Phase 1 requirement)
still needs to be run by someone with a real OPENAI_API_KEY and network
access, via `manage.py run_pipeline`.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from llm.openai_client import OpenAIResponsesClient
from llm.pricing import estimate_cost_usd

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _http_error(cls, status_code, message="error"):
    response = httpx.Response(status_code=status_code, request=_REQUEST)
    return cls(message, response=response, body=None)


def _fake_response(*, status="completed", output_text="hello", input_tokens=10, output_tokens=5, resp_id="resp_123", incomplete_reason=None, error=None):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    incomplete_details = SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
    return SimpleNamespace(
        id=resp_id,
        status=status,
        output_text=output_text,
        usage=usage,
        incomplete_details=incomplete_details,
        error=error,
    )


def _client(**overrides):
    kwargs = dict(api_key="sk-test", model="gpt-5", timeout_seconds=5.0, max_retries=2, backoff_base_seconds=0.0)
    kwargs.update(overrides)
    return OpenAIResponsesClient(**kwargs)


class TestConstruction:
    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            OpenAIResponsesClient(api_key="", model="gpt-5")

    def test_requires_model(self):
        with pytest.raises(ValueError):
            OpenAIResponsesClient(api_key="sk-test", model="")


class TestSuccess:
    def test_completed_status_is_success(self):
        client = _client()
        client._client.responses.create = MagicMock(return_value=_fake_response())

        result = client.invoke("hi", node_name="generate")

        assert result.ok is True
        assert result.status == "success"
        assert result.text == "hello"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.retry_count == 0
        assert result.raw_response_id == "resp_123"
        # gpt-5 is priced in llm/pricing.py: (10/1e6)*1.25 + (5/1e6)*10.00
        assert result.estimated_cost_usd == pytest.approx(estimate_cost_usd("gpt-5", 10, 5))
        assert result.estimated_cost_usd is not None

    def test_none_status_treated_as_success(self):
        # Some response shapes don't populate `status` at all.
        client = _client()
        client._client.responses.create = MagicMock(return_value=_fake_response(status=None))

        result = client.invoke("hi")

        assert result.ok is True

    def test_max_output_tokens_forwarded(self):
        client = _client(max_output_tokens=111)
        mock_create = MagicMock(return_value=_fake_response())
        client._client.responses.create = mock_create

        client.invoke("hi")
        assert mock_create.call_args.kwargs["max_output_tokens"] == 111

        client.invoke("hi", max_output_tokens=222)
        assert mock_create.call_args.kwargs["max_output_tokens"] == 222


class TestIncomplete:
    def test_incomplete_is_still_ok_with_metadata(self):
        client = _client()
        client._client.responses.create = MagicMock(
            return_value=_fake_response(status="incomplete", incomplete_reason="max_output_tokens")
        )

        result = client.invoke("hi")

        assert result.ok is True
        assert result.text == "hello"
        assert result.metadata["incomplete"] is True
        assert result.metadata["incomplete_reason"] == "max_output_tokens"

    def test_unexpected_status_is_failure(self):
        client = _client()
        client._client.responses.create = MagicMock(
            return_value=_fake_response(status="cancelled", error="something went wrong")
        )

        result = client.invoke("hi")

        assert result.ok is False
        assert result.status == "failed"
        assert "cancelled" in result.error_message


class TestNonRetryableFailures:
    def test_authentication_error_fails_immediately(self):
        client = _client()
        client._client.responses.create = MagicMock(side_effect=_http_error(AuthenticationError, 401))

        result = client.invoke("hi")

        assert result.status == "failed"
        assert result.retry_count == 0
        client._client.responses.create.assert_called_once()

    def test_bad_request_error_fails_immediately(self):
        client = _client()
        client._client.responses.create = MagicMock(side_effect=_http_error(BadRequestError, 400))

        result = client.invoke("hi")

        assert result.status == "failed"
        assert result.retry_count == 0
        client._client.responses.create.assert_called_once()

    def test_permission_denied_403_not_retried(self):
        client = _client()
        client._client.responses.create = MagicMock(side_effect=_http_error(PermissionDeniedError, 403))

        result = client.invoke("hi")

        assert result.status == "failed"
        client._client.responses.create.assert_called_once()


class TestRetryableFailures:
    def test_rate_limit_exhausts_retries_then_fails(self):
        client = _client(max_retries=2)
        err = _http_error(RateLimitError, 429)
        client._client.responses.create = MagicMock(side_effect=err)

        with patch("time.sleep") as mock_sleep:
            result = client.invoke("hi")

        assert result.status == "failed"
        assert result.retry_count == 2
        assert client._client.responses.create.call_count == 3  # 1 initial + 2 retries
        assert mock_sleep.call_count == 2

    def test_timeout_exhausts_retries_then_reports_timeout_status(self):
        client = _client(max_retries=1)
        client._client.responses.create = MagicMock(side_effect=APITimeoutError(request=_REQUEST))

        with patch("time.sleep"):
            result = client.invoke("hi")

        assert result.status == "timeout"
        assert result.retry_count == 1
        assert client._client.responses.create.call_count == 2

    def test_connection_error_retries_then_succeeds(self):
        client = _client(max_retries=3)
        client._client.responses.create = MagicMock(
            side_effect=[APIConnectionError(request=_REQUEST), _fake_response()]
        )

        with patch("time.sleep") as mock_sleep:
            result = client.invoke("hi")

        assert result.ok is True
        assert result.retry_count == 1
        assert mock_sleep.call_count == 1

    def test_server_5xx_retried_then_succeeds(self):
        client = _client(max_retries=3)
        client._client.responses.create = MagicMock(
            side_effect=[_http_error(InternalServerError, 500), _fake_response()]
        )

        with patch("time.sleep"):
            result = client.invoke("hi")

        assert result.ok is True
        assert result.retry_count == 1

    def test_backoff_is_exponential(self):
        client = _client(max_retries=3, backoff_base_seconds=1.0)
        client._client.responses.create = MagicMock(side_effect=_http_error(RateLimitError, 429))

        with patch("time.sleep") as mock_sleep:
            client.invoke("hi")

        # attempt 0 -> sleep(1 * 2**0)=1, attempt 1 -> sleep(1*2**1)=2, attempt 2 -> sleep(1*2**2)=4
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(4.0)

    def test_zero_max_retries_never_retries(self):
        client = _client(max_retries=0)
        client._client.responses.create = MagicMock(side_effect=_http_error(RateLimitError, 429))

        with patch("time.sleep") as mock_sleep:
            result = client.invoke("hi")

        assert result.status == "failed"
        assert result.retry_count == 0
        client._client.responses.create.assert_called_once()
        mock_sleep.assert_not_called()


class TestOnCallHook:
    def test_on_call_invoked_with_node_name_and_result(self):
        seen = []
        client = _client(on_call=lambda node_name, result: seen.append((node_name, result)))
        client._client.responses.create = MagicMock(return_value=_fake_response())

        client.invoke("hi", node_name="formatter")

        assert len(seen) == 1
        node_name, result = seen[0]
        assert node_name == "formatter"
        assert result.ok is True

    def test_on_call_invoked_on_failure_too(self):
        seen = []
        client = _client(on_call=lambda node_name, result: seen.append((node_name, result)), max_retries=0)
        client._client.responses.create = MagicMock(side_effect=_http_error(AuthenticationError, 401))

        client.invoke("hi", node_name="retrieve_guide")

        assert len(seen) == 1
        assert seen[0][1].ok is False

    def test_no_on_call_is_fine(self):
        client = _client(on_call=None)
        client._client.responses.create = MagicMock(return_value=_fake_response())
        # Should not raise.
        client.invoke("hi")


class TestPricing:
    def test_known_model(self):
        cost = estimate_cost_usd("gpt-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(1.25 + 10.00)

    def test_unknown_model_returns_none_not_zero(self):
        assert estimate_cost_usd("some-future-model", 100, 100) is None
