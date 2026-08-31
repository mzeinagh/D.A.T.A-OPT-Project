# The new utilities file to support version 9.0 onwards
# Last edited: August 24, 2026
# Functions surrounded by #** indicate functions that are either new or modified and haven't been tested yet.

def clean_pymupdf_text(text: str, debugging:bool=False) -> str:
    """
    Dynamically clean raw PyMuPDF text from scanned or digital PDFs.

    Detection-first approach: each cleaning stage analyses the text to
    decide whether the pattern is actually present before acting on it.
    Clean/well-typed documents pass through largely untouched.

    debugging: Boolean. If True, returns both the cleaned text and any removed lines.
    """
    import re
    from collections import Counter

    # ------------------------------------------------------------------ #
    # Stage 1 — Page numbers
    # ------------------------------------------------------------------ #

    #_PAGE_NUMBER_RE = re.compile(
    #    r'''^\s*
    #    (
    #        -\s*\d{1,4}\s*-        |   # -6-
    #        \(\s*\d{1,4}\s*\)      |   # (6)
    #        \[\s*\d{1,4}\s*\]      |   # [6]
    #        Page\s+\d{1,4}         |   # Page 6
    #        \d{1,4}\s*/\s*\d{1,4}     # 6/14  (current/total)
    #    )
    #    \s*$''',
    #    re.IGNORECASE | re.VERBOSE,
    #)

    _PAGE_OF_RE = re.compile(
    r'\bpage\s+\d{1,4}\s+of\s+\d{1,4}\b',
    re.IGNORECASE
    )

    def _isolate_page_of(text: str) -> str:
        # Surround matches with newlines so they become standalone lines
        return _PAGE_OF_RE.sub(r'\n\g<0>\n', text)
    #def _remove_page_numbers(lines: list[str]) -> list[str]:
    #    matches = [i for i, ln in enumerate(lines) if _PAGE_NUMBER_RE.match(ln)]
    #    if len(matches) < 2:
    #        return lines
    #    remove = set(matches)
    #    return [ln for i, ln in enumerate(lines) if i not in remove]


    # ------------------------------------------------------------------ #
    # Stage 2 — Repeated header / footer blocks
    # ------------------------------------------------------------------ #

    #def _remove_repeated_header_footer_blocks(
    #    lines: list[str],
    #    min_repeats: int = 5,
    #    block_radius: int = 3,
    #) -> list[str]:
    #    if len(text.split()) > 400: # ONLY use on MULTI-PAGE TEXTS!
    #        non_empty_indices = [i for i, ln in enumerate(lines) if ln.strip()]
    #
    #        def normalise(s: str) -> str:
    #            return re.sub(r'\s+', ' ', s.strip().lower())

    #        ngram_hits: Counter = Counter()
    #        ngram_line_sets: dict[str, list[list[int]]] = {}

    #        for size in range(1, block_radius + 1):
    #            for start in range(len(non_empty_indices) - size + 1):
    #                idx_group = non_empty_indices[start: start + size]
    #                key = '\x00'.join(normalise(lines[i]) for i in idx_group)
    #                if not key.strip('\x00'):
    #                    continue
    #                ngram_hits[key] += 1
    #                ngram_line_sets.setdefault(key, []).append(idx_group)

    #        remove: set[int] = set()
    #        for key, count in ngram_hits.items():
    #            if count < min_repeats:
    #                continue
    #            parts = key.split('\x00')
    #            if any(len(p) > 120 for p in parts):
    #                continue
    #            for idx_group in ngram_line_sets[key]:
    #                remove.update(idx_group)

    #        return [ln for i, ln in enumerate(lines) if i not in remove]
    #    else: # do nothing
    #        return lines

    # ------------------------------------------------------------------ #
    # Stage 3 — Table-of-contents lines
    # ------------------------------------------------------------------ #

    # FIX: Replaced nested/ambiguous quantifiers with a simple anchored pattern.
    # Old: r'(?:(?:\.{2,}|\s*\.\s*){2,})\s*\d{1,3}\s*$'  — catastrophic backtracking
    # New: require 3+ consecutive dots (real TOC leaders always are), no nesting.
    _TOC_RE = re.compile(r'\.{3,}\s*\d{1,3}\s*$')

    # FIX: Replaced the combined lazy+greedy r'.{5,}?\s{3,}\d' pattern with a
    # two-step function. The old single-regex form backtracked catastrophically
    # on long lines that partially matched but had no 3-space run before the number.
    def _is_toc_numbered(line: str) -> bool:
        """'2.2   Some Title   8' style TOC line, checked in two safe passes."""
        if len(line) > 300:
            return False
        if not re.match(r'^\s*\d+(?:\.\d+)*\s+', line):
            return False
        return bool(re.search(r'\s{3,}\d{1,3}\s*$', line))

    def _remove_toc_lines(lines: list[str]) -> list[str]:
        toc_indices = [
            i for i, ln in enumerate(lines)
            if _TOC_RE.search(ln) or _is_toc_numbered(ln)
        ]
        if len(toc_indices) < 6:
            return lines

        clustered = _has_cluster(toc_indices, window=30, min_in_window=3)
        if not clustered:
            return lines

        remove = set(toc_indices)
        return [ln for i, ln in enumerate(lines) if i not in remove]


    # ------------------------------------------------------------------ #
    # Stage 4 — Junk / noise tokens
    # ------------------------------------------------------------------ #

    _JUNK_CHECKS = [
        re.compile(r'[\x00-\x08\x0b-\x1f\x7f]'),
        re.compile(r'\^[A-Z@\[\\\]^_]'),
    ]

    def _is_junk_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        for pattern in _JUNK_CHECKS:
            if pattern.search(stripped):
                return True
        non_space = stripped.replace(' ', '')
        if len(non_space) == 0:
            return False
        # NEW: never flag lines containing digits as junk — real short data
        # values (percentages, dosages, measurements, "Purity: 99.9%" split
        # across lines) almost always contain digits; noise/garbled tokens
        # from PDF extraction almost never do.
        if any(c.isdigit() for c in non_space):
            return False
        symbol_count = sum(1 for c in non_space if not c.isalnum())
        if len(non_space) <= 6 and symbol_count / len(non_space) >= 0.5:
            return True
        return False

    def _remove_junk_tokens(lines: list[str]) -> list[str]:
        flagged = [i for i, ln in enumerate(lines) if _is_junk_line(ln)]
        if not flagged:
            return lines
        remove = set(flagged)
        return [ln for i, ln in enumerate(lines) if i not in remove]


    # ------------------------------------------------------------------ #
    # Stage 5 — Hyphenated line-breaks
    # ------------------------------------------------------------------ #

    _SOFT_HYPHEN_RE = re.compile(r'[¬\xad]-?\n\s*')
    _HARD_HYPHEN_RE = re.compile(r'(?<=[a-z])-\n([a-z])')

    def _fix_hyphenated_linebreaks(text: str) -> str:
        if '\xad' in text or '¬' in text:
            text = _SOFT_HYPHEN_RE.sub('-', text)
        if re.search(r'[a-z]-\n[a-z]', text):
            text = _HARD_HYPHEN_RE.sub(r'\1', text)
        return text


    # ------------------------------------------------------------------ #
    # Stage 6 — Over-spaced words
    # ------------------------------------------------------------------ #

    _SPACED_WORD_RE = re.compile(r'\b(?:[a-z] ){2,}[a-z]\b')

    def _fix_overspacedwords(text: str) -> str:
        words = re.findall(r'\b\w+\b', text)
        if not words:
            return text
        single_char_ratio = sum(1 for w in words if len(w) == 1) / len(words)
        if single_char_ratio < 0.20:
            return text

        def _collapse(m: re.Match) -> str:
            return m.group(0).replace(' ', '')

        return _SPACED_WORD_RE.sub(_collapse, text)

    # ------------------------------------------------------------------ #
    # Stage 7 — Soft line-wrap rejoining
    # ------------------------------------------------------------------ #

    def _rejoin_wrapped_lines(text: str) -> str:
        text = re.sub(r'\n{2,}', '\x00PARA\x00', text)
        lines = text.split('\n')

        wrapped_count = 0
        for i, ln in enumerate(lines[:-1]):
            ln_s = ln.rstrip()
            next_s = lines[i + 1].lstrip()
            if (ln_s
                    and next_s
                    and not re.search(r'[.!?:;]\s*$', ln_s)
                    and re.match(r'[a-z("]', next_s)):
                wrapped_count += 1

        wrap_ratio = wrapped_count / max(len(lines), 1)

        if wrap_ratio >= 0.25:
            text = re.sub(r'(?<![.!?:;])\n(?=[a-z("])', ' ', text)

        text = text.replace('\x00PARA\x00', '\n\n')
        return text


    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    def _has_cluster(
        indices: list[int],
        window: int,
        min_in_window: int,
    ) -> bool:
        if not indices:
            return False
        for i, start in enumerate(indices):
            count = sum(1 for j in indices[i:] if j - start <= window)
            if count >= min_in_window:
                return True
        return False

    def clean_with_audit(text: str) -> tuple[str, dict[str, list[str]]]: # returns kept AND removed lines for debugging
        removed = {}
        lines = text.split('\n')

        #before = lines
        #lines = _remove_repeated_header_footer_blocks(lines)
        #removed['header_footer'] = [ln for ln in before if ln not in lines]

        before = lines
        lines = _remove_toc_lines(lines)
        removed['toc'] = [ln for ln in before if ln not in lines]

        before = lines
        lines = _remove_junk_tokens(lines)
        removed['junk'] = [ln for ln in before if ln not in lines]

        return '\n'.join(lines), removed

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = _isolate_page_of(text)
    lines = text.split('\n')

    if debugging:
        kept_lines, removed_lines = clean_with_audit
        return kept_lines, removed_lines
    
    #lines = _remove_page_numbers(lines)
    #lines = _remove_repeated_header_footer_blocks(lines)
    lines = _remove_toc_lines(lines)
    lines = _remove_junk_tokens(lines)

    text = '\n'.join(lines)

    text = _fix_hyphenated_linebreaks(text)
    text = _fix_overspacedwords(text)
    text = _rejoin_wrapped_lines(text)

    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

