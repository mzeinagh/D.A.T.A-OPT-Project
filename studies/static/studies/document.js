/* Extracted-text reader: search, clean/raw switch, keyword highlighting,
   page filters, and tables from OCR shown as tables. Runs on every
   [data-reader] block on the page. Only reads text already on the page. */
(function () {
    "use strict";

    function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

    function isTableLine(line) { return /^\s*\|.*\|\s*$/.test(line); }
    function isSeparatorRow(line) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line); }

    // Appends text to parent, wrapping regex matches in <mark>. Group 1 of
    // the regex is a search match, group 2 a keyword match.
    function appendHighlighted(parent, text, regex) {
        if (!regex || !text) { parent.appendChild(document.createTextNode(text)); return; }
        var last = 0;
        regex.lastIndex = 0;
        var m;
        while ((m = regex.exec(text)) !== null) {
            if (m[0] === "") { regex.lastIndex++; continue; }
            if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
            var mark = document.createElement("mark");
            mark.className = m[1] !== undefined ? "hit" : "kw";
            mark.textContent = m[0];
            parent.appendChild(mark);
            last = m.index + m[0].length;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }

    // Rebuilds one page's text: Markdown-style "| a | b |" blocks become
    // tables, everything else stays as wrapped plain text.
    function renderText(el, regex) {
        var src = el._src;
        el.textContent = "";
        var lines = src.split("\n");
        var buffer = [];
        function flushText() {
            if (!buffer.length) return;
            var div = document.createElement("div");
            div.className = "text-block";
            appendHighlighted(div, buffer.join("\n"), regex);
            el.appendChild(div);
            buffer = [];
        }
        var i = 0;
        while (i < lines.length) {
            if (isTableLine(lines[i]) && i + 1 < lines.length && isTableLine(lines[i + 1])) {
                flushText();
                var wrap = document.createElement("div");
                wrap.className = "table-wrap";
                var table = document.createElement("table");
                table.className = "text-table";
                var first = true;
                while (i < lines.length && isTableLine(lines[i])) {
                    if (!isSeparatorRow(lines[i])) {
                        var cells = lines[i].trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
                        var tr = document.createElement("tr");
                        cells.forEach(function (c) {
                            var cell = document.createElement(first ? "th" : "td");
                            appendHighlighted(cell, c.trim(), regex);
                            tr.appendChild(cell);
                        });
                        table.appendChild(tr);
                        first = false;
                    }
                    i++;
                }
                wrap.appendChild(table);
                el.appendChild(wrap);
            } else {
                buffer.push(lines[i]);
                i++;
            }
        }
        flushText();
    }

    function initReader(reader) {
        var kwScript = reader.querySelector('script[type="application/json"]');
        var keywords = [];
        try { keywords = kwScript ? JSON.parse(kwScript.textContent) : []; } catch (e) { keywords = []; }

        var search = reader.querySelector("[data-search]");
        var countEl = reader.querySelector("[data-count]");
        var kwToggle = reader.querySelector("[data-keywords-toggle]");
        var onlyMatches = reader.querySelector("[data-only-matches]");
        var filter = reader.querySelector("[data-filter]");
        var items = Array.prototype.slice.call(reader.querySelectorAll("[data-page-item]"));
        var texts = Array.prototype.slice.call(reader.querySelectorAll("[data-text]"));
        var mode = "clean";
        var hits = [];
        var current = -1;

        texts.forEach(function (el) { el._src = el.textContent; });
        reader.classList.add("js");

        var summary = reader.querySelector("[data-summary]");
        if (summary) {
            var ocr = items.filter(function (li) { return li.dataset.ocr; }).length;
            var tables = items.filter(function (li) { return li.dataset.table; }).length;
            var parts = [];
            if (ocr) parts.push(ocr + " read by OCR");
            if (tables) parts.push(tables + " with tables");
            summary.textContent = parts.length ? "· " + parts.join(" · ") : "";
        }

        function buildRegex() {
            var term = search.value.trim();
            var parts = [];
            var searchPart = term.length >= 2 ? "(" + escapeRegExp(term) + ")" : "()(?!)";
            var useKw = kwToggle && kwToggle.checked && keywords.length;
            if (term.length < 2 && !useKw) return { regex: null, term: "" };
            parts.push(searchPart);
            if (useKw) {
                parts.push("(\\b(?:" + keywords.map(escapeRegExp).join("|") + ")\\b)");
            }
            return { regex: new RegExp(parts.join("|"), "gi"), term: term.length >= 2 ? term : "" };
        }

        function apply() {
            var built = buildRegex();
            hits = [];
            current = -1;
            items.forEach(function (li) {
                var clean = li.querySelector('[data-text="clean"]');
                var raw = li.querySelector('[data-text="raw"]');
                var same = li.querySelector("[data-text-same]");
                var active = mode === "raw" && raw ? raw : clean;
                if (clean) clean.hidden = active !== clean;
                if (raw) raw.hidden = active !== raw;
                if (same) same.hidden = mode !== "raw";
                renderText(active, built.regex);
                var pageHits = Array.prototype.slice.call(active.querySelectorAll("mark.hit"));
                hits = hits.concat(pageHits);
                var badge = li.querySelector("[data-matches]");
                badge.textContent = built.term ? (pageHits.length ? pageHits.length + " match" + (pageHits.length > 1 ? "es" : "") : "no matches") : "";
                badge.classList.toggle("has", pageHits.length > 0);
                li._hasHits = pageHits.length > 0;
                if (built.term && pageHits.length) li.querySelector("details").open = true;
            });
            countEl.textContent = built.term ? (hits.length ? hits.length + " found" : "Not found") : "";
            applyFilter(Boolean(built.term));
        }

        function applyFilter(searching) {
            var f = filter.value;
            items.forEach(function (li) {
                var show = true;
                if (f === "ocr" && !li.dataset.ocr) show = false;
                if (f === "table" && !li.dataset.table) show = false;
                if (onlyMatches.checked && searching && !li._hasHits) show = false;
                li.hidden = !show;
            });
        }

        function go(step) {
            var visible = hits.filter(function (h) { return !h.closest("[data-page-item]").hidden; });
            if (!visible.length) return;
            if (current >= 0 && visible[current]) visible[current].classList.remove("current");
            current = (current + step + visible.length) % visible.length;
            var target = visible[current];
            var sec = target.closest("details");
            sec.open = true;
            focusPage(sec);
            target.classList.add("current");
            target.scrollIntoView({ block: "center" });
            countEl.textContent = (current + 1) + " of " + visible.length;
        }

        var timer = null;
        search.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(apply, 200); });
        search.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); if (!hits.length) apply(); go(e.shiftKey ? -1 : 1); }
        });
        reader.querySelector("[data-next]").addEventListener("click", function () { go(1); });
        reader.querySelector("[data-prev]").addEventListener("click", function () { go(-1); });
        reader.querySelectorAll("[data-mode]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                mode = btn.dataset.mode;
                reader.querySelectorAll("[data-mode]").forEach(function (b) { b.setAttribute("aria-pressed", String(b === btn)); });
                apply();
            });
        });
        if (kwToggle) kwToggle.addEventListener("change", apply);
        onlyMatches.addEventListener("change", function () { applyFilter(Boolean(search.value.trim().length >= 2)); });
        filter.addEventListener("change", function () { applyFilter(Boolean(search.value.trim().length >= 2)); });
        reader.querySelector("[data-expand]").addEventListener("click", function () {
            items.forEach(function (li) { if (!li.hidden) li.querySelector("details").open = true; });
        });
        reader.querySelector("[data-collapse]").addEventListener("click", function () {
            items.forEach(function (li) { li.querySelector("details").open = false; });
        });

        reader.addEventListener("click", function (e) {
            var summary = e.target.closest(".page-sec > summary");
            if (!summary) return;
            var sec = summary.parentElement;
            setTimeout(function () { if (sec.open) focusPage(sec); }, 0);
        });

        apply();
    }

    // Tells the PDF pane (if any) which page the reader is looking at.
    function focusPage(sec) {
        sec.dispatchEvent(new CustomEvent("reader:page", { bubbles: true, detail: { page: sec.dataset.pageNumber } }));
    }

    function openFromHash() {
        if (!location.hash) return;
        var el = document.getElementById(location.hash.slice(1));
        if (el && el.tagName === "DETAILS") {
            el.open = true;
            el.classList.add("flash");
            el.scrollIntoView({ block: "start" });
            focusPage(el);
        }
    }

    // The original PDF beside the text, in the browser's own PDF viewer.
    // The viewer can't report which page it shows, so the text leads:
    // opening a page in the text (or a search match, or a link to #page-N)
    // moves the PDF to that page.
    function initPdfPane(layout) {
        var url = layout.dataset.pdfUrl;
        var pane = layout.querySelector("[data-pdf-pane]");
        var toggle = document.querySelector("[data-pdf-toggle]");
        if (!url || !pane || !toggle) return;
        var frame = pane.querySelector("[data-pdf-frame]");
        var label = pane.querySelector("[data-pdf-page]");
        var sections = Array.prototype.slice.call(layout.querySelectorAll("[data-page-number]"));
        var numbers = sections.map(function (s) { return parseInt(s.dataset.pageNumber, 10); }).filter(function (n) { return !isNaN(n); });
        var page = numbers.length ? numbers[0] : 1;
        var on = false;

        function show(n) {
            page = n;
            label.textContent = n;
            sections.forEach(function (s) { s.classList.toggle("in-pdf", on && parseInt(s.dataset.pageNumber, 10) === n); });
            // The query string makes the viewer reload at the new page; the
            // file itself comes from the browser's cache.
            if (on) frame.src = url + "?p=" + n + "#page=" + n + "&navpanes=0&view=FitH";
        }

        function setOn(value) {
            on = value;
            layout.classList.toggle("split", on);
            pane.hidden = !on;
            toggle.setAttribute("aria-pressed", String(on));
            toggle.textContent = on ? "Hide the PDF" : "Show the PDF beside the text";
            try { localStorage.setItem("dataopt.showPdf", on ? "1" : "0"); } catch (e) { /* not available */ }
            if (on) show(page); else frame.src = "about:blank";
        }

        function step(delta) {
            var i = numbers.indexOf(page);
            var next = numbers[Math.min(Math.max((i < 0 ? 0 : i) + delta, 0), numbers.length - 1)];
            var sec = sections[numbers.indexOf(next)];
            if (sec) { sec.open = true; sec.scrollIntoView({ block: "start" }); }
            show(next);
        }

        layout.addEventListener("reader:page", function (e) {
            var n = parseInt(e.detail.page, 10);
            if (!isNaN(n)) show(n);
        });
        pane.querySelector("[data-pdf-prev]").addEventListener("click", function () { step(-1); });
        pane.querySelector("[data-pdf-next]").addEventListener("click", function () { step(1); });
        toggle.addEventListener("click", function () { setOn(!on); });
        toggle.hidden = false;

        var saved = null;
        try { saved = localStorage.getItem("dataopt.showPdf"); } catch (e) { saved = null; }
        setOn(saved === null ? window.innerWidth >= 1000 : saved === "1");
    }

    document.querySelectorAll("[data-reader]").forEach(initReader);
    document.querySelectorAll("[data-doc-layout]").forEach(initPdfPane);
    openFromHash();
    window.addEventListener("hashchange", openFromHash);
})();
