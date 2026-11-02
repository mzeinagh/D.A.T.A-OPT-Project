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
            target.closest("details").open = true;
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

        apply();
    }

    function openFromHash() {
        if (!location.hash) return;
        var el = document.getElementById(location.hash.slice(1));
        if (el && el.tagName === "DETAILS") {
            el.open = true;
            el.classList.add("flash");
            el.scrollIntoView({ block: "start" });
        }
    }

    document.querySelectorAll("[data-reader]").forEach(initReader);
    openFromHash();
    window.addEventListener("hashchange", openFromHash);
})();