def clean_prompt_input(prompt: str) -> str:
    """
    Post-interpolation cleanup for LLM prompts containing PyMuPDF-extracted
    text injected into a structured template.

    Fixes the common typo EXERPT -> EXCERPT, then isolates the text between
    the BEGIN/END EXCERPT delimiters and cleans only that portion. Everything
    outside the delimiters (system prompt, instructions, few-shots) is
    left completely untouched.

    Returns the prompt with the excerpt portion cleaned.
    """
    import re 

    def _clean_excerpt(text: str) -> str:
        """
        Master cleaning pipeline for the extracted study text.

        Applies each stage in order:
        1. TOC block removal - removed from code.
        2. Structural noise line removal
        3. Orphan fragment removal
        4. Blank line normalisation

        Each stage is gated — it will do nothing if the relevant pattern
        is not sufficiently present in the text (safe for clean documents).
        """

        # Stage 2: Remove isolated structural metadata lines (section numbers,
        # all-caps labels, lone page numbers, stray punctuation)
        lines = text.split('\n')
        lines = _remove_structural_noise_lines(lines)
        text  = '\n'.join(lines)

        # Stage 4: Collapse runs of 3+ blank lines down to a single blank line
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text

    # ------------------------------------------------------------------ #
    # Stage 2 — Structural noise line removal
    # ------------------------------------------------------------------ #

    # Lines that are only dashes, quotes, or similar stray punctuation characters
    # Unicode escapes used to avoid embedding literal quote chars in the string:
    #   \u2013 = en dash, \u2014 = em dash
    #   \u2018/\u2019 = curly single quotes, \u201c/\u201d = curly double quotes
    _STRAY_PUNCT_RE      = re.compile(
        r'^[\-\u2013\u2014\'\"\u2018\u2019\u201c\u201d]{1,3}\s*$'
    )

    # Safeguard: these toxicology-relevant ALL-CAPS terms should never be removed
    # even if they match one of the patterns above (e.g. "DERMAL", "ORAL")
    _KEEP_LABELS_RE = re.compile(
        r'\b(DERMAL|ORAL|INHALATION|TOPICAL|INTRADERMAL|OCCLUSIVE|'
        r'INDUCTION|CHALLENGE|DOSE|RESULT|CONCLUSION|SUMMARY)\b'
    )


    def _is_structural_noise(line: str) -> bool:
        """
        Return True if this line is isolated structural metadata that adds
        no semantic value to the prompt (e.g. a bare section number, a
        lone page number, an all-caps document label).

        Always returns False for lines containing toxicology-relevant terms
        so that meaningful content is never accidentally stripped.
        """
        s = line.strip()
        if not s:
            return False  # blank lines handled separately by blank-line normalisation

        # Never remove lines that contain domain-relevant keywords
        if _KEEP_LABELS_RE.search(s):
            return False

        return bool(
            _STRAY_PUNCT_RE.match(s)
        )

    def _remove_structural_noise_lines(lines: list[str]) -> list[str]:
        """
        Filter out every line identified as structural noise.
        Operates on the full line list so context (surrounding lines) is
        available to the individual check if needed in future.
        """
        return [ln for ln in lines if not _is_structural_noise(ln)]

    # ------------------------------------------------------------------ #
    # Shared utility
    # ------------------------------------------------------------------ #

    def _has_cluster(indices: list[int], window: int, min_in_window: int) -> bool:
        """
        Return True if at least `min_in_window` values in `indices` fall
        within any contiguous span of `window` lines.

        Used to confirm that matched lines are grouped together (e.g. a real
        TOC block) rather than scattered randomly across the document.
        """
        for i, start in enumerate(indices):
            count = sum(1 for j in indices[i:] if j - start <= window)
            if count >= min_in_window:
                return True
        return False
    
    # Cleaning

    # Fix the delimiter typo that appears in the original prompt template
    prompt = prompt.replace('BEGIN EXERPT', 'BEGIN EXCERPT')
    prompt = prompt.replace('END EXERPT', 'END EXCERPT')

    # Match everything between the dashed delimiter lines, capturing:
    #   group(1) = opening delimiter  e.g. "BEGIN EXCERPT-------"
    #   group(2) = the raw extracted text we want to clean
    #   group(3) = closing delimiter  e.g. "-------END EXCERPT"
    delimiter_re = re.compile(
        r'(BEGIN EXCERPT-+)(.*?)(-+END EXCERPT)',
        re.DOTALL
    )
    match = delimiter_re.search(prompt)
    if not match:
        # No excerpt block found — return the prompt unchanged
        return prompt

    before  = prompt[:match.start()]   # everything before the opening delimiter
    open_d  = match.group(1)           # the opening delimiter line itself
    excerpt = match.group(2)           # the injected study text
    close_d = match.group(3)           # the closing delimiter line itself
    after   = prompt[match.end():]     # everything after the closing delimiter

    excerpt = _clean_excerpt(excerpt)

    # Reassemble the prompt with the cleaned excerpt in place
    return before + open_d + excerpt + close_d + after

def cleanup_text(text: str) -> str:
    """
    Removes footers, page numbers, tables, and irrelevant statistical
    information from extracted PDF text.

    Args:
        text: Raw extracted text string.

    Returns:
        Cleaned text string.
    """
    import re

    header_patterns = [
        r"^\s*(Appendix No\. \d+):.*",
        r"^\s*Table \d+.*",
        r"^\s*Figure \d+.*",
    ]
    footer_patterns = [
        r"Page \d+ of \d+",
        r"^\s*\d+\s*$",
    ]
    table_patterns = [
        r"^\s*(\d+\.\d+\s+){4,}",
        r"^\s*(\d+\s+){4,}",
    ]
    stat_patterns = [
        r"p\s*[<>]?\s*0\.\d+",
        r"\d+\s*±\s*\d+",
        r"[A-Za-z]+\s*=\s*\d+",
        r"r\s*=\s*-?\d+\.\d+",
    ]
    table_keywords = {
        "SD", "SEM", "N.S.", "p<", "p>", "p=", "t-test", "ANOVA",
        "±", "SE", "STD", "STDEV", "standard deviation", "standard error",
        "statistical", "TABLE",
    }

    lines = text.split("\n")
    cleaned_lines = []
    in_table = False
    table_lines = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if any(re.match(p, line) for p in header_patterns):
            continue
        if any(re.match(p, line) for p in footer_patterns):
            continue
        if line.isupper() and len(line) > 3:
            continue

        is_table_line = (
            any(re.match(p, line) for p in table_patterns)
            or any(kw.lower() in line.lower() for kw in table_keywords)
            or any(re.search(p, line) for p in stat_patterns)
            or line.count("\t") > 2
            or line.count("  ") > 3
        )

        if is_table_line:
            in_table = True
            table_lines += 1
            continue

        if in_table and (len(line.split()) > 5 or len(line) > 60):
            in_table = False
            table_lines = 0

        if not in_table:
            line = re.sub(r"\([^)]*\b(p|SD|SE|SEM|n)\b[^)]*\)", "", line)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)
    result = re.sub(r"\b(SD|SE|SEM)\b\s*[<>]?=\s*\d+\.?\d*", "", result)
    result = re.sub(r"\d+\s*±\s*\d+", "", result)
    result = re.sub(r"\s+", " ", result).strip()

    return result

def is_table(text:str, boolean:bool = True, threshold:float = 0.70):
    """
    Returns the likelihood of a page being a table if boolean = False. 0-1
    Otherwise, returns True/False.
    """
    
    import re

    _NUM_TOKEN = re.compile(
        r"(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?%?(?![\w.])"
    )
    _TABLE_CAPTION = re.compile(r'\btables?\s+\d+(?:\s*-\s*\d+)?\b', re.IGNORECASE)
    _STATS_PATTERN = re.compile(r"\b[pP]\s*[<>]=?\s*0?\.\d+")
    _DECIMAL_ISOLATED = re.compile(r"(?<![\w.])\d+\.\d+(?![\w.])")


    def _isolated_numbers(line: str) -> list[str]:
        """Standalone numeric tokens in a line (not digits embedded in a word)."""
        return _NUM_TOKEN.findall(line)


    def _has_short_line_run(lines, max_len=30, run_len=5, digit_frac=0.4):
        run = digit_lines = 0
        for line in lines:
            if len(line) <= max_len:
                run += 1
                if _isolated_numbers(line):
                    digit_lines += 1
                if run >= run_len and digit_lines / run >= digit_frac:
                    return True
            else:
                run = digit_lines = 0
        return False


    def table_likelihood(
        text: str,
        min_numbers: int = 6,
        min_multilines: int = 6,
        min_decimal_count: int = 8,
        ) -> float:
        """
        Returns a 0.0-1.0 likelihood that `text` is a table.

        min_numbers = isolated numeric tokens in a line to count as 'numeric heavy'.
        min_multilines = isolated numeric tokens in a line to count toward 'multi_number_lines'.
        min_decimal_count = isolated decimal values in the text for full decimal-density credit.
        """
        non_blank_lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not non_blank_lines:
            return 0.0

        n_lines = len(non_blank_lines)
        line_num_counts = [len(_isolated_numbers(l)) for l in non_blank_lines]

        decimal_count = len(_DECIMAL_ISOLATED.findall(text))
        numeric_heavy_frac = sum(c >= min_numbers for c in line_num_counts) / n_lines
        multi_number_frac = sum(c >= min_multilines for c in line_num_counts) / n_lines
        has_caption = bool(_TABLE_CAPTION.search(text))
        has_stats = bool(_STATS_PATTERN.search(text))
        has_short_run = _has_short_line_run(non_blank_lines)

        score = 0.0
        if has_caption:
            score += 0.50
        score += min(decimal_count / min_decimal_count, 1.0) * 0.20
        score += min(numeric_heavy_frac / 0.60, 1.0) * 0.20
        score += min(multi_number_frac / 0.60, 1.0) * 0.15
        if has_stats:
            score += 0.10
        if has_short_run:
            score += 0.15

        return min(score, 1.0)

    if boolean:
            threshold = threshold
            endpoint = False
            if table_likelihood(text) >= threshold:
                endpoint = True
            return endpoint
    
    return table_likelihood(text)

