"""Fills in the estimated cost of past runs whose cost is missing because
their model's price wasn't known when they ran, using the token counts
already recorded for each AI call and the current price list
(llm/pricing.py, or the OPENAI_PRICE_* settings when both are set).

Shows what it would change by default; pass --apply to save. Only fills
costs that are empty: it never changes a cost that was already recorded.

    python manage.py recalculate_costs            # preview
    python manage.py recalculate_costs --apply    # save
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from llm.pricing import estimate_cost_usd
from studies.models import PipelineRun


def _price(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if settings.OPENAI_PRICE_INPUT_USD_PER_1M is not None and settings.OPENAI_PRICE_OUTPUT_USD_PER_1M is not None:
        return (input_tokens / 1_000_000) * settings.OPENAI_PRICE_INPUT_USD_PER_1M + (
            output_tokens / 1_000_000
        ) * settings.OPENAI_PRICE_OUTPUT_USD_PER_1M
    return estimate_cost_usd(model, input_tokens, output_tokens)


class Command(BaseCommand):
    help = "Fill in missing cost estimates for past runs from their recorded token counts."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the changes (default: preview only).")

    def handle(self, *args, apply=False, **options):
        runs = PipelineRun.objects.filter(estimated_cost_usd__isnull=True).prefetch_related("call_logs")
        changed = 0
        for run in runs:
            calls = list(run.call_logs.all())
            if not calls:
                continue
            new_costs = {}
            for call in calls:
                if call.estimated_cost_usd is not None:
                    continue
                # The run's model name is the one the price list knows (on
                # Azure, the deployment name); fall back to the call's own.
                cost = _price(run.llm_model, call.input_tokens, call.output_tokens)
                if cost is None:
                    cost = _price(call.model, call.input_tokens, call.output_tokens)
                if cost is not None:
                    new_costs[call.id] = cost
            missing = [c for c in calls if c.estimated_cost_usd is None and c.id not in new_costs]
            if missing:
                self.stdout.write(f"Run {run.id} (study {run.study_id}): skipped, no price for model '{run.llm_model}'.")
                continue
            total = sum(c.estimated_cost_usd or new_costs.get(c.id, 0.0) for c in calls)
            self.stdout.write(
                f"Run {run.id} (study {run.study_id}, {run.llm_model}): {len(calls)} calls, "
                f"{run.total_input_tokens} in / {run.total_output_tokens} out tokens -> US${total:.4f}"
            )
            if apply:
                with transaction.atomic():
                    for call in calls:
                        if call.id in new_costs:
                            call.estimated_cost_usd = new_costs[call.id]
                            call.save(update_fields=["estimated_cost_usd"])
                    run.estimated_cost_usd = total
                    run.save(update_fields=["estimated_cost_usd"])
            changed += 1
        verb = "Updated" if apply else "Would update"
        self.stdout.write(self.style.SUCCESS(f"{verb} {changed} run(s)."))
        if changed and not apply:
            self.stdout.write("Nothing saved yet. Run again with --apply to save.")
