"""Cost estimates: gpt-5.4-mini's price, showing costs in Canadian
dollars, and filling in costs for past runs from recorded token counts."""
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse

from llm.pricing import estimate_cost_usd
from studies.models import LLMCallLog, PipelineRun, PromptConfig, Question, Study, UploadBatch
from studies.services import start_pipeline_run
from studies.templatetags.money import cost

User = get_user_model()


def test_gpt_5_4_mini_price():
    # 1M input at $0.75 + 1M output at $4.50
    assert estimate_cost_usd("gpt-5.4-mini", 1_000_000, 1_000_000) == pytest.approx(5.25)


def test_gpt_5_price_unchanged():
    assert estimate_cost_usd("gpt-5", 1_000_000, 1_000_000) == pytest.approx(11.25)


def test_cost_shows_usd_without_a_rate(settings):
    settings.COST_USD_TO_CAD_RATE = None
    assert cost(0.155) == "US$0.1550"
    assert cost(None) == "—"


def test_cost_shows_cad_with_a_rate(settings):
    settings.COST_USD_TO_CAD_RATE = 1.4
    assert cost(0.155) == "CA$0.2170 (US$0.1550)"


@pytest.fixture
def run_without_cost(db):
    alice = User.objects.create_user(username="alice", password="pw12345")
    PromptConfig.objects.create(name="default", intro_text="i", few_shots_text="f", is_active=True)
    Question.objects.create(text="Q?", order=0, active=True)
    batch = UploadBatch.objects.create(
        uploaded_file=SimpleUploadedFile("s.pdf", b"%PDF-1.4", content_type="application/pdf"),
        original_filename="s.pdf", uploaded_by=alice, split_status=UploadBatch.SplitStatus.CONFIRMED,
    )
    study = Study.objects.create(batch=batch, label="S", status=Study.Status.COMPLETED)
    run = start_pipeline_run(study, alice)
    run.status = PipelineRun.Status.COMPLETED
    run.llm_model = "gpt-5.4-mini"
    run.total_input_tokens = 2_000_000
    run.total_output_tokens = 100_000
    run.estimated_cost_usd = None
    run.save()
    for _ in range(2):
        LLMCallLog.objects.create(
            run=run, node_name="generate", model="gpt-5.4-mini-2026-03-17",
            input_tokens=1_000_000, output_tokens=50_000, status="success",
        )
    return run


@pytest.mark.django_db
def test_recalculate_previews_without_saving(run_without_cost):
    out = StringIO()
    call_command("recalculate_costs", stdout=out)

    run_without_cost.refresh_from_db()
    assert run_without_cost.estimated_cost_usd is None
    assert "US$1.9500" in out.getvalue()  # 2 x (0.75 + 0.05 x 4.50)


@pytest.mark.django_db
def test_recalculate_apply_fills_run_and_calls(run_without_cost):
    call_command("recalculate_costs", "--apply", stdout=StringIO())

    run_without_cost.refresh_from_db()
    assert run_without_cost.estimated_cost_usd == pytest.approx(1.95)
    assert all(c.estimated_cost_usd == pytest.approx(0.975) for c in run_without_cost.call_logs.all())


@pytest.mark.django_db
def test_recalculate_never_overwrites_a_recorded_cost(run_without_cost):
    run_without_cost.estimated_cost_usd = 9.99
    run_without_cost.save()

    call_command("recalculate_costs", "--apply", stdout=StringIO())

    run_without_cost.refresh_from_db()
    assert run_without_cost.estimated_cost_usd == 9.99


@pytest.mark.django_db
def test_results_page_shows_cad(client, run_without_cost, settings):
    settings.COST_USD_TO_CAD_RATE = 1.4
    call_command("recalculate_costs", "--apply", stdout=StringIO())
    client.login(username="alice", password="pw12345")

    resp = client.get(reverse("studies:run_detail", kwargs={"study_id": run_without_cost.study_id, "run_id": run_without_cost.id}))

    html = resp.content.decode()
    assert "CA$2.7300 (US$1.9500)" in html
    assert "upper estimate" in html