def detect_sections(pdf_fp:str | None, page:int | None = None, text:str | None = None, target_titles:list|None=None,searching:bool=False):
    """
    Inputs:
    pdf_fp: str
    page: optional, specify a single page in the pdf to search, 1-based!!
    text: optional, specify a single string of text (i.e. a page content) to search.
    searching:bool, if true, must provide a list of target titles to search
    target_titles: list of string

    Outputs:
    Sections - List of Section Objects

    if searching == True, the search returns first-hits only
    """
    import re
    import fitz
    import unicodedata
    from collections import Counter

    sections = []
    debug = []

    if page and text:
        raise ValueError("When using detect_sections, please only specify the page number OR the text string you wish to use! Do not input both.")
    if pdf_fp and text:
        raise ValueError("When using detect_sections, please only specify the PDF OR the text string you wish to use! Do not input both. Specifying the text string will only return sections present in the text string.")

    class Section:
        def __init__(self, page_num:int, content:str, title:str):
            self.page_num = page_num
            self.content = content
            self.title = title
    
    Title_patterns = r"^\s*(?:[A-Z][A-Z0-9&''\-]*\s+){0,3}[A-Z][A-Z0-9&''\-]*\s*(?:\((?:cont(?:\.|d|inued)?|continued|contd?)\))?\s*$" # All cap words followed by opt. (cont.) or the like.
    
    TOC_TITLE_RE = re.compile(
        r'(?:(?<=\n)|(?<=\A))'
        r'[ \t]*'
        r'('
            r'(?=.*[A-Za-z])'
            r'(?:[A-Z][a-zA-Z]*(?:[-–—][a-zA-Z]+)?(?:[-–—][a-zA-Z]+)?|[a-z]{1,4})'
            r'(?:[ \t]+(?:[A-Z][a-zA-Z]*(?:[-–—][a-zA-Z]+)?(?:[-–—][a-zA-Z]+)?|[a-z]{1,4}))*'
            r'|'
            r'(?=.*[A-Za-z])'
            r'[A-Z0-9][A-Z0-9 \t\-–—]{2,}'
        r')'
        r'[ \t]*'
        r'\n+'
        r'\s*'
        r'(?=[A-Z])',
        re.MULTILINE
    )

    with fitz.open(pdf_fp) as doc:
        pages = [page.get_text() for page in doc]

        if page or text:
            if page:
                clean_text = clean_pymupdf_text(pages[int(page-1)])

                page_num = page
            if text:
                page_num = 0
                clean_text = text

            # match the first pattern ----------------------------------------------
            matches1 = re.findall(Title_patterns, clean_text, re.MULTILINE)

            if len(matches1)>0:
                for match in matches1:
                    title = match.strip()
                    
                    if (title):
                        sections.append(
                            Section(
                                page_num=page_num,
                                content=clean_text,
                                title=title
                            )
                        )

            # Then match the second pattern ----------------------------------------------
            for m in TOC_TITLE_RE.finditer(clean_text):
                title=m.group(1).strip()

                if (title):
                    sections.append(
                        Section(
                            page_num=page_num,
                            content=clean_text,
                            title=title
                        )
                    )
            
            return sections
        
        # else (not page or text)
        for page_num, text in enumerate(pages, start=1):
            clean_text = clean_pymupdf_text(text) # First clean the format

            # First match the first pattern
            matches1 = re.findall(Title_patterns, clean_text, re.MULTILINE)

            if len(matches1)>0:
                for match in matches1:
                    title = match.strip()
                    
                    if (title):
                        sections.append(
                            Section(
                                page_num=page_num,
                                content=clean_text,
                                title=title
                            )
                        )
            
            # Then match the second pattern
            for m in TOC_TITLE_RE.finditer(clean_text):
                title=m.group(1).strip()

                if (title):
                    sections.append(
                        Section(
                            page_num=page_num,
                            content=clean_text,
                            title=title
                        )
                    )

        if searching == False: # Simply return a list of all section objects detected in the pdf
            return sections
        
        elif searching == True:
            if target_titles:
                # Search for the relevant sections and return them
                target_sections = []
                for target in target_titles:
                    for section in sections:
                        if (target.lower().strip() in section.title.lower().strip()):
                            debug.append(f"Hit found: {section.title}, page:{section.page_num}")
                            relevant = True
                            if is_toc(section.content):
                                debug.append(f"TOC page detected, page: {section.page_num}")
                                continue # skip TOC
                        
                            if target.lower() in ['summary','sumnary']: # No summary of titles. These are usually used to refer to summary of smaller results, not study summary
                                if (" of " in section.title.lower().strip()):
                                    study_summaries = re.compile(r'\bsummary\s+of\s+(?:\w+\s+)*?study\b', re.IGNORECASE)

                                    if not bool(study_summaries.search(section.title.lower().strip())):
                                        debug.append(f'Summary title found but summary was detected to be irrelevant: page {section.page_num}, title: {section.title}')
                                        relevant = False
                                    
                                    elif ("table" in section.title.lower().strip()):
                                        debug.append(f"Summary title found but detected to be a table summary on page {section.page_num}, title: {section.title}")
                                        relevant = False
                            
                            if relevant:
                                target_sections.append(section)

                if len(target_sections)==0:
                    debug.append(f"NO target sections found")
                    return target_sections
                    
                debug.append([t.title for t in target_sections])
                organized_target_sections = []

                for section in target_sections:
                    if section.title.lower() in [s.title.lower() for s in organized_target_sections]: # matching section titles between analyzed section and pre-covered sections
                        continue # move on to next one; already covered.

                    page_num = section.page_num

                    # First and foremost detect if the page is a TOC
                    if is_toc(section.content):
                        continue # do not add to organized_target_sections if page is a TOC
                    
                    # Second detect if the page fully contains the section (includes multiple sections)
                    section_hot_words = ["introduction","1ntroduction","summary","sumnary","abstract","test substance","test material","procedure","results","discussion","references","methods","cell line","harvest","staining","coding"]
                    excluded_hot_words = [word for word in section_hot_words if word not in set(target_titles)]

                    other_matches = re.findall(Title_patterns, pages[page_num-1], re.MULTILINE)
                    other_matches2 = [m.group(0).strip() for m in TOC_TITLE_RE.finditer(pages[page_num-1])]
                    combined_matches = set(other_matches + other_matches2)

                    if (len(combined_matches)) > 1: # More than one section object exists for this page
                        content = section.content
                        target_title = None
                        non_target_titles = []
                        for title in combined_matches:
                            if ( re.compile(r'\b(?=\S*[A-Za-z])(?=\S*\d)(?=\S*[^A-Za-z0-9])\S+\b').search(title) ):
                                # if title is junk (both alphanumeric characters and multiple special characters.)
                                continue
                            if (title.lower() in excluded_hot_words) or any(word.lower() in title.lower() for word in excluded_hot_words):
                                # if title is in elcuded hot words, or any excluded hot words are in title
                                non_target_titles.append(title)
                                continue
                            if (title.lower() in target_titles) or any(word.lower() in title.lower() for word in target_titles):
                                # If this title is a target title
                                target_title = title

                        if (len(non_target_titles)>0) and target_title: # Non target section titles exist on this page
                            debug.append(f"NON target section titles exist here:{non_target_titles}")
                            # Now remove text preceeding our target title and proceeding any non-target titles
                            match = re.search(re.escape(target_title), content, re.IGNORECASE)
                            if match: # Cut everything before the target title
                                debug.append(f"start of target section: {match.start()}")
                                content = content[match.start():]

                            # Cut everything from the first non-target title onward
                            for title in non_target_titles:
                                match2 = re.search(r'(?<!\S)' + re.escape(title.strip()) + r'(?!\S)', content, re.IGNORECASE)
                                if match2:
                                    if ( match2.start() > match.start() ) and ( match2.start()>=150): # ONLY if at least 150 letters have elapsed.
                                        debug.append(f"start of non-target section: {match2.start()}")
                                        try:
                                            content = content[:match2.start()]
                                    
                                        except:
                                            pass

                            organized_target_sections.append(
                                Section(
                                    page_num=page_num,
                                    content=content,
                                    title=target_title
                                )
                            )
                            continue # if non-target titles exist, then the summary section must have ended on this page

                        else: # Only the target section title exists on this page
                            organized_target_sections.append(section)

                    else: # Only one section header exists here, including non-sensical ones.
                        organized_target_sections.append(section)

                    # Checking subsequent page ...
                    if page_num >= len(pages): # make sure following page exists
                        continue
                    if page_num+1 in [t.page_num for t in organized_target_sections]: # subsequent page already included in the retrieved pages (i.e. it contains a target title)
                        continue

                    next_page = pages[page_num] # Since pages is 0-based, page_num should be 1 page after the target page
                    clean_next_page = clean_pymupdf_text(next_page)

                    # Find titles in next page
                    next_matches = re.findall(Title_patterns, clean_next_page, re.MULTILINE)
                    next_matches2 = [m.group(1).strip() for m in TOC_TITLE_RE.finditer(clean_next_page)]
                    combined_next_matches = next_matches + next_matches2
                    non_target_titles2 = []

                    for line in clean_next_page.split('\n'): # search line by line
                        line = line.strip()
                        if ( len(line.split()) > 10 ): # first sentence
                            debug.append(line)
                            if line[0].islower(): # first sentence is lowercase
                                debug.append(f"IS LOWER: {page_num+1}")
                                if (len(next_matches)+len(next_matches))>0: # More than 1 title exists on the next page
                                    content2 = None

                                    for title in combined_next_matches:
                                        if ( re.compile(r'\b(?=\S*[A-Za-z])(?=\S*\d)(?=\S*[^A-Za-z0-9])\S+\b').search(title) ):
                                            # if title is junk (both alphanumeric characters and multiple special characters.)
                                            continue
                                        if (title.strip().lower() in excluded_hot_words) or any(word.strip().lower() in title.lower() for word in excluded_hot_words):
                                            # if title is in elcuded hot words, or any excluded hot words are in title
                                            non_target_titles2.append(title)
                                            continue

                                    if (len(non_target_titles2)>0): # Non target section titles exist on this page

                                        # Cut everything from the first non-target title onward
                                        for title in non_target_titles2:
                                            match = re.search(r'(?<!\S)' + re.escape(title.strip()) + r'(?!\S)', clean_next_page, re.IGNORECASE)
                                            if match:
                                                content2 = clean_next_page[:match.start()]

                                        next_page_sec = Section(
                                            page_num=page_num+1,
                                            content=content2,
                                            title=str(f"{section.title}; continued page")
                                        )
                                        if next_page_sec.page_num not in [t.page_num for t in organized_target_sections]:
                                            organized_target_sections.append(next_page_sec)
                                            break

                                else:
                                    next_page_sec = Section(
                                        page_num=page_num+1,
                                        content=clean_next_page,
                                        title=str(f"{section.title}; continued page")
                                    )
                                    organized_target_sections.append(next_page_sec)
                                    break

                            else: # First sentence is not lower case
                                # Find titles in the following page
                                if (len(next_matches)>0) or (len(next_matches2)>0):
                                    if any(t.strip().lower() in excluded_hot_words for t in next_matches) or any(t.strip().lower() in excluded_hot_words for t in next_matches2): # next page contains a new section
                                        break
                                    else:
                                        pass

                                # next page does not begin a new section
                                # If the next page has very little words, assume it is a continuation of the previous page. 
                                word_limit = 50
                                if len(clean_next_page.split()) <= word_limit:
                                    next_page_sec = Section(
                                        page_num = page_num+1,
                                        content = clean_next_page,
                                        title = str(f"{section.title}; continued page")
                                    )
                                    organized_target_sections.append(next_page_sec)
                                    break

                                else:
                                    pass

                                if (page_num+1) not in [o.page_num for o in organized_target_sections]:
                                    PAGE_NUM_RE = re.compile(
                                        r"""
                                        (?:
                                            # Form 1: "Page 2", "Page 2 of 14", case-insensitive
                                            \bPage\s+\d+(?:\s+of\s+\d+)?\b
                                        )
                                        """,
                                        re.VERBOSE | re.IGNORECASE,
                                    )
                                    matches_next_page = PAGE_NUM_RE.findall(clean_next_page)
                                    matches_previous_page = PAGE_NUM_RE.findall(section.content)

                                    if matches_previous_page: # page number exists in the target section
                                        page_num_previous = int(matches_previous_page[0].split()[1]) # both 'page __ of __' and 'page __' has the page number in the second position.
                                        next_page_num = section.page_num+1

                                        while not matches_next_page: # while next page also contains page number; not an insert
                                            debug.append(f"not page numbers found on page {next_page_num}")
                                            next_page_num += 1
                                            if next_page_num > len(pages):
                                                break # no pages left to analyze
                                            matches_next_page = PAGE_NUM_RE.findall(pages[next_page_num-1]) # convert to 0-based

                                        if matches_next_page:
                                            if int(matches_next_page[0].split()[1]) == (page_num_previous+1): # Page numbers are sequential
                                                debug.append(f"Continuation page found on page {next_page_num}")
                                                if next_page_num not in [o.page_num for o in organized_target_sections]:
                                                    next_page_sec = Section(
                                                        page_num = next_page_num,
                                                        content = pages[next_page_num-1],
                                                        title = str(f"{section.title}; continued page")
                                                    )
                                                    organized_target_sections.append(next_page_sec)
                                                    break
                                                else: # page already included in organized target sections
                                                    break
                                    
                                        else: # next page does not contain page number; likely an insert page (i.e. those 'best copy available' inserts)
                                            break

                                    else: # page numbers are not a thing in this document or does not exist in the target section
                                        break # be safe and assume no continuations.

                                else: # Page already included in organized target sections
                                    break

                return organized_target_sections

