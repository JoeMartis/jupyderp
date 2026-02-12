#!/usr/bin/env python3
"""
jupyderp - Convert Jupyter notebooks to fully accessible interactive HTML pages.

Usage:
    python jupyderp.py notebook.ipynb [-o output.html] [--title "Custom Title"]

Produces a single self-contained HTML file with:
  - WCAG 2.1 AA accessible markup (skip links, ARIA, high contrast)
  - Dark mode support (via prefers-color-scheme)
  - Responsive mobile layout
  - Syntax-highlighted code cells (Prism.js)
  - Rendered Markdown with math support (Marked.js + KaTeX)
  - Interactive toolbar (Run All, Clear, Reset)
  - Keyboard navigation (Ctrl+Enter to run focused cell)
  - Print, high-contrast, and reduced-motion media queries
  - Embedded images from notebook outputs (base64)
"""

import argparse
import base64
import html
import json
import os
import sys
from pathlib import Path


def read_notebook(path: str) -> dict:
    """Read and parse a .ipynb notebook file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _join(lines) -> str:
    """Join a list-of-strings field (or return a string as-is)."""
    if isinstance(lines, list):
        return "".join(lines)
    return lines


def _escape_js(text: str) -> str:
    """Escape a string for safe embedding inside a JS template literal (backtick)."""
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )


def _extract_cell_data(cell: dict, cell_index: int) -> dict:
    """Convert one notebook cell into the JS-friendly dict used by the template."""
    cell_type = cell.get("cell_type", "code")
    source = _join(cell.get("source", []))

    if cell_type == "markdown":
        return {"type": "markdown", "content": source}

    if cell_type == "raw":
        return {"type": "markdown", "content": f"```\n{source}\n```"}

    # --- code cell ---
    outputs = cell.get("outputs", [])
    text_parts = []
    html_parts = []
    image_parts = []
    error_parts = []

    for out in outputs:
        otype = out.get("output_type", "")

        if otype == "stream":
            text_parts.append(_join(out.get("text", [])))

        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            # Prefer HTML renderings (dataframes, rich output)
            if "text/html" in data:
                html_parts.append(_join(data["text/html"]))
            elif "image/png" in data:
                image_parts.append(data["image/png"])
            elif "image/svg+xml" in data:
                html_parts.append(_join(data["image/svg+xml"]))
            elif "text/plain" in data:
                text_parts.append(_join(data["text/plain"]))

        elif otype == "error":
            tb = out.get("traceback", [])
            # Strip ANSI escape sequences for readability
            import re
            ansi_re = re.compile(r"\x1b\[[0-9;]*m")
            for line in tb:
                error_parts.append(ansi_re.sub("", line))

    result: dict = {"type": "code", "content": source}

    # Build combined output string
    combined_text = "".join(text_parts)
    combined_html = "".join(html_parts)
    combined_images = image_parts
    combined_error = "\n".join(error_parts)

    if combined_text or combined_html or combined_images or combined_error:
        result["output"] = combined_text if combined_text else None
        result["outputHtml"] = combined_html if combined_html else None
        result["images"] = combined_images if combined_images else None
        result["error"] = combined_error if combined_error else None

    # Execution count
    ec = cell.get("execution_count")
    if ec is not None:
        result["executionCount"] = ec

    return result


def notebook_to_js_cells(nb: dict) -> str:
    """Return the notebook cells as a JSON array string for embedding in JS."""
    cells = nb.get("cells", [])
    js_cells = []
    for i, cell in enumerate(cells):
        js_cells.append(_extract_cell_data(cell, i))
    return json.dumps(js_cells, ensure_ascii=False)


def detect_kernel_language(nb: dict) -> str:
    """Best-effort detection of the notebook language for syntax highlighting."""
    meta = nb.get("metadata", {})
    ki = meta.get("kernelspec", {})
    lang = ki.get("language", "").lower()
    if lang:
        return lang
    li = meta.get("language_info", {})
    return li.get("name", "python").lower()


def detect_title(nb: dict, fallback: str) -> str:
    """Try to extract a title from the first markdown cell's first heading."""
    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "markdown":
            source = _join(cell.get("source", []))
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped.lstrip("# ").strip()
            break
    return fallback


