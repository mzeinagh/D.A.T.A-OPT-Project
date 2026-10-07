"""Tests for the two settings added for Azure OpenAI: OPENAI_BASE_URL (which
service the client talks to) and the OPENAI_PRICE_* override (cost
estimates for a deployment name llm/pricing.py doesn't know).

Both must leave today's OpenAI path unchanged when they're empty.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llm.factory import build_default_llm_client
from llm.openai_client import OpenAIResponsesClient

AZURE_URL = "https://example-resource.openai.azure.com/openai/v1/"


def _fake_response(input_tokens=1_000_000, output_tokens=1_000_000):
    return SimpleNamespace(
        id="resp_1",
        status="completed",
        output_text="ok",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        incomplete_details=None,
        error=None,
    )


@pytest.mark.parametrize("base_url, expected", [(None, None), ("", None), (AZURE_URL, AZURE_URL)])
def test_base_url_is_passed_to_the_sdk_only_when_set(base_url, expected):
    with patch("llm.openai_client.OpenAI") as sdk:
        OpenAIResponsesClient(api_key="k", model="m", base_url=base_url)
    assert sdk.call_args.kwargs["base_url"] == expected
    assert sdk.call_args.kwargs["max_retries"] == 0


def test_without_a_price_override_the_pricing_table_is_used():
    client = OpenAIResponsesClient(api_key="k", model="gpt-5")
    with patch.object(client._client.responses, "create", return_value=_fake_response()):
        result = client.invoke("hi")
    assert result.estimated_cost_usd == pytest.approx(1.25 + 10.00)


def test_unknown_model_without_override_has_no_cost_estimate():
    client = OpenAIResponsesClient(api_key="k", model="my-azure-deployment")
    with patch.object(client._client.responses, "create", return_value=_fake_response()):
        result = client.invoke("hi")
    assert result.estimated_cost_usd is None


def test_price_override_replaces_the_table():
    client = OpenAIResponsesClient(
        api_key="k", model="my-azure-deployment", price_usd_per_million=(0.5, 2.0)
    )
    with patch.object(
        client._client.responses,
        "create",
        return_value=_fake_response(input_tokens=2_000_000, output_tokens=500_000),
    ):
        result = client.invoke("hi")
    assert result.estimated_cost_usd == pytest.approx(2 * 0.5 + 0.5 * 2.0)


def test_factory_passes_azure_settings_through(settings):
    settings.OPENAI_API_KEY = "k"
    settings.OPENAI_MODEL = "my-azure-deployment"
    settings.OPENAI_BASE_URL = AZURE_URL
    settings.OPENAI_PRICE_INPUT_USD_PER_1M = 0.5
    settings.OPENAI_PRICE_OUTPUT_USD_PER_1M = 2.0
    with patch("llm.openai_client.OpenAI") as sdk:
        client = build_default_llm_client()
    assert sdk.call_args.kwargs["base_url"] == AZURE_URL
    assert client.price_usd_per_million == (0.5, 2.0)


def test_factory_ignores_a_half_set_price(settings):
    settings.OPENAI_API_KEY = "k"
    settings.OPENAI_BASE_URL = ""
    settings.OPENAI_PRICE_INPUT_USD_PER_1M = 0.5
    settings.OPENAI_PRICE_OUTPUT_USD_PER_1M = None
    with patch("llm.openai_client.OpenAI") as sdk:
        client = build_default_llm_client()
    assert sdk.call_args.kwargs["base_url"] is None
    assert client.price_usd_per_million is None
