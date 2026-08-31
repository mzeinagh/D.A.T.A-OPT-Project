"""Phase 1's verification tool: runs the full core_pipeline graph against a
real PDF using the real OpenAI Responses API, printing every question's
formatted answer to stdout.

This is deliberately a thin, DB-free command — no Study/PipelineRun/Answer
rows are written (those models don't exist until Phase 2). Its purpose is
narrow: let a human point it at a real study PDF, with a real
OPENAI_API_KEY and a real downloaded embedding model, and manually compare
the output against a legacy run of the original Ollama-based pipeline
(`python main.py`) for output parity, per the migration plan's Phase 1
requirement.

Usage:
    python manage.py run_pipeline path/to/study.pdf
    python manage.py run_pipeline path/to/study.pdf --split --debugging
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core_pipeline.document_processor import build_corpora
from core_pipeline.graph import graph
from core_pipeline.questions import few_shots, inputs, intro
from core_pipeline.search.vector_store import VectorStore
from llm.factory import build_default_llm_client


class Command(BaseCommand):
    help = "Run the core_pipeline graph against one PDF using the real OpenAI Responses API, for manual output-parity checking against the legacy Ollama pipeline."

    def add_arguments(self, parser):
        parser.add_argument("pdf_path", help="Path to a single study PDF.")
        parser.add_argument(
            "--split",
            action="store_true",
            default=False,
            help="Enable multi-study splitting (core_pipeline.document_processor's split=True path). Off by default, matching the original pipeline's default.",
        )
        parser.add_argument(
            "--debugging",
            action="store_true",
            default=False,
            help="Print each node's progress and retrieved-page debug info as the graph runs.",
        )
        parser.add_argument(
            "--question",
            type=int,
            default=None,
            help="Run only this one question number (1-11) instead of all of them — useful for a quick spot check.",
        )

    def handle(self, *args, **options):
        pdf_path = options["pdf_path"]
        split = options["split"]
        debugging = options["debugging"]
        only_question = options["question"]

        if not settings.OPENAI_API_KEY:
            raise CommandError(
                "OPENAI_API_KEY is not set. Set it in your environment (see .env.example) before running this command — "
                "it makes real, billed calls to the OpenAI API."
            )

        self.stdout.write(f"Building corpora for {pdf_path} (split={split})...")
        try:
            corpora = build_corpora(pdf_path, split=split)
        except Exception as exc:
            raise CommandError(f"build_corpora failed: {exc}") from exc

        if not corpora:
            raise CommandError("build_corpora returned no corpora for this PDF.")

        usage_totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "failures": 0}

        def on_call(node_name, result):
            usage_totals["calls"] += 1
            usage_totals["input_tokens"] += result.input_tokens
            usage_totals["output_tokens"] += result.output_tokens
            if result.estimated_cost_usd is not None:
                usage_totals["cost_usd"] += result.estimated_cost_usd
            if not result.ok:
                usage_totals["failures"] += 1
            status_label = self.style.SUCCESS(result.status) if result.ok else self.style.ERROR(result.status)
            self.stdout.write(
                f"    [{node_name}] {status_label} "
                f"in={result.input_tokens} out={result.output_tokens} "
                f"latency={result.latency_ms:.0f}ms retries={result.retry_count}"
                + (f" error={result.error_message}" if result.error_message else "")
            )

        llm_client = build_default_llm_client(on_call=on_call)

        question_set = inputs
        if only_question is not None:
            question_set = [q for q in inputs if q[0] == f"question {only_question}"]
            if not question_set:
                raise CommandError(f"No question numbered {only_question} (valid range: 1-{len(inputs)}).")

        for corpus_idx, corpus in enumerate(corpora):
            label = corpus.get("label") or "(whole document)"
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Corpus {corpus_idx + 1}/{len(corpora)}: {label} ==="))
            self.stdout.write(f"  {len(corpus['docs'])} page(s) indexed.")

            store = VectorStore(model_dir=settings.EMBEDDING_MODEL_PATH)
            store.add_documents(documents=corpus["docs"])

            for q_idx, q in enumerate(question_set):
                question_id, (question_text, keywords) = q
                self.stdout.write(f"\n  -- {question_id} --")

                start = time.monotonic()
                result_state = graph.invoke({
                    "intro": intro,
                    "few_shots": few_shots,
                    "guidebook_fp": settings.HANDBOOK_PDF_PATH,
                    "guide": None,
                    "question": question_text,
                    "augmented_question": None,
                    "context": [],
                    "output": None,
                    "chats_dir": "",
                    "messages": [],
                    "corpus_store": store,
                    "summary": corpus["summary"],
                    "title_page": corpus["title_page"],
                    "corrected_output": None,
                    "retrieved_pages": None,
                    "debugging": debugging,
                    "keywords": keywords if keywords else None,
                    "llm_client": llm_client,
                })
                duration = time.monotonic() - start

                self.stdout.write(f"  Answer ({duration:.1f}s): {self.style.SUCCESS(result_state['corrected_output'])}")
                if debugging:
                    rp = result_state["retrieved_pages"]
                    self.stdout.write(f"    retrieved {rp['length']} page(s): {rp['page numbers']}")

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Usage summary ==="))
        self.stdout.write(f"  API calls:     {usage_totals['calls']}")
        self.stdout.write(f"  Failed calls:  {usage_totals['failures']}")
        self.stdout.write(f"  Input tokens:  {usage_totals['input_tokens']}")
        self.stdout.write(f"  Output tokens: {usage_totals['output_tokens']}")
        self.stdout.write(f"  Est. cost:     ${usage_totals['cost_usd']:.4f}")