def is_toc(text: str, min_entries: int = 4, min_ratio: float = 0.2,words_threshold:int = 100) -> bool:
    import re

    non_blank_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not non_blank_lines:
        return False

    # -------------------------------------------------------------
    # Identify Obvious TOCs
    # -------------------------------------------------------------
    len_words = len(text.split())
    if (len_words < words_threshold) and ('table of contents' in text.lower().strip()):
        return True
    
    # if 'contents' in header
    headers = detect_sections(pdf_fp=None, text=text)
    headers_text = [h.title.lower() for h in headers]
    if any('contents' in h for h in headers_text):
        return True

    # -------------------------------------------------------------
    # Reject obvious tables
    # -------------------------------------------------------------

    table = is_table(text)
    if table:
        return False

    # -------------------------------------------------------------
    # In-depth Positive TOC evidence
    # -------------------------------------------------------------

    # Section numbers like 2.1, 3.4.5
    section_re = re.compile(
        r"""
        ^[\s`'_~•·\-–—]*
        [A-Za-z]{0,2}
        [\s`'_~•·\-–—]*
        \d+
        (?:\s*\.\s*\d+)+
        \s*\.?
        """,
        re.MULTILINE | re.VERBOSE,
    )

    # ALL CAPS headings
    caps_heading_re = re.compile(
        r"^[A-Z][A-Z\s\-/&(),:.]{4,}$",
        re.MULTILINE,
    )

    # TOC entries ending with page numbers
    trailing_page_re = re.compile(
        r"""
        ^
        (?!.*\d.*\d.*\d)          # no more than ~2 numbers on line
        [^\n]{3,80}?
        (?:\.{2,}|\s{2,})
        \d{1,3}
        \s*$
        """,
        re.MULTILINE | re.VERBOSE,
    )

    standalone_page_re = re.compile(
        r"^\s*\d{1,3}\s*$",
        re.MULTILINE,
    )

    numbered = len(section_re.findall(text))
    caps = len(caps_heading_re.findall(text))
    trailing = len(trailing_page_re.findall(text))
    standalone = len(standalone_page_re.findall(text))

    title_like = numbered + caps + trailing

    if title_like >= 4:
        matched = title_like + standalone
    else:
        matched = title_like

    has_contents_header = bool(
        re.search(
            r"^\s*(contents|table of contents)\s*$",
            text,
            re.I | re.M,
        )
    )

    ratio = matched / len(non_blank_lines)

    return has_contents_header or (
        matched >= min_entries
        and ratio >= min_ratio
    )

def ocr_marker(pdf_fp:str, page:int,use_llm:bool=False):
    """
    Marker OCR extraction.
    use_llm=True Uses Ollama's Phi4 model to clean up the text after surya OCR. Optional.
    Enter the page as 1-based!!!
    """
    from marker.converters.pdf import PdfConverter
    from marker.config.parser import ConfigParser
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    if page:
        page_num = f"{page-1}" # Marker takes page numbers as 0-based.

        if use_llm:
            config = {
            "page_range": str(page_num),
            "output_format": "markdown",
            "use_llm": True,
            "llm_service": "marker.services.ollama.OllamaService",
            "ollama_model": "phi4",
            "disable_multiprocessing": True
            }

        else:
            config = {
            "page_range": str(page_num),
            "output_format": "markdown",
            "disable_multiprocessing": True
            }

        config_parser = ConfigParser(config)
        converter = PdfConverter(
            config=config_parser.generate_config_dict(),
            artifact_dict=create_model_dict(),
            processor_list=config_parser.get_processors(),
            renderer=config_parser.get_renderer()
        )

        rendered = converter(pdf_fp)

        text, _, images = text_from_rendered(rendered)
    
    else: # no page specified
        raise ValueError("OCR function is called without a page number. To minimize computational time, please specify the page!")

    return text # string, markdown format

def ocr_paddle(
    pdf_path: str,
    pages: int | list[int] | tuple[int, int],
    det_model_dir: str = r"C:\Users\Grace\.paddlex\official_models\PP-OCRv6_medium_det",
    rec_model_dir: str = r"C:\Users\Grace\.paddlex\official_models\PP-OCRv6_medium_rec",
    textline_ori_model_dir: str = r"C:\Users\Grace\.paddlex\official_models\PP-LCNet_x1_0_textline_ori",
    dpi: int = 200,
    lang: str = "en",
    use_textline_orientation: bool = True,
    ) -> dict[int, str]:
    """
    Run PaddleOCR (3.x) on a subset of pages from a PDF, fully offline.

    pages: single page number (0-indexed), list of page numbers,
           or (start, end) tuple treated as an inclusive range.
    *_model_dir: local paths to pre-downloaded PaddleOCR model dirs,
                 required to avoid any network call.

    Returns {page_number: ocr_text}.
    """
    import fitz  # PyMuPDF
    import numpy as np
    from paddleocr import PaddleOCR

    if isinstance(pages, int):
        page_nums = [pages]
    elif isinstance(pages, tuple):
        start, end = pages
        page_nums = list(range(start, end + 1))
    else:
        page_nums = list(pages)

    ocr = PaddleOCR(
        lang=lang,
        device="gpu",
        text_detection_model_dir=det_model_dir,
        text_recognition_model_dir=rec_model_dir,
        textline_orientation_model_dir=textline_ori_model_dir,
        use_textline_orientation=use_textline_orientation,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
    )

    results = {}
    doc = fitz.open(pdf_path)
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)

    for p in page_nums:
        if p < 0 or p >= len(doc):
            continue
        pix = doc[p].get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        if pix.n == 4:
            img = img[:, :, :3]

        raw = ocr.predict(img)
        page_res = raw[0] if raw else None
        text = "\n".join(page_res["rec_texts"]) if page_res else ""
        results[p] = text

    doc.close()
    return results

def ocr_docling(pdf_path: str, start_page: int | None = None, end_page: int | None = None) -> str:
    """
    Convert a single page or range of pages from a PDF to markdown using docling.

    Args:
        pdf_path: path to the PDF file
        start_page: 1-indexed starting page
        end_page: 1-indexed ending page (inclusive). If None, only start_page is converted.

    Returns:
        Markdown string of the specified page range.
    """
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import InputFormat

    if end_page is None:
        end_page = start_page

    if start_page is None:
        converter = DocumentConverter()
        result = converter.convert(pdf_path)
    else:
        converter = DocumentConverter()
        result = converter.convert(pdf_path, page_range=(start_page, end_page))

    return result.document.export_to_markdown()

