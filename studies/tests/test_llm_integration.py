import pytest

from llm.base import LLMResult
from studies.llm_integration import CostLimitedLLMClient, handle_llm_call
from studies.models import LLMCallLog, PromptConfig, PromptConfigVersion, Study, UploadBatch
from studies.services import start_pipeline_run

pytestmark = pytest.mark.django_db


class FakeInner:
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def invoke(self, prompt, *, node_name=None, max_output_tokens=None):
        self.calls += 1
        return LLMResult(
            text="ok", model=self.model, status="success",
            input_tokens=10, output_tokens=5, latency_ms=1.0, estimated_cost_usd=0.01,
        )


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="researcher", password="x")


@pytest.fixture
def run(user):
    config = PromptConfig.objects.create(name="d", intro_text="i", few_shots_text="f", is_active=True)
    version = PromptConfigVersion.objects.create(source_config=config, version_number=1, intro_text="i", few_shots_text="f")
    batch = UploadBatch.objects.create(uploaded_file="uploads/x.pdf", uploaded_by=user)
    study = Study.objects.create(batch=batch)
    from studies.models import PipelineRun

    return PipelineRun.objects.create(study=study, run_number=1, started_by=user, prompt_config_version=version)


class TestHandleLlmCall:
    def test_creates_log_row_and_accumulates_totals(self, run):
        result = LLMResult(
            text="hi", model="gpt-5", status="success",
            input_tokens=20, output_tokens=8, latency_ms=150.0, estimated_cost_usd=0.002,
        )
        handle_llm_call(run, "generate", result)

        run.refresh_from_db()
        assert LLMCallLog.objects.filter(run=run).count() == 1
        log = LLMCallLog.objects.get(run=run)
        assert log.node_name == "generate"
        assert log.status == "success"
        assert log.input_tokens == 20

        assert run.total_api_calls == 1
        assert run.total_input_tokens == 20
        assert run.total_output_tokens == 8
        assert run.total_latency_ms == pytest.approx(150.0)
        assert run.estimated_cost_usd == pytest.approx(0.002)

    def test_accumulates_across_multiple_calls(self, run):
        handle_llm_call(run, "retrieve_guide", LLMResult(text="", model="m", status="success", input_tokens=5, output_tokens=2, estimated_cost_usd=0.001))
        handle_llm_call(run, "generate", LLMResult(text="", model="m", status="success", input_tokens=10, output_tokens=4, estimated_cost_usd=0.002))

        run.refresh_from_db()
        assert run.total_api_calls == 2
        assert run.total_input_tokens == 15
        assert run.estimated_cost_usd == pytest.approx(0.003)

    def test_failed_call_is_recorded_clearly(self, run):
        result = LLMResult(text="", model="gpt-5", status="failed", error_message="rate limited")
        handle_llm_call(run, "formatter", result)

        log = LLMCallLog.objects.get(run=run)
        assert log.status == "failed"
        assert log.error_message == "rate limited"
        # A failed call still counts toward total_api_calls (a call was made).
        run.refresh_from_db()
        assert run.total_api_calls == 1

    def test_none_cost_does_not_crash_or_change_total(self, run):
        handle_llm_call(run, "generate", LLMResult(text="", model="m", status="failed", estimated_cost_usd=None))
        run.refresh_from_db()
        assert run.estimated_cost_usd is None


class TestCostLimitedLLMClient:
    def test_disabled_by_default_always_delegates(self, run):
        inner = FakeInner()
        client = CostLimitedLLMClient(inner, run)

        for _ in range(5):
            result = client.invoke("prompt")
            assert result.ok

        assert inner.calls == 5

    def test_enabled_but_under_limit_delegates(self, run):
        run.cost_limit_enabled = True
        run.cost_limit_usd = 10.0
        run.estimated_cost_usd = 1.0
        run.save()

        inner = FakeInner()
        result = CostLimitedLLMClient(inner, run).invoke("prompt")

        assert result.ok
        assert inner.calls == 1
        run.refresh_from_db()
        assert run.halted_due_to_cost_limit is False

    def test_enabled_and_at_limit_refuses_without_calling_inner(self, run):
        run.cost_limit_enabled = True
        run.cost_limit_usd = 0.5
        run.estimated_cost_usd = 0.5
        run.save()

        inner = FakeInner()
        result = CostLimitedLLMClient(inner, run).invoke("prompt")

        assert result.ok is False
        assert result.status == "failed"
        assert "limit" in result.error_message.lower()
        assert inner.calls == 0

        run.refresh_from_db()
        assert run.halted_due_to_cost_limit is True

    def test_enabled_and_over_limit_refuses(self, run):
        run.cost_limit_enabled = True
        run.cost_limit_usd = 0.5
        run.estimated_cost_usd = 0.9
        run.save()

        inner = FakeInner()
        result = CostLimitedLLMClient(inner, run).invoke("prompt")

        assert result.ok is False
        assert inner.calls == 0
