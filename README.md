# DATA_Project — Toxicology Report QA Pipeline

An automated pipeline that reads toxicology study PDFs and answers a fixed
set of extraction questions (exposure route, purity, dosage, hazard
classification, etc.) using a local LLM, a hybrid (semantic + BM25)
retrieval index, and LangGraph for orchestration.

> **This README describes the original CLI pipeline below, which remains
> untouched and fully runnable.** A Django/GPT-5 conversion of this same
> pipeline is in progress on the `django-app` branch — see
> [`docs/PHASE_STATUS.md`](docs/PHASE_STATUS.md) for what's implemented
> and what isn't yet.

---

## 1. Prerequisites

- **Python 3.10+** (the codebase uses `str | None` style type hints)
- **Ollama 0.30.9** — see setup below. This exact version matters.
- **all-MiniLM-L6-v2** - This is the local embedding model, you should download this model from huggingface.
- A CUDA-capable NVIDIA GPU is recommended but not required.

### Local Embedding Model - all-MiniLM-L6-v2
Visit the model on HuggingFace: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
You should navigate to 'Files and versions' and download the exact setup (folders included) for the all-MiniLM-L6-v2
as shown in the depot. After download, copy the entire folder contents to the folder named 'all-MiniLM-L6-v2' in the 
'embeddings_local' folder, so that D.A.T.A can access it later on.

### Why Ollama 0.30.9 specifically

Install **Ollama version 0.30.9**, not the latest release. Versions after
0.30.9 changed how NVIDIA GPU support is handled in ways that don't play
well with this setup, so newer builds are currently avoided here.