def title_page_likeliness(text:str):
    """
    Returns:
    score: float between 0 and 1
    features: dictionary of feature values
    """
    import re
    TITLE_KEYWORDS = {
        "final report",
        "study",
        "subject",
        "from:",
        "to:",
        "study report",
        "test substance",
        "substance",
        "project",
        "project number",
        "study number",
        "protocol",
        "sponsor",
        "laboratory",
        "performed for",
        "prepared for",
        "quality assurance",
        "glp",
        "good laboratory practice",
        "confidential",
        "report",
        "author",
        "study director",
        "study code",
        "cas rn",
        "cas no",
        "cas"
    }

    SECTION_HEADINGS = {
        "summary",
        "introduction",
        "materials",
        "methods",
        "results",
        "discussion",
        "conclusion",
        "references",
        "appendix",
    }

    Appendix_headings = {
        'appendix',
        'appendices'
    }

    if not text.strip():
        return 0.0, {}

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lower = text.lower()

    score = 0
    features = {}

    # 1. Low amount of text

    word_count = len(re.findall(r"\b\w+\b", text))
    features["word_count"] = word_count

    if word_count < 70:
        if not any(a in text.lower() for a in Appendix_headings):
            score += 6
    elif word_count < 100:
        if not any(a in text.lower() for a in Appendix_headings):
            score += 3
    elif word_count < 150:
        if not any(a in text.lower() for a in Appendix_headings):
            score += 2
    else: # penalize verbosity
        score -= 6

    # 2. Few long paragraphs

    long_lines = sum(len(l.split()) > 15 for l in lines)
    features["long_lines"] = long_lines

    if long_lines <= 2:
        score += 2

    # 3. Many short centered-looking lines

    short_lines = sum(len(l.split()) <= 6 for l in lines)
    features["short_lines"] = short_lines

    if short_lines >= len(lines) * 0.5:
        score += 2

    # 4. Title keywords

    keyword_hits = []

    for keyword in TITLE_KEYWORDS:
        if keyword in lower:
            keyword_hits.append(keyword)

    features["title_keywords"] = keyword_hits

    score += min(len(keyword_hits), 5)

    # 5. Penalize section headings

    section_hits = []

    for heading in SECTION_HEADINGS:
        if heading in lower:
            section_hits.append(heading)

    features["section_headings"] = section_hits

    score -= len(section_hits)

    # 6. Peanlize appendix keywords

    if any(a in text.lower() for a in Appendix_headings):
        score -= 4
        features["appendix_headings"]=True
    else:
        features["appendix_headings"]=False

    # 7. Peanlize TOC-like pages
    if is_toc(text):
        score -= 4
        features["toc_like"] = True
    else:
        features["toc_like"] = False

    # 8. Peanlize Table-like pages
    if is_table(text,boolean=False) >= 0.9: # extreme match
        score -= 8
        features["table_like"] = True
    else:
        features["table_like"] = False

    # Normalize

    probability = max(0, min(score / 12, 1))

    return probability, features