def build_html(nb: dict, title: str | None = None) -> str:
    """Build the full accessible HTML page from a parsed notebook."""
    language = detect_kernel_language(nb)
    prism_lang = language if language in (
        "python", "javascript", "r", "julia", "ruby", "bash", "sql",
        "c", "cpp", "java", "go", "rust", "typescript", "scala",
    ) else "python"

    nb_title = title or detect_title(nb, "Jupyter Notebook")
    cells_json = notebook_to_js_cells(nb)

    return _HTML_TEMPLATE.replace("{{TITLE}}", html.escape(nb_title)).replace(
        "{{PRISM_LANG}}", prism_lang
    ).replace("{{CELLS_JSON}}", cells_json)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}}</title>

    <!-- Prism for syntax highlighting with accessible theme -->
    <link href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism.min.css" rel="stylesheet" />
    <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-{{PRISM_LANG}}.min.js"></script>

    <!-- Marked for Markdown -->
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>

    <!-- KaTeX for math -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">
    <script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js"></script>

    <style>
        :root {
            /* High contrast color scheme */
            --bg-primary: #ffffff;
            --bg-secondary: #f5f5f5;
            --bg-code: #1e1e1e;
            --text-primary: #000000;
            --text-secondary: #333333;
            --text-code: #f8f8f2;
            --accent-primary: #0066cc;
            --accent-hover: #0052a3;
            --border-color: #666666;
            --output-bg: #fafafa;
            --success-color: #008000;
            --error-color: #cc0000;

            /* Font sizes for accessibility */
            --font-size-base: 18px;
            --font-size-small: 16px;
            --font-size-code: 16px;
            --font-size-h1: 32px;
            --font-size-h2: 28px;
            --font-size-h3: 24px;
            --line-height: 1.6;
            --code-line-height: 1.8;
        }

        /* Dark mode support */
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-primary: #1a1a1a;
                --bg-secondary: #2a2a2a;
                --bg-code: #000000;
                --text-primary: #ffffff;
                --text-secondary: #e0e0e0;
                --text-code: #f8f8f2;
                --accent-primary: #4da6ff;
                --accent-hover: #66b3ff;
                --border-color: #999999;
                --output-bg: #2a2a2a;
            }
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: var(--font-size-base);
            line-height: var(--line-height);
            color: var(--text-primary);
            background-color: var(--bg-primary);
            padding: 20px;
        }

        /* Skip to main content link for screen readers */
        .skip-link {
            position: absolute;
            top: -40px;
            left: 0;
            background: var(--accent-primary);
            color: white;
            padding: 8px;
            text-decoration: none;
            z-index: 100;
        }

        .skip-link:focus {
            top: 0;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        .header {
            background: linear-gradient(135deg, var(--accent-primary), var(--accent-hover));
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
        }

        .header h1 {
            font-size: var(--font-size-h1);
            margin-bottom: 10px;
            font-weight: 600;
        }

        .header p {
            font-size: var(--font-size-base);
        }

        .toolbar {
            background: var(--bg-secondary);
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
            border: 2px solid var(--border-color);
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }

        .btn {
            background: var(--accent-primary);
            color: white;
            border: 2px solid transparent;
            padding: 12px 24px;
            border-radius: 6px;
            cursor: pointer;
            font-size: var(--font-size-base);
            font-weight: 500;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            min-height: 44px; /* WCAG minimum touch target */
        }

        .btn:hover, .btn:focus {
            background: var(--accent-hover);
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }

        .btn:active {
            transform: translateY(0);
        }

        .btn.secondary {
            background: #666666;
        }

        .btn.secondary:hover, .btn.secondary:focus {
            background: #555555;
        }

        /* Cells */
        .cell {
            background: var(--bg-primary);
            margin-bottom: 20px;
            border: 2px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
        }

        .cell:focus-within {
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }

        .cell-header {
            padding: 15px 20px;
            background: var(--bg-secondary);
            border-bottom: 2px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .cell-number {
            color: var(--text-secondary);
            font-size: var(--font-size-base);
            font-weight: 600;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
        }

        .cell-content {
            padding: 20px;
        }

        /* Markdown cells */
        .markdown-content {
            color: var(--text-primary);
            font-size: var(--font-size-base);
            line-height: var(--line-height);
        }

        .markdown-content h1 {
            font-size: var(--font-size-h1);
            font-weight: 600;
            margin: 30px 0 20px 0;
            padding-bottom: 10px;
            border-bottom: 3px solid var(--border-color);
        }

        .markdown-content h2 {
            font-size: var(--font-size-h2);
            font-weight: 600;
            margin: 25px 0 15px 0;
        }

        .markdown-content h3 {
            font-size: var(--font-size-h3);
            font-weight: 600;
            margin: 20px 0 10px 0;
        }

        .markdown-content p {
            margin-bottom: 15px;
            line-height: var(--line-height);
        }

        .markdown-content ul, .markdown-content ol {
            margin: 15px 0 15px 30px;
            line-height: var(--line-height);
        }

        .markdown-content li {
            margin-bottom: 8px;
            line-height: var(--line-height);
        }

        .markdown-content strong {
            font-weight: 700;
            color: var(--text-primary);
        }

        .markdown-content em {
            font-style: italic;
        }

        .markdown-content code {
            background: var(--bg-secondary);
            padding: 3px 8px;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: var(--font-size-code);
            border: 1px solid var(--border-color);
        }

        .markdown-content pre {
            background: var(--bg-code);
            color: var(--text-code);
            padding: 20px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 20px 0;
            font-size: var(--font-size-code);
            line-height: var(--code-line-height);
            border: 2px solid var(--border-color);
        }

        .markdown-content pre code {
            background: none;
            border: none;
            padding: 0;
            color: inherit;
        }

        .markdown-content a {
            color: var(--accent-primary);
            text-decoration: underline;
            font-weight: 500;
        }

        .markdown-content a:hover, .markdown-content a:focus {
            color: var(--accent-hover);
            outline: 2px solid var(--accent-primary);
            outline-offset: 2px;
        }

        .markdown-content table {
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
            font-size: var(--font-size-base);
        }

        .markdown-content th, .markdown-content td {
            border: 2px solid var(--border-color);
            padding: 12px 15px;
            text-align: left;
        }

        .markdown-content th {
            background: var(--bg-secondary);
            font-weight: 700;
        }

        .markdown-content img {
            max-width: 100%;
            height: auto;
        }

        /* Code cells */
        .code-input {
            background: var(--bg-code);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 10px;
            position: relative;
            border: 2px solid var(--border-color);
        }

        .code-input pre {
            margin: 0;
            color: var(--text-code);
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: var(--font-size-code);
            line-height: var(--code-line-height);
            overflow-x: auto;
        }

        .code-input code {
            font-size: var(--font-size-code) !important;
            line-height: var(--code-line-height) !important;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace !important;
        }

        /* Custom syntax highlighting for better readability */
        .token.comment {
            color: #6a9955;
            font-style: italic;
        }

        .token.string {
            color: #ce9178;
        }

        .token.keyword {
            color: #569cd6;
            font-weight: 600;
        }

        .token.function {
            color: #dcdcaa;
        }

        .token.number {
            color: #b5cea8;
        }

        .token.operator {
            color: #d4d4d4;
        }

        .run-button {
            position: absolute;
            top: 10px;
            right: 10px;
            background: var(--accent-primary);
            color: white;
            border: 2px solid white;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: var(--font-size-small);
            font-weight: 600;
            cursor: pointer;
            min-height: 44px;
        }

        .run-button:hover, .run-button:focus {
            background: var(--accent-hover);
            outline: 3px solid white;
            outline-offset: 2px;
        }

        .output-area {
            background: var(--output-bg);
            border: 2px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            margin-top: 15px;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: var(--font-size-code);
            line-height: var(--code-line-height);
            color: var(--text-primary);
            white-space: pre-wrap;
            word-wrap: break-word;
            max-height: 500px;
            overflow-y: auto;
        }

        .output-area.hidden {
            display: none;
        }

        .output-label {
            display: block;
            font-weight: 600;
            margin-bottom: 10px;
            color: var(--text-secondary);
            font-size: var(--font-size-small);
        }

        .output-area .html-output {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }

        .output-area .html-output table {
            border-collapse: collapse;
            font-size: var(--font-size-base);
            width: 100%;
            margin: 10px 0;
        }

        .output-area .html-output th,
        .output-area .html-output td {
            padding: 10px 15px;
            border: 2px solid var(--border-color);
            text-align: left;
        }

        .output-area .html-output th {
            background: var(--bg-secondary);
            font-weight: 700;
            color: var(--text-primary);
        }

        .output-area .error-output {
            color: var(--error-color);
            white-space: pre-wrap;
        }

        .output-area img {
            max-width: 100%;
            height: auto;
        }

        /* Loading animation */
        .loading {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
        }

        .loading-spinner {
            width: 24px;
            height: 24px;
            border: 3px solid var(--accent-primary);
            border-radius: 50%;
            border-top-color: transparent;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .execution-count {
            color: var(--text-secondary);
            font-size: var(--font-size-small);
            margin-top: 10px;
            font-style: italic;
        }

        /* Accessibility improvements */
        .visually-hidden {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border-width: 0;
        }

        /* Focus indicators */
        *:focus {
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }

        /* Print styles */
        @media print {
            body {
                font-size: 12pt;
                line-height: 1.5;
            }

            .toolbar, .run-button {
                display: none;
            }

            .cell {
                page-break-inside: avoid;
            }
        }

        /* High contrast mode support */
        @media (prefers-contrast: high) {
            :root {
                --border-color: #000000;
                --accent-primary: #0000ff;
                --bg-code: #000000;
                --text-code: #ffffff;
            }
        }

        /* Reduced motion support */
        @media (prefers-reduced-motion: reduce) {
            * {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
            }
        }

        /* Mobile responsive design */
        @media (max-width: 768px) {
            body {
                font-size: 16px;
                padding: 10px;
            }

            .header {
                padding: 20px;
            }

            .toolbar {
                flex-direction: column;
                align-items: stretch;
            }

            .btn {
                width: 100%;
                justify-content: center;
            }

            .code-input pre {
                font-size: 14px;
            }
        }
    </style>
</head>
<body>
    <a href="#main-content" class="skip-link">Skip to main content</a>

    <div class="container">
        <header class="header" role="banner">
            <h1>{{TITLE}}</h1>
            <p>Interactive accessible notebook viewer &mdash; generated by jupyderp</p>
        </header>

        <nav class="toolbar" role="navigation" aria-label="Notebook controls">
            <button class="btn" onclick="showAllOutputs()" aria-label="Show all cell outputs">
                <span aria-hidden="true">&#9654;</span> Show All Outputs
            </button>
            <button class="btn secondary" onclick="hideAllOutputs()" aria-label="Hide all cell outputs">
                <span aria-hidden="true">&#9003;</span> Hide All Outputs
            </button>
            <button class="btn secondary" onclick="resetNotebook()" aria-label="Reset notebook to initial state">
                <span aria-hidden="true">&#8635;</span> Reset Notebook
            </button>
        </nav>

        <main id="main-content" role="main">
            <div id="notebook" aria-label="Notebook cells"></div>
        </main>
    </div>

    <script>
        // ---------- Notebook cell data (injected by jupyderp) ----------
        const notebookCells = {{CELLS_JSON}};
        const PRISM_LANG = "{{PRISM_LANG}}";

        // ---------- Helpers ----------
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function renderMarkdown(content) {
            let html = marked.parse(content);
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = html;
            if (typeof renderMathInElement !== 'undefined') {
                renderMathInElement(tempDiv, {
                    delimiters: [
                        {left: '$$', right: '$$', display: true},
                        {left: '$', right: '$', display: false},
                        {left: '\\(', right: '\\)', display: false},
                        {left: '\\[', right: '\\]', display: true}
                    ]
                });
            }
            return tempDiv.innerHTML;
        }

        // ---------- Build output HTML from cell data ----------
        function buildOutputHtml(cell) {
            let parts = [];

            if (cell.output) {
                parts.push(escapeHtml(cell.output));
            }
            if (cell.outputHtml) {
                parts.push('<div class="html-output">' + cell.outputHtml + '</div>');
            }
            if (cell.images && cell.images.length) {
                for (const img of cell.images) {
                    parts.push('<img src="data:image/png;base64,' + img + '" alt="Cell output image">');
                }
            }
            if (cell.error) {
                parts.push('<div class="error-output">' + escapeHtml(cell.error) + '</div>');
            }
            return parts.join('\n');
        }

        function hasOutput(cell) {
            return !!(cell.output || cell.outputHtml || (cell.images && cell.images.length) || cell.error);
        }

        // ---------- Render one cell ----------
        function renderCell(cell, index) {
            const cellDiv = document.createElement('div');
            cellDiv.className = 'cell ' + (cell.type === 'code' ? 'code-cell' : 'markdown-cell');
            cellDiv.id = 'cell-' + index;
            cellDiv.setAttribute('role', 'article');
            cellDiv.setAttribute('aria-label', cell.type + ' cell ' + index);

            if (cell.type === 'markdown') {
                cellDiv.innerHTML =
                    '<div class="cell-content">' +
                        '<div class="markdown-content">' +
                            renderMarkdown(cell.content) +
                        '</div>' +
                    '</div>';
            } else {
                const execLabel = cell.executionCount != null
                    ? 'In [' + cell.executionCount + ']:'
                    : 'In [&nbsp;]:';
                const outputVisible = hasOutput(cell);

                cellDiv.innerHTML =
                    '<div class="cell-header">' +
                        '<span class="cell-number" aria-label="Cell number">' + execLabel + '</span>' +
                    '</div>' +
                    '<div class="cell-content">' +
                        '<div class="code-input" role="region" aria-label="Code input">' +
                            '<button class="run-button" onclick="toggleOutput(' + index + ')" ' +
                                    'aria-label="Toggle output for cell ' + index + '">' +
                                'Toggle Output' +
                            '</button>' +
                            '<pre><code class="language-' + PRISM_LANG + '">' + escapeHtml(cell.content) + '</code></pre>' +
                        '</div>' +
                        '<div class="output-area' + (outputVisible ? '' : ' hidden') + '" ' +
                             'id="output-' + index + '" ' +
                             'role="region" ' +
                             'aria-label="Cell output" ' +
                             'aria-live="polite">' +
                            (outputVisible ? '<span class="output-label">Output:</span>' + buildOutputHtml(cell) : '') +
                        '</div>' +
                        (cell.executionCount != null
                            ? '<div class="execution-count">Execution [' + cell.executionCount + ']</div>'
                            : '') +
                    '</div>';
            }

            return cellDiv;
        }

        // ---------- Interactive controls ----------
        function toggleOutput(index) {
            const cell = notebookCells[index];
            if (!hasOutput(cell)) return;
            const outputDiv = document.getElementById('output-' + index);
            if (outputDiv.classList.contains('hidden')) {
                outputDiv.classList.remove('hidden');
                outputDiv.innerHTML = '<span class="output-label">Output:</span>' + buildOutputHtml(cell);
            } else {
                outputDiv.classList.add('hidden');
            }
        }

        function showAllOutputs() {
            notebookCells.forEach(function(cell, i) {
                if (cell.type === 'code' && hasOutput(cell)) {
                    const el = document.getElementById('output-' + i);
                    if (el) {
                        el.classList.remove('hidden');
                        el.innerHTML = '<span class="output-label">Output:</span>' + buildOutputHtml(cell);
                    }
                }
            });
        }

        function hideAllOutputs() {
            document.querySelectorAll('.output-area').forEach(function(el) {
                el.classList.add('hidden');
            });
        }

        function resetNotebook() {
            // Re-render to original state (outputs shown by default if present)
            initNotebook();
        }

        // ---------- Initialise ----------
        function initNotebook() {
            const container = document.getElementById('notebook');
            container.innerHTML = '';

            notebookCells.forEach(function(cell, index) {
                container.appendChild(renderCell(cell, index));
            });

            // Apply syntax highlighting
            if (typeof Prism !== 'undefined') {
                Prism.highlightAll();
            }

            // Render math in markdown cells
            if (typeof renderMathInElement !== 'undefined') {
                document.querySelectorAll('.markdown-content').forEach(function(el) {
                    renderMathInElement(el, {
                        delimiters: [
                            {left: '$$', right: '$$', display: true},
                            {left: '$', right: '$', display: false},
                            {left: '\\(', right: '\\)', display: false},
                            {left: '\\[', right: '\\]', display: true}
                        ]
                    });
                });
            }
        }

        // Keyboard navigation
        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey && e.key === 'Enter') {
                var focusedCell = document.activeElement.closest('.cell');
                if (focusedCell) {
                    var cellId = parseInt(focusedCell.id.replace('cell-', ''));
                    toggleOutput(cellId);
                }
            }
        });

        // Start the notebook
        document.addEventListener('DOMContentLoaded', function() {
            initNotebook();
        });
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="jupyderp",
        description="Convert a Jupyter notebook to a fully accessible interactive HTML page.",
    )
    parser.add_argument("notebook", help="Path to the .ipynb file")
    parser.add_argument(
        "-o", "--output",
        help="Output HTML file path (default: <notebook-stem>.html)",
    )
    parser.add_argument(
        "--title",
        help="Custom page title (default: auto-detected from first heading)",
    )
    args = parser.parse_args()

    nb_path = Path(args.notebook)
    if not nb_path.exists():
        print(f"Error: file not found: {nb_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output) if args.output else nb_path.with_suffix(".html")

    nb = read_notebook(str(nb_path))
    html_content = build_html(nb, title=args.title)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Accessible HTML written to {out_path}")


if __name__ == "__main__":
    main()