1. Download the **0.30.9** installer from the
   [Ollama GitHub releases page](https://github.com/ollama/ollama/releases)
   (find the `v0.30.9` tag — do not use the "latest" link on ollama.com,
   which will pull a newer version).
2. Run the installer.
3. **The first time Ollama opens after installation, go to its settings and
   disable "Auto-download updates" before doing anything else.** If you skip
   this, Ollama will silently upgrade itself past 0.30.9 the next time it
   runs.
4. Pull the model this pipeline uses:
   ```bash
   ollama pull phi4
   ```
5. Confirm the version:
   ```bash
   ollama --version
   ```

---

## 2. Python environment

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / HPC
source venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt` covers the core pipeline (LangGraph, LangChain-Ollama,
PyMuPDF, sentence-transformers, docling for table OCR). `marker-pdf` and
`paddleocr` are commented out — only install those if you plan to call
`ocr_marker()` or `ocr_paddle()` in `utils.py` directly; the active pipeline
only uses `ocr_docling()`.

On HPC / offline clusters: the embedding model is loaded from a **local**
folder (see below), so no Hugging Face download is required at runtime.

---

## 3. Folder layout

The app expects the following structure alongside the code (matching the
paths in `config.py`):

```
DATA_Project/
├── embeddings_local/
│   └── all-MiniLM-L6-v2/        # local sentence-transformers model files
├── pdf/
│   └── <your_studies>/          # the toxicology report PDFs to process
├── dependants/
│   └── Structured EAU1 _student_ handbook (2).pdf   # evaluator's guide
├── chats/                       # created automatically; run output goes here
└── (the .py files from this repo)
```

- **`embeddings_local/all-MiniLM-L6-v2`** — download the
  `sentence-transformers/all-MiniLM-L6-v2` model files once and point
  `config.embedding_model_fp` at the local folder (this avoids a
  Hugging Face download on machines without internet access, e.g. HPC).
- **`dependants/...handbook.pdf`** — the "evaluator's manual" PDF whose
  section titles are wrapped in `+++Title+++` markers; `retrieve_guide`
  parses this to find guidance relevant to each question.
- **`chats/`** — where per-study run transcripts and responses are written.

Open `config.py` and update `chats_dir`, `pdf_dir`, and `handbook_dir` to
match your machine (Windows local dev vs. Linux HPC paths differ).

---

## 4. Running it

Make sure Ollama is running in the background (`ollama serve`, or just have
the desktop app open), then:

```bash
python main.py
```

This will:
1. List every PDF in `pdf_dir`.
2. For each PDF, build a retrieval index once (see architecture below).
3. Ask it all 11 questions from `questions.py`.
4. Write `*_full_convo.txt` and `*_response_only.txt` files per study into
   `chats_dir`.

Studies that already have a `*_full_convo.txt` file in `chats_dir` are
skipped, so a run can be safely re-launched after an interruption.

---

## 5. Architecture

### 5.1 Module map

| File | Responsibility |
|---|---|
| `main.py` | Entry point — wires config + questions + graph and starts the run. |
| `config.py` | LLM settings (Ollama model, temperature, etc.) and filesystem paths. |
| `state.py` | `GraphState` — the shared dict LangGraph passes between nodes. |
| `models.py` | Plain data classes: `Document`, `SearchResult`, `Chunk`. |
| `questions.py` | The fixed Q1–Q11 question set, plus the shared intro/few-shot text. |
| `document_processor.py` | Turns one PDF into one or more retrieval **corpora** (`docs` + `summary` + `title_page`). |
| `search/bm25.py` | Tokenizer, keyword-candidate generator, and `SimpleBM25`. |
| `search/vector_store.py` | `VectorStore` — hybrid semantic + BM25 retrieval, fused via Reciprocal Rank Fusion (RRF). |
| `nodes/retrieve.py` | LangGraph node — runs `VectorStore.search()` and assembles context. |
| `nodes/retrieve_guide.py` | LangGraph node — picks the most relevant handbook section for the question. |
| `nodes/augment.py` | LangGraph node — builds the final prompt from context + guide + question. |
| `nodes/generate.py` | LangGraph node — calls the LLM. |
| `nodes/formatter.py` | LangGraph node — normalizes the raw answer into `<CATEGORY> : <ANSWER>` lines. |
| `graph.py` | Wires the five nodes above into a compiled LangGraph pipeline. |
| `run_methods.py` | Orchestrator — loops PDFs, builds each index once, loops questions against it. |
| `utils.py` | Lower-level helpers: text cleaning, TOC/table detection, OCR, section detection, PDF chunking/splitting. |

### 5.2 Runtime flow

**Step A — build the index once per PDF/corpus**

```
for each PDF in pdf_dir:
    document_processor.build_corpora(pdf)
        -> reads the PDF page by page (pymupdf)
        -> cleans text (utils.clean_pymupdf_text)
        -> detects & OCRs table pages (utils.is_table / utils.ocr_docling)
        -> drops table-of-contents pages (utils.is_toc)
        -> finds the title page and summary/abstract section
        -> returns a list of corpora: [{docs, summary, title_page, label}, ...]
```

A PDF normally yields a **single corpus** (the whole report). It can yield
**multiple corpora** in two special cases, both handled inside
`build_corpora`:

- the report is long and gets split into per-study chunks
  (`utils.chunk_report`), or
- the report is an "integrated" multi-assessment regulatory summary
  (`utils.chunk_integrated`) that bundles several assessments (toxicity,
  environmental, residue, ...) under one document — one corpus is built per
  matching target section, sharing any report-wide sections.

For each corpus, `run_methods.py` builds a `VectorStore` **once**:

```python
store = VectorStore(model_dir=embedding_model_fp)
store.add_documents(documents=corpus['docs'])
```

**Step B — answer every question against that same store**

```
for each of the 11 questions:
    graph.invoke({... 'corpus_store': store, 'question': question, ...})
        1. retrieve        -> store.search(query) + title page/summary
        2. retrieve_guide   -> LLM picks the matching handbook section
        3. augment          -> builds the full prompt
        4. generate         -> LLM answers
        5. formatter        -> LLM normalizes to <CATEGORY> : <ANSWER>
    -> both message history and the final answer are appended to
       chats_dir/<study>_run_N_full_convo.txt / _response_only.txt
```

Only `store.search()` runs per question — the embeddings and BM25 index are
computed once per corpus and reused for all 11 questions, which is the main
performance win over re-indexing per question.

### 5.3 Retrieval detail (`search/vector_store.py`)

`VectorStore.search()` combines two signals and fuses them with Reciprocal
Rank Fusion:

- **Cosine similarity** between the query and page embeddings
  (`all-MiniLM-L6-v2`, loaded locally).
- **BM25** over tokenized page text, with term weights boosted for
  query-relevant keywords — either explicit keywords supplied per-question
  (`questions.py`) or automatically extracted via a blend of semantic
  centrality and corpus-wide IDF rarity (`VectorStore._extract_keywords`).

### 5.4 Output files

For each corpus processed, two files land in `chats_dir`:

- `<study>[_label]_run_<N>_full_convo.txt` — full LangGraph message history
  for every question (useful for debugging retrieval/prompting).
- `<study>[_label]_run_<N>_response_only.txt` — just the final formatted
  answers, one block per question.

`[_label]` is only present for split/integrated reports, where it identifies
the page range or section the corpus came from.

---

## 6. Notes / known limitations

- The pipeline is currently configured for `debugging=True` and `split=False`
  in `main.py` — re-indexing splitting behavior for very long multi-study
  PDFs is implemented but off by default; flip `split=True` in the
  `gen_run_9_4(...)` call in `main.py` to enable it.
- `ocr_marker` and `ocr_paddle` exist in `utils.py` as alternative OCR
  backends but aren't wired into `document_processor.py`; only
  `ocr_docling` is used for table pages in the active pipeline.