def chunk_report(pdf_fp:str, num_batches:int|None = None, batch_size:int|None = 8, targets:list[str]|None = None, store_chunks_locally:bool = False, store_fp:str|None = None):
    """
    This function takes the pdf and creates a document hiearchy for each of the following 2 types of reports:
    1. Single-study reports
    2. Multi-study reports (submission package or simple multi-study report)

    By utilizing the following approach:
    page-by-page analysis to identify study boundaries, where a 'new section score' is defined by the variables:
    Title_page_like,
    Study_preface_like,
    Repeat_Summary,
    Repeat_Introduction,
    Reset_pages,
    Reset_study_id,
    (x)New_substance --> temporarily suspended due to lack of identifying re patterns

    Inputs:
    pdf_fp
    num_batches: the number of batches to use to detect page boundaries.
    batch_size: if not num_batches, you can set the batch size instead.
    targets: if present, only return the sections whose title pages contain the target words. (OR condition)
    store_chunks_locally
    store_fp

    Outputs:
    returns a List of Chunck Objects | None
    Optional. Stores the chunck results as separate pdfs (studies) to a local filepath (folder).
    """

    import pymupdf
    import re
    from collections import Counter
    import os
    import statistics
    from itertools import islice

    class Chunk:
        def __init__(self,page_start:int,page_end:int,title:str|None): # page numbers are 1-based.
            self.page_start = page_start
            self.page_end = page_end
            self.title = title
            self.page_range = [n for n in range(page_start,page_end+1)]


    def study_preface_likeliness(text:str):
        """
        Returns:
        study_preface_score: 0 - 1
        features: {}
        """
        study_preface_keywords = [
            "study director",
            "study sponsor",
            "glp compliance",
            "quality assurance"
        ]

        additional_booost_words = [
            'signature',
            'date'
        ]

        Appendix_headings = {
            'appendix',
            'appendices'
        }

        if not text.strip():
            return 0.0, {}

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        lower = text.lower()

        score = 0
        features = {}

        # Count words
        if len(text.split(' ')) < 150:
            score += 2
            features['word count'] = 'Low'
        elif len(text.split(' ')) < 210:
            score += 1
            features['word count'] = 'Medium-Low'
        else: # Peanlize high word counts
            score -= 4
            features['word count'] = 'Medium to High'
       
        # Count occurance of key words
        if any(w.lower() in text.strip().lower() for w in study_preface_keywords):
            if any(w.lower() in text.strip().lower() for w in additional_booost_words):
                score += 8 # both preface keywords and boost words appear
            else:
                score += 4
            features['key words present'] = True
        else:
            features['key words present'] = False
        
        # Detect study-like section headers
        section_hot_words = ["introduction","1ntroduction","summary","sumnary","abstract","test substance","test material","procedure","results","discussion","references","methods","cell line","harvest","staining","coding"]
        sections = detect_sections(pdf_fp = None, text=text)
        num_hits = 0
        for s in sections:
            if any(w in s.title.lower() for w in section_hot_words):
                num_hits += 1
        
        if (num_hits < 1) and (any(w.lower() in text.strip().lower() for w in study_preface_keywords)): # no section-like titles
            score += 4
        
        # penalize study-like titles
        if num_hits > 1:
            score -= 1

        features['Num_study_like_titles'] = num_hits
        
        # penalize appendix titles
        if any(a in text.lower() for a in Appendix_headings):
            score -= 4
        
        # penalize TOC
        if is_toc(text):
            score -= 4
            features['is_toc'] = True
        else:
            features['is_toc'] = False
        
        # Peanlize Table-like pages
        if is_table(text):
            score -= 4
            features["table_like"] = True
        else:
            features["table_like"] = False

        probability = max(0, min(score / 8, 1))

        return probability, features

    def study_id(text:str):
        """
        Returns the study id (if found) from a page of text.
        """

        keywords = [
            'project number',
            'study number',
            'study id',
            'study no.',
            'study identifier',
            'project no.',
            'sponsor reference',
            'protocol no.',
            'protocol number',
            'report no.',
            'report number',
            'reference no.',
            'internal reference'
        ]

        pattern = re.compile(
        rf"""
        (?:{'|'.join(map(re.escape, keywords))})   # keyword
        \s*                                        # optional whitespace
        [:.#-]?                                    # optional punctuation
        \s*                                        # optional whitespace
        ([A-Za-z0-9][A-Za-z0-9._/-]*)              # capture the identifier
        """,
        re.IGNORECASE | re.VERBOSE,
        )

        matches = re.findall(pattern, text.lower())

        if len(matches)>0:
            match = matches[0].strip() # Get first match

        else:
            match = None
        
        return match

    def page_number(text:str):
        """
        Returns any specified page numbers from the text
        """

        main_patterns = re.compile(
            r'''^\s*
            (
                -\s*\d{1,4}\s*-        |   # -6-
                \[\s*\d{1,4}\s*\]      |   # [6]
                page\s+\d{1,4}         |   # page 6
                \bpage\s+\d{1,4}\s+of\s+\d{1,4}\b # page 6 of 14
            )
            \s*$''',
            re.IGNORECASE | re.VERBOSE,
        )

        other_pattern1 = re.compile(
            r'''^\s*
            (
                \(\s*\d{1,4}\s*\)         # (6)
            )
            \s*$''',
            re.IGNORECASE | re.VERBOSE,
        )
        
        other_pattern2 = re.compile(
            r'''^\s*
            (
                \d{1,4}\s*/\s*\d{1,4}     # 6/14  (current/total)
            )
            \s*$''',
            re.IGNORECASE | re.VERBOSE,
        )

        strong_matches = re.findall(main_patterns, text.lower())

        if len(strong_matches)>0: # any strong matches
            page_num = strong_matches[0].strip()

        else: # no strong matches
            weak_match1 = re.findall(other_pattern1, text.lower())
            if len(weak_match1) == 1: # exactly one match (ensures it's not part of a table)
                page_num = weak_match1[0].strip() 
            else:
                weak_match2 = re.findall(other_pattern2, text.lower())
                if len(weak_match2) == 1: # exactly one match (ensures it's not part of a table)
                    page_num = weak_match2[0].strip() 
                else:
                    page_num = None

        return page_num
    
    def summary_page(text:str):
        """
        Returns the likeliness of a page being a summary page (0 or 1) and a list of all matching section objects whose titles have been identified as summary titles.
        """
        sections = detect_sections(pdf_fp=None, text=text)
        score = 0
        study_keywords = ['study','report','results']
        summary_sections = []

        for ss in sections:
            s = ss.title.lower()
            if ('summary of' in s) or ('sumnary of' in s):
                # Pass on non-study summaries
                if not any(k in s for k in study_keywords):
                    continue
                else:
                    score += 1
                    summary_sections.append(ss)

            elif ('summary' in s) or ('sumnary' in s):
                # Reward 1-2 word summary titles (i.e. summary or summary (cont.))
                if len(s.split()) <= 2:
                    score += 1
                    summary_sections.append(ss)

            elif ('abstract' in s):
                if len(s.split()) <= 2:
                    score += 1
                    summary_sections.append(ss)
        
        probability = max(0, min(score / 1, 1))

        return probability, summary_sections

    def introduction(text:str):
        """
        Returns the likeliness of a page being an introduction page (0 or 1).
        """
        sections = detect_sections(pdf_fp = None, text=text)
        score = 0

        for ss in sections:
            s = ss.title.lower()

            if ('introduction' in s) or ('1troduction' in s):
                if len(s.split()) <= 2:
                    score += 1
        
        probability = max(0, min(score / 1, 1))

        return probability

    def split_pdf_by_ranges(input_path, output_folder, page_ranges):
        """
        Split a PDF into separate PDFs based on page index ranges.

        input_path: path to the source PDF
        output_folder: folder to save the split PDFs into (created if missing)
        page_ranges: list of (start, end) tuples, 1-indexed, end-inclusive.
        e.g. [(0, 3), (3, 7), (7, 10)]
        """
        os.makedirs(output_folder, exist_ok=True)

        src = pymupdf.open(input_path)
        filename = os.path.basename(input_path)
        pdf_name = os.path.splitext(filename)[0]

        total_pages = src.page_count

        for i, (start, end) in enumerate(page_ranges):
            end = min(end-1, total_pages)

            # insert_pdf copies a page range directly from src into a new doc
            out_doc = pymupdf.open()
            out_doc.insert_pdf(src, from_page=start-1, to_page=end)

            output_path = os.path.join(output_folder, f"{pdf_name}_split_{start}_{end}.pdf")
            out_doc.save(output_path)
            out_doc.close()

            print(f"Saved {output_path} ({end - start} pages)")

        src.close()
    
    pages = [] # cleaned text
    chunks = []
    page_boundary_scores = {} # page number (1-based), score (float 0-1, or -1(for TOC pages))

    with pymupdf.open(pdf_fp) as doc:
        ps = [p.get_text() for p in doc]
        for p in ps:
            pages.append(clean_pymupdf_text(p))

    scores = {} # page number: {'toc_score':int(0,1),'title_page_score':float, 'study_preface_score':float, 'pdf_page_num':int|None, 'study_id':str|None, 'summary_score':int(0,1), 'intro_score':int(0,1)}

    #------------------------------------------------
    # First assign individual categoric scores to all pages - allows positive and negative lookahead later on. Also stores each page as a document object
    #------------------------------------------------

    for pdx in range(len(pages)):

        p = pages[pdx]
        if is_toc(p):
            scores[pdx+1] = {'toc_score':1,'title_page_score':0.0, 'study_preface_score':0.0, 'pdf_page_num':None, 'study_id':None, 'summary_score':0, 'intro_score':0}

            continue
        
        title_page_score, _ = title_page_likeliness(p)
        study_preface_score, _ = study_preface_likeliness(p)
        studyid = study_id(p)
        pagenum = page_number(p)
        summary_score, _ = summary_page(p)
        intro_score = introduction(p)
        
        scores[pdx+1] = {'toc_score':0,'title_page_score':title_page_score, 'study_preface_score':study_preface_score, 'pdf_page_num':pagenum, 'study_id':studyid, 'summary_score':summary_score, 'intro_score':intro_score}

    #------------------------------------------------
    # Evaluate page-by-page
    #------------------------------------------------
    title_pages = [] # page numbers only
    summaries = [] # page numbers only
    introductions = [] # page numbers only
    for s in scores.keys():
        value = scores[s]

        toc_score = value['toc_score']
        if toc_score >0:
            page_boundary_scores[s] = 0.7 # TOCs have a score of 0.7 by default (they are boundary pages, but could belong to the study or appendix)
            continue
    
        title_page_score = value['title_page_score']
        study_preface_score = value['study_preface_score']
        page_num = value['pdf_page_num']
        study_id = value['study_id']
        summary_score = value['summary_score']
        intro_score = value['intro_score']

        boundary_score = 0
        boost = 0
        additional_boosts = 0

        # 
        # Title Pages
        # 
        if title_page_score > 0.60:
            boundary_score += 0.70*title_page_score
            
            # Positive lookaheads
            lookahead_range = range(s+1,s+9) # look ahead 8 pages
        
            for l in lookahead_range:
                try:
                    lvalue = scores[l]
                    if lvalue['toc_score'] >0:
                        boundary_score += 0.0375
                        continue
                    
                    boundary_score += 0.0375* (lvalue['study_preface_score'] + lvalue['summary_score'] + lvalue['intro_score'])
                except:
                    continue # page does not exist (out of bounds)
            
            # Bonus boost - checking for repeat title pages
            if boundary_score > 0.70:
                if (s not in title_pages) and (len(title_pages)>=1):
                    boost += 1
            
            boundary_score += 0.1*boost # boost by 10% if repeat
            title_pages.append(s)

        # 
        # Study Preface
        # 
        elif study_preface_score > 0.50:
            boundary_score  += study_preface_score * 0.70

            # Positive and negative lookahead
            lookahead_range = range(s-3,s+6) # look behind 3 pages and ahead 5 pages
            for l in lookahead_range:
                if l == s:
                    continue # skip current page
                try:
                    lvalue = scores[l]
                    if lvalue['toc_score'] >0:
                        boundary_score += 0.0375
                        continue
                    boundary_score += 0.0375*(lvalue['title_page_score'] + lvalue['summary_score'] + lvalue['intro_score'])
                
                except:
                    continue
            
        # 
        # Summary
        # 
        elif summary_score > 0:
            boundary_score += summary_score*0.7

            # Positive and Negative lookahead
            lookahead_range = range(s-5,s+4) # look behind 5 pages and ahead 3 pages

            for l in lookahead_range:
                if l == s:
                    continue
                try:
                    lvalue = scores[l]
                    if lvalue['toc_score'] >0:
                        boundary_score += 0.0375
                        continue
                    boundary_score += 0.0375*(lvalue['title_page_score'] + lvalue['study_prface_score'] + lvalue['intro_score'])
                except:
                    continue

            # Bonus 
            if (s not in summaries) and (len(summaries) >=1):
                boost += 1
            
            boundary_score += 0.1*boost 
            summaries.append(s)

        # 
        # Introduction
        # 
        elif intro_score > 0:
            boundary_score += 0.7*intro_score

            # Negative lookahead
            lookahead_range = range(s-8,s) # look behind 8 pages

            for l in lookahead_range:
                try:
                    lvalue = scores[l]
                    if lvalue['toc_score'] >0:
                        boundary_score += 0.0375
                        continue
                    boundary_score += 0.0375*(lvalue['title_page_score'] + lvalue['study_prface_score'] + lvalue['summary_score'])
                except:
                    continue
            
            # Bonus
            if (s not in introductions) and (len(introductions)>=1):
                boost += 1
            
            boundary_score += 0.1*boost
            introductions.append(s)

        # 
        # Additional Bonuses
        # 

        # 1. Reset page numbers
        if page_num:
            lookahead_range = range(s-15,s)
            for l in lookahead_range:
                try:
                    lvalue = scores[l]
                    page_numl = lvalue['pdf_page_num']
                    if (page_numl) and (page_numl in (range(page_num-l-3, page_num-l+3))): # 3-page wiggle room for inserts
                        pass 
                    elif not page_numl:
                        pass
                    else:
                        additional_boosts += 1
                except:
                    continue
        
        # 2. New Study id
        if studyid:
            lookahead_range = range(s-15,s)
            for l in lookahead_range:
                try:
                    lvalue = scores[l]
                    studyidl = lvalue['study_id']
                    if (studyidl) and (studyidl.lower().strip() != studyid.lower().strip()): # non-matching study ids
                        additional_boosts += 1

                except:
                    continue

        # Normalize additional bonuses
        boundary_score += 0.2*max(0, min(additional_boosts / 14, 1)) # max bonus score is 14 (7 from each case). A bonus of 1 will add 20% to the overall score.

        page_boundary_scores[s] = boundary_score
    
    #------------------------------------------------
    # Determine Report Boundaries by batching
    #------------------------------------------------
    boundaries = {} # start page number, page text
    batches = [] # list of dicts --> [ {2 : 0.2 , 3 : 0.25 , 4 : 0.8 , ...} ... ]

    if num_batches:
        batch_size = len(page_boundary_scores)//num_batches
        it = iter(page_boundary_scores.items())
        for i in range(0, len(page_boundary_scores), batch_size):
            batches.append({k: v for k, v in islice(it, batch_size)})

    elif batch_size:
        batch_size = batch_size
        it = iter(page_boundary_scores.items())
        for i in range(0, len(page_boundary_scores), batch_size):
            batches.append({k: v for k, v in islice(it, batch_size)})
    else:
        raise ValueError("Please specify the number of batches or batch size for the chunking algorithm to work!")

    batch_scores = {} #start_page, avg score

    for b in batches:
        avg = sum(list(b.values()))/len(b)
        start_page = list(b)[0]
        batch_scores[start_page] = avg
    
    min_score = min(list(batch_scores.values()))
    LoD = min_score + 2 * statistics.pstdev(list(batch_scores.values()))
    lod_batches = [] #lists of Idxs of items in the list batches

    # Isolate detectable boundary batches
    for bdx in range(len(batches)):
        b = batches[bdx]
        if ( sum(list(b.values()))/len(b) ) >= LoD:
            lod_batches.append(bdx)

    if len(lod_batches) <= 1: # none or only 1 batch exceeded LoD -> Not a multi-study report
        return None

    combined_batches = [] # List of dicts {page_num: boundary_score ... }
    # combine adjacent batches
    tail = -1
    
    for bdx in lod_batches:
        start_page = list(batches[bdx])[0]

        # Failsafe: reject boundary pages close to end of report
        if start_page >= (len(pages) + 1 - 10): # within 10 pages of the end of report
            continue # skip this lod batch and do not add to combined batches

        combined_idxs = [bdx]
        combined = {}

        if bdx <= tail:
            continue

        # look ahead 5 batches
        for i in range(5):
            idx = bdx + i + 1
            try:
                batch_ind = lod_batches[idx] # get the next lod batch
                lookahead_start_page = list(batches[batch_ind])[0]
                if lookahead_start_page <= start_page + 15: # Combine batches if start pages are within n pages of eachother
                    combined_idxs.append(idx)
                    tail = idx
            except:
                continue
        
        # combine
        if len(combined_idxs) >1:
            for i in combined_idxs:
                combined.update(batches[i])
        else:
            combined = batches[bdx]
        
        combined_batches.append(combined)
    
    # Evaluate score rises and dips within combined dicts
    boundary_pages = []

    for dic in combined_batches:
        changes = {} # {page number, dip (-1) or rise (1)}

        vals = list(dic.values())

        for vdx in range(len(vals)-1):
            v = vals[vdx]
            v1 = vals[vdx+1]

            if v <= min(list(batch_scores.values())): # score is noise.
                if (v1 - v) >= 1.5*statistics.pstdev(list(dic.values())): # can only go up from here.
                    changes[list(dic)[vdx+1]] = 1
                else:
                    continue

            elif (v >= max(list(batch_scores.values()))-0.1) or (v >= 0.85): # within 0.1 of the max score of the batch or score is >= 0.85
                break # assume this page is the start page

            elif (v1) <= (v*0.5): # Score drops by equal to or more than half
                changes[list(dic)[vdx]] = -1 # page before dip
            
            elif (v1) >= (v*2): # Score rises by equal to or more than half
                changes[list(dic)[vdx+1]] = 1 # start page of rise

        if len(changes)>0:
            if list(changes.values())[0] < 0:
                boundary_pages.append(list(changes)[0]) # if the changes begin with a dip, then we can assume that the batch begins with the start page of the boundary.
            
            else:
                for key in changes:
                    val = changes[key]

                    if val > 0: # if the changes begin with a rise, then the batch does not begin with a boundary page, so the first rise in the batch must be the start of the boundary.
                        boundary_pages.append(key)
                        break
        
        else: # No changes detected in the combined batch; assume that the window was not big enough to capture change and that the first page of the window is the start of a study boundary.
            boundary_pages.append(list(dic)[0])
    
    # finally add the boundary pages to the boundaries list
    if len(boundary_pages) > 1:
        for b in boundary_pages:
            boundaries[b] = pages[b-1]

    else: # Not a multi-study report
        return None
    
    #------------------------------------------------
    # Split report by boundaries
    #------------------------------------------------
    
    for bdx in range(len(boundaries)):
        key = list(boundaries)[bdx]
        value = list(boundaries.values())[bdx]

        if bdx+1 < len(boundaries):
            next_page = list(boundaries)[bdx+1]
        else:
            next_page = len(pages)+1 # last page of pdf
        
        chunks.append(Chunk(
            page_start = key,
            page_end = next_page,
            title = value
        ))
    
    #------------------------------------------------
    # Return chunks and optionally store as seperate pdfs
    #------------------------------------------------
    if targets:
        chunks2 = chunks.copy()
        for c in chunks2:
            title_page = c.title.lower()
            if any(t.lower() in title_page for t in targets):
                continue
            else:
                chunks.remove(c)

    if store_chunks_locally:
        if store_fp:
            for c in chunks:
                page_range = tuple(c.page_start,c.page_end)
                split_pdf_by_ranges(
                    pdf_fp,
                    store_fp,
                    page_range
                )
        else:
            raise ValueError("No filepath to store pdf chunks specified! Please specify a folder in which to store the chunks.")
    
    return chunks

def chunk_integrated(pdf_fp:str):
    """
    Integrated-study detection and chunking for multi-study regulatory PDFs
    (e.g. NRA/APVMA-style Public Release Summaries that stitch together a
    toxicological assessment, residues assessment, environmental assessment,
    OH&S assessment, and efficacy assessment for one chemical, each condensing
    many individual underlying studies).

    Two things verified against real samples of this doc type that shaped the
    approach below:

    1. The TOC often lists ONLY high-level sections, flat, with title and page
        number on separate physical lines (not "Title ....... 12" on one line).
        There's nothing to key a hierarchy off of in the TOC itself.
    2. The "many individual studies condensed under one heading" structure
        lives in the BODY text as short heading lines ("Acute Studies",
        "Long-Term Studies", "Genotoxicity"...), not as TOC subentries. So
        both is_integrated()'s clustering signal and get_sections()'s chunk
        boundaries are found by scanning body text within each high-level
        section's page range, not by parsing TOC indentation.

    Assumes access to your existing `is_toc()` classifier -- pass it in as
    `is_toc_fn`. Run `clean_pdf_text()` over Section.content afterwards if you
    want OCR-noise cleanup; that's left out here to keep this self-contained.

    Note: this only works on PDFs with an extractable text layer. Scanned pages
    with no OCR'd text layer (checked via `has_text_layer()`) will silently
    produce zero sections -- run OCR first for those.
    """

    import re
    from dataclasses import dataclass
    from typing import Optional

    import pymupdf


    @dataclass
    class Section:
        page_num: int
        content: str
        title: str  # "High Level -> Low Level" (or just "High Level" if no body subheadings found)


    # ---------------------------------------------------------------- patterns

    # Same-line TOC style: "Foreword ....... iii" or "Foreword          iii"
    DOT_LEADER_RE = re.compile(
        r"^(?P<indent>\s*)(?P<title>.+?)\s*\.{2,}\s*(?P<page>[ivxlcdm\d]+)\s*$",
        re.IGNORECASE,
    )
    PAGE_TAIL_RE = re.compile(
        r"^(?P<indent>\s*)(?P<title>.+?)\s{2,}(?P<page>[ivxlcdm\d]+)\s*$",
        re.IGNORECASE,
    )
    # Line-pair TOC style: title on its own line, page number alone on the next.
    # Deliberately strict (proper roman-numeral structure, length>=2): a loose
    # "any combo of ivxlcdm letters" match false-positives hard on this document
    # type, since single-letter unit abbreviations in a glossary ("d" = day,
    # "L" = litre, "h" = hour...) are themselves valid lone roman numerals.
    ROMAN_LINE_RE = re.compile(
        r"^(?=[mdclxvi]{2,}$)m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$",
        re.IGNORECASE,
    )
    ARABIC_LINE_RE = re.compile(r"^\d{1,4}$")

    SUBSECTION_KEYWORD_RE = re.compile(r"\b(stud(y|ies)|summary|assessment)\b", re.IGNORECASE)
    ASSESSMENT_TOPIC_RE = re.compile(
        r"toxicolog|environmental|residues|occupational|efficacy|assessment", re.IGNORECASE
    )


    # ---------------------------------------------------------------- helpers

    def _roman_or_int(token: str) -> int:
        """Arabic -> int. Roman numerals (front matter) -> negative, so they
        sort before page 1 without colliding with real page numbers."""
        token = token.strip().lower()
        if token.isdigit():
            return int(token)
        roman_map = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        if token and all(c in roman_map for c in token):
            total, prev = 0, 0
            for c in reversed(token):
                v = roman_map[c]
                total += -v if v < prev else v
                prev = v
            return -1000 + total
        return -9999


    def has_text_layer(doc, min_chars: int = 200) -> bool:
        """Cheap sanity check before running any of this: scanned pages with no
        OCR text layer will otherwise silently yield zero sections."""
        total = sum(len(doc[i].get_text().strip()) for i in range(min(doc.page_count, 10)))
        return total >= min_chars


    def _looks_like_toc(text: str) -> bool:
        """Fallback TOC page check, used only if no is_toc_fn is supplied."""
        header_hit = bool(re.search(r"\bcontents\b", text, re.IGNORECASE))
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        same_line_hits = sum(1 for l in lines if DOT_LEADER_RE.match(l) or PAGE_TAIL_RE.match(l))
        pair_hits = sum(1 for l in lines if ROMAN_LINE_RE.match(l) or ARABIC_LINE_RE.match(l))
        return header_hit or same_line_hits >= 4 or pair_hits >= 4


    def get_toc_text(doc, is_toc_fn=None, max_pages_to_scan: int = 15) -> Optional[str]:
        """Locate TOC page(s) in the first `max_pages_to_scan` pages and return
        the concatenated raw text, page order preserved. Plug in your existing
        `is_toc()` via `is_toc_fn`; otherwise falls back to `_looks_like_toc`."""
        hits = []
        for i in range(min(max_pages_to_scan, doc.page_count)):
            text = doc[i].get_text()
            matched = is_toc_fn(text) if is_toc_fn is not None else _looks_like_toc(text)
            if matched:
                hits.append((i, text))
        if not hits:
            return None
        return "\n".join(t for _, t in sorted(hits, key=lambda x: x[0]))


    def parse_toc_entries(toc_text: str) -> list[dict]:
        """Parse TOC raw text into a flat list of {"title", "page", "level"}
        dicts, handling both same-line ("Title ... 12") and line-pair (title,
        then a lone page-number line) styles.

        Hierarchy: if indentation varies across same-line entries, that's used
        to split high/low. Otherwise (flat TOC, or line-pair style with no
        indent info) every entry is marked "high" -- low-level subsections get
        found later from body headings, see _detect_body_subheadings().
        """
        raw_lines = [l.strip() for l in toc_text.splitlines()]
        lines = [l for l in raw_lines if l]
        entries = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.lower() in ("contents", "table of contents"):
                i += 1
                continue
            m = DOT_LEADER_RE.match(line) or PAGE_TAIL_RE.match(line)
            if m:
                title = re.sub(r"\s+", " ", m.group("title")).strip()
                if title:
                    entries.append({"title": title, "page": _roman_or_int(m.group("page")), "indent": len(m.group("indent"))})
                i += 1
                continue
            if i + 1 < len(lines) and (ARABIC_LINE_RE.match(lines[i + 1]) or ROMAN_LINE_RE.match(lines[i + 1])):
                entries.append({"title": line, "page": _roman_or_int(lines[i + 1]), "indent": 0})
                i += 2
                continue
            i += 1

        if not entries:
            return []

        indents = {e["indent"] for e in entries}
        if len(indents) > 1:
            min_indent = min(indents)
            for e in entries:
                e["level"] = "high" if e["indent"] <= min_indent else "low"
        else:
            for e in entries:
                e["level"] = "high"  # flat TOC -- resolved via body headings instead
        return entries


    def _looks_like_heading_line(line: str, max_words: int = 6) -> bool:
        """Heuristic for a standalone body heading (e.g. 'Acute Studies',
        'Genotoxicity'): short, starts with a capital letter, doesn't end in
        sentence punctuation. Verified against real samples to have ~zero false
        positives within body prose (paragraph line-wraps almost always end in
        punctuation or start lowercase)."""
        line = line.strip()
        if not line or line[-1] in ".,;:":
            return False
        words = line.split()
        if not (1 <= len(words) <= max_words):
            return False
        return words[0][0].isupper()


    def _detect_body_subheadings(doc, start_page: int, end_page: int) -> list[dict]:
        """Scan pages [start_page, end_page] (INCLUSIVE -- callers must pass the
        last page belonging to this section, not the next section's start page,
        or the boundary heading/content bleeds across sections) for standalone
        heading-like lines. Returns [{"title", "page"}], in reading order."""
        found = []
        end_page = min(end_page, doc.page_count - 1)
        for p in range(start_page, max(end_page, start_page) + 1):
            for line in doc[p].get_text().splitlines():
                if _looks_like_heading_line(line):
                    found.append({"title": line.strip(), "page": p})
        return found


    def _has_clustered_study_subsections(headings: list[dict], min_hits: int = 3, max_page_span: int = 6) -> bool:
        """True if some run of >=min_hits headings matching SUBSECTION_KEYWORD_RE
        falls within a max_page_span window -- proxy for "many individual
        studies condensed under one heading"."""
        hits = sorted(
            (h for h in headings if SUBSECTION_KEYWORD_RE.search(h["title"])),
            key=lambda h: h["page"],
        )
        if len(hits) < min_hits:
            return False
        for i in range(len(hits) - min_hits + 1):
            window = hits[i : i + min_hits]
            if window[-1]["page"] - window[0]["page"] <= max_page_span:
                return True
        return False


    # Body major-heading style in this doc family: the TOC title reappears in
    # the body as a standalone ALL-CAPS line (e.g. TOC "Toxicological
    # Assessment" -> body "TOXICOLOGICAL ASSESSMENT"). Matches the all-caps
    # heading heuristic already used elsewhere in the pipeline.
    ALLCAPS_HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 ,&/'\-]{3,80}$")


    def _resolve_page_index(doc, heading_title: str, search_from: int = 0) -> Optional[int]:
        """TOC page numbers are unreliable to use directly -- printed page
        numbers usually don't match pymupdf's 0-indexed pages once front matter
        (roman-numbered) offsets things, and the offset isn't constant. Instead,
        search forward from `search_from` (monotonic, since TOC order == body
        order) for the ALL-CAPS body heading matching this TOC title exactly."""
        norm_title = re.sub(r"\s+", " ", heading_title).strip().upper()
        for p in range(search_from, doc.page_count):
            for line in doc[p].get_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                if re.sub(r"\s+", " ", line).upper() == norm_title and ALLCAPS_HEADING_RE.match(line):
                    return p
        return None  # not found as a body heading -- drop this TOC entry rather than guess


    def _extract_span_text(doc, start_title, start_page, end_title, end_page) -> str:
        """Text from `start_title`'s occurrence on `start_page` up to (not
        including) `end_title`'s occurrence on `end_page`. Falls back to the
        full page range if either heading can't be located verbatim."""
        blob = "\n".join(doc[p].get_text() for p in range(start_page, end_page + 1))

        start_idx = 0
        if start_title:
            m = re.search(re.escape(start_title[:20]), blob, re.IGNORECASE)
            if m:
                start_idx = m.end()

        end_idx = len(blob)
        if end_title:
            m = re.search(re.escape(end_title[:20]), blob[start_idx:], re.IGNORECASE)
            if m:
                end_idx = start_idx + m.start()

        return blob[start_idx:end_idx].strip()


    def _resolved_high_level_sections(doc, toc: Optional[str]) -> list[dict]:
        """TOC entries -> [{"title", "page_idx"}], with real page indices
        resolved by searching forward through the body in TOC order (so
        front-matter pagination offsets can't desync page numbers). Entries
        that can't be found as a body heading are dropped. Returns [] if no
        usable TOC."""
        if not toc:
            return []
        entries = [e for e in parse_toc_entries(toc) if e["level"] == "high"]
        resolved = []
        search_from = 0
        for e in entries:
            page_idx = _resolve_page_index(doc, e["title"], search_from=search_from)
            if page_idx is not None:
                resolved.append({"title": e["title"], "page_idx": page_idx})
                search_from = page_idx
        return resolved


    # ---------------------------------------------------------------- 1 & 1.a

    def is_integrated(pdf_fp: str, is_toc_fn=None) -> float:
        """
        Score 0-1: likelihood `pdf_fp` is an integrated multi-study report
        (vs. a single stand-alone study).

        Signals (additive, capped at 1.0):
        - TOC has >=2 distinct high-level "assessment"-style sections (0.5)
        - TOC mentions "assessment" >=3 times across high-level titles (0.2)
        - Within some high-level section's page range, body headings cluster
            >=3 study/summary keywords in a small page span (0.3)
        Falls back to a whole-document body-heading scan if there's no TOC.
        """
        doc = pymupdf.open(pdf_fp)
        try:
            if not has_text_layer(doc):
                return 0.0  # no extractable text -- needs OCR first, can't assess

            toc_text = get_toc_text(doc, is_toc_fn=is_toc_fn)
            score = 0.0

            if toc_text:
                high_sections = _resolved_high_level_sections(doc, toc_text)
                high_titles = [h["title"] for h in high_sections]

                assessment_sections = {t for t in high_titles if ASSESSMENT_TOPIC_RE.search(t)}
                if len(assessment_sections) >= 2:
                    score += 0.5

                assessment_mentions = sum(1 for t in high_titles if re.search(r"\bassessment\b", t, re.IGNORECASE))
                if assessment_mentions >= 3:
                    score += 0.2

                for i, h in enumerate(high_sections):
                    end_page = (high_sections[i + 1]["page_idx"] - 1) if i + 1 < len(high_sections) else doc.page_count - 1
                    body_headings = _detect_body_subheadings(doc, h["page_idx"], end_page)
                    if _has_clustered_study_subsections(body_headings):
                        score += 0.3
                        break
            else:
                body_headings = _detect_body_subheadings(doc, 0, doc.page_count - 1)
                if _has_clustered_study_subsections(body_headings):
                    score += 0.4
                assessment_like = sum(1 for h in body_headings if ASSESSMENT_TOPIC_RE.search(h["title"]))
                if assessment_like >= 2:
                    score += 0.4

            return min(score, 1.0)
        finally:
            doc.close()


    # ---------------------------------------------------------------- 2, 3, 4

    def get_sections(toc: Optional[str], pdf_fp: Optional[str] = None, is_toc_fn=None) -> list[Section]:
        """
        Returns a flat list of Section objects. For each TOC high-level
        section, scans its page range for body subheadings (e.g. "Acute
        Studies") and chunks content between them; if none are found, the
        whole high-level section becomes a single Section.

        If `toc` is None, `pdf_fp` is required and high-level sections are
        approximated from the whole-document body-heading scan instead. -> Not yet implemented AUG 2026
        """
        if not pdf_fp:
            raise ValueError("pdf_fp is required to extract section content")

        doc = pymupdf.open(pdf_fp)
        try:
            if not has_text_layer(doc):
                return []  # scanned, no OCR text layer -- nothing to chunk

            high_sections = _resolved_high_level_sections(doc, toc) if toc else []
            if not high_sections:
                # No usable TOC: treat the whole doc as one "high-level" span
                # and rely entirely on body headings.
                high_sections = [{"title": None, "page_idx": 0}]

            sections: list[Section] = []
            for i, h in enumerate(high_sections):
                start_page = h["page_idx"]
                end_page = (high_sections[i + 1]["page_idx"] - 1) if i + 1 < len(high_sections) else doc.page_count - 1
                end_page = max(end_page, start_page)

                # Body-heading subsectioning only pays off (and only avoids false
                # positives) inside narrative "assessment" sections -- front
                # matter, glossaries, abbreviation lists, and labelling/rate
                # tables are full of short capitalized standalone lines that
                # aren't real subheadings, so leave those as one whole chunk.
                is_assessment_section = bool(h["title"] and ASSESSMENT_TOPIC_RE.search(h["title"]))
                subheadings = _detect_body_subheadings(doc, start_page, end_page) if is_assessment_section else []
                if h["title"]:
                    subheadings = [s for s in subheadings if s["title"].strip().lower() != h["title"].strip().lower()]

                if not subheadings:
                    content = "\n".join(doc[p].get_text() for p in range(start_page, end_page + 1)).strip()
                    sections.append(Section(page_num=start_page + 1, content=content, title=h["title"] or f"Section (p{start_page + 1})"))
                    continue

                for j, sub in enumerate(subheadings):
                    sub_start_page = sub["page"]
                    sub_end_page = subheadings[j + 1]["page"] if j + 1 < len(subheadings) else end_page
                    content = _extract_span_text(
                        doc, sub["title"], sub_start_page,
                        subheadings[j + 1]["title"] if j + 1 < len(subheadings) else None,
                        sub_end_page,
                    )
                    combined_title = f"{h['title']} -> {sub['title']}" if h["title"] else sub["title"]
                    sections.append(Section(page_num=sub_start_page + 1, content=content, title=combined_title))

            return sections
        finally:
            doc.close()


    # ---------------------------------------------------------------- orchestrator

    def chunk_integrated(pdf_fp: str, is_toc_fn=None) -> list[Section]:
        """
        1. Detect whether the study is integrated (TOC-based, or body-heading
        scan fallback if there's no TOC -> Not yet implemented AUG 2026).
        2/3/4. If integrated, chunk each high-level TOC section by its body
        subheadings.

        Returns [] if the report doesn't look integrated, or has no extractable
        text layer -- callers should treat [] as "not integrated / needs OCR
        first", not "empty document."
        """
        doc = pymupdf.open(pdf_fp)
        toc_text = get_toc_text(doc, is_toc_fn=is_toc_fn)
        doc.close()

        if is_integrated(pdf_fp, is_toc_fn=is_toc_fn) < 0.5:
            return []

        return get_sections(toc_text, pdf_fp, is_toc_fn=is_toc_fn)
    
    secs = chunk_integrated(pdf_fp=pdf_fp, is_toc_fn=is_toc)

    return secs

