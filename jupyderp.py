#!/usr/bin/env python3
"""
jupyderp - Convert Jupyter notebooks to fully accessible interactive HTML pages.

Usage:
    # Convert a single notebook
    python jupyderp.py notebook.ipynb [-o output.html] [--title "Custom Title"]

    # Launch the web interface
    python jupyderp.py --serve [--port 8000]

Produces a single self-contained HTML file with:
  - WCAG 2.1 AA accessible markup (skip links, ARIA, high contrast)
  - Dark mode support (via prefers-color-scheme)
  - Responsive mobile layout
  - Syntax-highlighted code cells (Prism.js)
  - Rendered Markdown with math support (Marked.js + KaTeX)
  - Interactive toolbar (Show/Hide/Reset outputs)
  - Keyboard navigation (Ctrl+Enter to toggle focused cell output)
  - Print, high-contrast, and reduced-motion media queries
  - Embedded images from notebook outputs (base64)
"""

import argparse
import base64
import html
import io
import json
import os
import re as _re
import sys
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
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
            # Collect ALL representations present (not elif -- they can coexist)
            has_rich = False
            if "image/png" in data:
                img_data = data["image/png"]
                # Handle both string and list-of-strings
                if isinstance(img_data, list):
                    img_data = "".join(img_data)
                # Strip whitespace/newlines from base64
                image_parts.append(img_data.strip())
                has_rich = True
            if "image/jpeg" in data:
                img_data = data["image/jpeg"]
                if isinstance(img_data, list):
                    img_data = "".join(img_data)
                image_parts.append(img_data.strip())
                has_rich = True
            if "image/svg+xml" in data:
                html_parts.append(_join(data["image/svg+xml"]))
                has_rich = True
            if "text/html" in data:
                html_parts.append(_join(data["text/html"]))
                has_rich = True
            # Only use text/plain as fallback when no richer format exists
            if not has_rich and "text/plain" in data:
                text_parts.append(_join(data["text/plain"]))

        elif otype == "error":
            tb = out.get("traceback", [])
            # Strip ANSI escape sequences for readability
            ansi_re = _re.compile(r"\x1b\[[0-9;]*m")
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
        data = _extract_cell_data(cell, i)
        # Skip empty code cells (no source content)
        if data["type"] == "code" and not data.get("content", "").strip():
            continue
        js_cells.append(data)
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

        .loading {
            display: flex;
            align-items: center;
            gap: 12px;
            color: var(--text-secondary);
            font-size: var(--font-size-base);
            padding: 10px 0;
        }

        .loading-spinner {
            width: 24px;
            height: 24px;
            border: 3px solid var(--border-color);
            border-top-color: var(--accent-primary);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
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
            <button class="btn" onclick="runAllCells()" aria-label="Run all code cells">
                <span aria-hidden="true">&#9654;</span> Run All Cells
            </button>
            <button class="btn secondary" onclick="clearOutputs()" aria-label="Clear all cell outputs">
                <span aria-hidden="true">&#9003;</span> Clear All Outputs
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

        let isExecuting = false;
        let currentCell = 0;

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

                cellDiv.innerHTML =
                    '<div class="cell-header">' +
                        '<span class="cell-number" aria-label="Cell number">' + execLabel + '</span>' +
                    '</div>' +
                    '<div class="cell-content">' +
                        '<div class="code-input" role="region" aria-label="Code input">' +
                            '<button class="run-button" onclick="executeCell(' + index + ')" ' +
                                    'aria-label="Run cell ' + index + '">' +
                                'Run Cell' +
                            '</button>' +
                            '<pre><code class="language-' + PRISM_LANG + '">' + escapeHtml(cell.content) + '</code></pre>' +
                        '</div>' +
                        '<div class="output-area hidden" ' +
                             'id="output-' + index + '" ' +
                             'role="region" ' +
                             'aria-label="Cell output" ' +
                             'aria-live="polite">' +
                        '</div>' +
                    '</div>';
            }

            return cellDiv;
        }

        // ---------- Execute a single cell ----------
        function executeCell(index) {
            if (isExecuting) return;

            var cell = notebookCells[index];
            if (cell.type !== 'code') return;

            isExecuting = true;
            var outputDiv = document.getElementById('output-' + index);
            var cellDiv = document.getElementById('cell-' + index);

            // Show loading spinner
            outputDiv.classList.remove('hidden');
            outputDiv.innerHTML =
                '<div class="loading">' +
                    '<div class="loading-spinner"></div>' +
                    '<span>Executing cell...</span>' +
                '</div>';

            // Simulate execution delay, then show output
            setTimeout(function() {
                if (hasOutput(cell)) {
                    outputDiv.innerHTML =
                        '<span class="output-label">Output:</span>' +
                        buildOutputHtml(cell);
                } else {
                    outputDiv.innerHTML =
                        '<span style="color: var(--text-secondary);">' +
                        'Cell executed successfully (no output)</span>';
                }

                isExecuting = false;

                // If running all, advance to the next code cell
                if (window.runningAll) {
                    advanceRunAll();
                }
            }, Math.random() * 800 + 400);
        }

        // ---------- Run All Cells ----------
        function runAllCells() {
            if (isExecuting) return;
            window.runningAll = true;
            currentCell = 0;

            // Find first code cell
            while (currentCell < notebookCells.length && notebookCells[currentCell].type !== 'code') {
                currentCell++;
            }

            if (currentCell < notebookCells.length) {
                executeCell(currentCell);
            } else {
                window.runningAll = false;
            }
        }

        function advanceRunAll() {
            currentCell++;
            // Skip to next code cell
            while (currentCell < notebookCells.length && notebookCells[currentCell].type !== 'code') {
                currentCell++;
            }
            if (currentCell < notebookCells.length) {
                setTimeout(function() { executeCell(currentCell); }, 300);
            } else {
                window.runningAll = false;
            }
        }

        // ---------- Clear All Outputs ----------
        function clearOutputs() {
            window.runningAll = false;
            document.querySelectorAll('.output-area').forEach(function(el) {
                el.classList.add('hidden');
                el.innerHTML = '';
            });
        }

        // ---------- Reset Notebook ----------
        function resetNotebook() {
            window.runningAll = false;
            isExecuting = false;
            currentCell = 0;
            initNotebook();
        }

        // ---------- Initialise ----------
        function initNotebook() {
            var container = document.getElementById('notebook');
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

        // Keyboard navigation: Ctrl+Enter runs focused cell
        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey && e.key === 'Enter') {
                var focusedCell = document.activeElement.closest('.cell');
                if (focusedCell) {
                    var cellId = parseInt(focusedCell.id.replace('cell-', ''));
                    executeCell(cellId);
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
# Web interface HTML template
# ---------------------------------------------------------------------------
_UPLOAD_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>jupyderp - Accessible Notebook Converter</title>
    <style>
        :root {
            --bg-primary: #ffffff;
            --bg-secondary: #f5f5f5;
            --text-primary: #000000;
            --text-secondary: #333333;
            --accent-primary: #0066cc;
            --accent-hover: #0052a3;
            --border-color: #666666;
            --success-color: #008000;
            --error-color: #cc0000;
            --font-size-base: 18px;
        }

        @media (prefers-color-scheme: dark) {
            :root {
                --bg-primary: #1a1a1a;
                --bg-secondary: #2a2a2a;
                --text-primary: #ffffff;
                --text-secondary: #e0e0e0;
                --accent-primary: #4da6ff;
                --accent-hover: #66b3ff;
                --border-color: #999999;
            }
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: var(--font-size-base);
            line-height: 1.6;
            color: var(--text-primary);
            background: var(--bg-primary);
            padding: 20px;
        }

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
        .skip-link:focus { top: 0; }

        .container {
            max-width: 800px;
            margin: 0 auto;
        }

        .header {
            background: linear-gradient(135deg, var(--accent-primary), var(--accent-hover));
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
            text-align: center;
        }
        .header h1 { font-size: 32px; margin-bottom: 10px; }
        .header p { font-size: var(--font-size-base); opacity: 0.9; }

        .upload-section {
            background: var(--bg-secondary);
            border: 3px dashed var(--border-color);
            border-radius: 12px;
            padding: 60px 40px;
            text-align: center;
            margin-bottom: 30px;
            transition: border-color 0.2s, background 0.2s;
        }
        .upload-section.drag-over {
            border-color: var(--accent-primary);
            background: color-mix(in srgb, var(--accent-primary) 10%, var(--bg-secondary));
        }
        .upload-section h2 {
            font-size: 24px;
            margin-bottom: 15px;
        }
        .upload-section p {
            color: var(--text-secondary);
            margin-bottom: 25px;
        }

        .file-input-wrapper {
            position: relative;
            display: inline-block;
        }
        .file-input-wrapper input[type="file"] {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0,0,0,0);
            border: 0;
        }

        .btn {
            background: var(--accent-primary);
            color: white;
            border: 2px solid transparent;
            padding: 14px 32px;
            border-radius: 6px;
            cursor: pointer;
            font-size: var(--font-size-base);
            font-weight: 600;
            min-height: 44px;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }
        .btn:hover, .btn:focus {
            background: var(--accent-hover);
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }

        .title-field {
            margin-bottom: 30px;
        }
        .title-field label {
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .title-field input {
            width: 100%;
            padding: 12px 16px;
            font-size: var(--font-size-base);
            border: 2px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 44px;
        }
        .title-field input:focus {
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }

        .file-name {
            margin-top: 15px;
            font-weight: 600;
            color: var(--success-color);
        }
        .file-name:empty { display: none; }

        .status {
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-weight: 500;
            display: none;
        }
        .status.visible { display: block; }
        .status.success {
            background: color-mix(in srgb, var(--success-color) 10%, var(--bg-secondary));
            border: 2px solid var(--success-color);
            color: var(--success-color);
        }
        .status.error {
            background: color-mix(in srgb, var(--error-color) 10%, var(--bg-secondary));
            border: 2px solid var(--error-color);
            color: var(--error-color);
        }

        .result-actions {
            display: none;
            gap: 15px;
            flex-wrap: wrap;
            margin-bottom: 30px;
        }
        .result-actions.visible {
            display: flex;
        }

        .preview-frame {
            display: none;
            width: 100%;
            border: 2px solid var(--border-color);
            border-radius: 8px;
            min-height: 600px;
            background: white;
        }
        .preview-frame.visible { display: block; }

        .instructions {
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            border-radius: 8px;
            padding: 25px;
            margin-bottom: 30px;
        }
        .instructions h3 { margin-bottom: 10px; }
        .instructions ol {
            margin-left: 25px;
        }
        .instructions li {
            margin-bottom: 8px;
        }

        footer {
            text-align: center;
            color: var(--text-secondary);
            padding: 20px;
            font-size: 16px;
        }

        *:focus {
            outline: 3px solid var(--accent-primary);
            outline-offset: 2px;
        }

        @media (prefers-reduced-motion: reduce) {
            * {
                animation-duration: 0.01ms !important;
                transition-duration: 0.01ms !important;
            }
        }

        @media (prefers-contrast: high) {
            :root {
                --border-color: #000000;
                --accent-primary: #0000ff;
            }
        }

        @media (max-width: 768px) {
            body { padding: 10px; }
            .upload-section { padding: 30px 20px; }
            .result-actions { flex-direction: column; }
            .btn { width: 100%; justify-content: center; }
        }
    </style>
</head>
<body>
    <a href="#main-content" class="skip-link">Skip to main content</a>

    <div class="container">
        <header class="header" role="banner">
            <h1>jupyderp</h1>
            <p>Convert Jupyter notebooks to fully accessible interactive HTML</p>
        </header>

        <main id="main-content" role="main">
            <div class="instructions" role="region" aria-label="Instructions">
                <h3>How it works</h3>
                <ol>
                    <li>Upload a <code>.ipynb</code> Jupyter notebook file</li>
                    <li>Optionally set a custom page title</li>
                    <li>Click <strong>Convert</strong> to generate an accessible HTML page</li>
                    <li>Preview inline or download the result</li>
                </ol>
            </div>

            <form id="upload-form" aria-label="Notebook upload form">
                <div class="upload-section" id="drop-zone" role="region" aria-label="File upload area">
                    <h2>Upload Notebook</h2>
                    <p>Drag and drop a .ipynb file here, or click to browse</p>
                    <div class="file-input-wrapper">
                        <label class="btn" for="file-input" role="button" tabindex="0">
                            Choose File
                        </label>
                        <input type="file" id="file-input" name="notebook"
                               accept=".ipynb,application/json"
                               aria-describedby="file-name-display">
                    </div>
                    <div class="file-name" id="file-name-display" aria-live="polite"></div>
                </div>

                <div class="title-field">
                    <label for="custom-title">Custom page title (optional)</label>
                    <input type="text" id="custom-title" name="title"
                           placeholder="Auto-detected from first heading if left blank">
                </div>

                <button type="submit" class="btn" id="convert-btn" disabled
                        aria-label="Convert notebook to accessible HTML">
                    Convert to Accessible HTML
                </button>
            </form>

            <div class="status" id="status" role="alert" aria-live="assertive"></div>

            <div class="result-actions" id="result-actions">
                <button class="btn" id="download-btn" aria-label="Download converted HTML file">
                    Download HTML
                </button>
                <button class="btn" id="preview-btn" aria-label="Preview converted HTML inline">
                    Preview
                </button>
                <button class="btn" id="new-tab-btn" aria-label="Open converted HTML in a new tab">
                    Open in New Tab
                </button>
            </div>

            <iframe class="preview-frame" id="preview-frame"
                    title="Notebook preview" aria-label="Converted notebook preview"></iframe>
        </main>

        <footer role="contentinfo">
            <p>jupyderp &mdash; accessible notebook conversion</p>
        </footer>
    </div>

    <script>
        const dropZone = document.getElementById('drop-zone');
        const fileInput = document.getElementById('file-input');
        const form = document.getElementById('upload-form');
        const convertBtn = document.getElementById('convert-btn');
        const statusEl = document.getElementById('status');
        const fileNameEl = document.getElementById('file-name-display');
        const resultActions = document.getElementById('result-actions');
        const previewFrame = document.getElementById('preview-frame');

        let convertedHtml = null;
        let convertedFileName = 'notebook.html';

        // --- Drag and drop ---
        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });
        dropZone.addEventListener('dragleave', function() {
            dropZone.classList.remove('drag-over');
        });
        dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            const files = e.dataTransfer.files;
            if (files.length > 0 && files[0].name.endsWith('.ipynb')) {
                fileInput.files = files;
                onFileSelected(files[0]);
            } else {
                showStatus('Please drop a .ipynb file.', 'error');
            }
        });

        // --- Click the drop zone to trigger file input ---
        dropZone.addEventListener('click', function(e) {
            if (e.target === fileInput || e.target.closest('.file-input-wrapper')) return;
            fileInput.click();
        });

        // --- File selection ---
        fileInput.addEventListener('change', function() {
            if (fileInput.files.length > 0) {
                onFileSelected(fileInput.files[0]);
            }
        });

        function onFileSelected(file) {
            fileNameEl.textContent = 'Selected: ' + file.name;
            convertBtn.disabled = false;
            convertedHtml = null;
            resultActions.classList.remove('visible');
            previewFrame.classList.remove('visible');
            statusEl.classList.remove('visible');
            convertedFileName = file.name.replace(/\.ipynb$/, '.html');
        }

        // --- Form submit ---
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            if (!fileInput.files.length) return;

            convertBtn.disabled = true;
            showStatus('Converting...', 'success');

            const formData = new FormData();
            formData.append('notebook', fileInput.files[0]);
            const title = document.getElementById('custom-title').value.trim();
            if (title) formData.append('title', title);

            fetch('/convert', {
                method: 'POST',
                body: formData
            })
            .then(function(resp) {
                if (!resp.ok) return resp.text().then(function(t) { throw new Error(t); });
                return resp.text();
            })
            .then(function(html) {
                convertedHtml = html;
                showStatus('Conversion successful!', 'success');
                resultActions.classList.add('visible');
                convertBtn.disabled = false;
            })
            .catch(function(err) {
                showStatus('Error: ' + err.message, 'error');
                convertBtn.disabled = false;
            });
        });

        // --- Result actions ---
        document.getElementById('download-btn').addEventListener('click', function() {
            if (!convertedHtml) return;
            const blob = new Blob([convertedHtml], { type: 'text/html' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = convertedFileName;
            a.click();
            URL.revokeObjectURL(url);
        });

        document.getElementById('preview-btn').addEventListener('click', function() {
            if (!convertedHtml) return;
            previewFrame.classList.toggle('visible');
            if (previewFrame.classList.contains('visible')) {
                previewFrame.srcdoc = convertedHtml;
            }
        });

        document.getElementById('new-tab-btn').addEventListener('click', function() {
            if (!convertedHtml) return;
            const blob = new Blob([convertedHtml], { type: 'text/html' });
            const url = URL.createObjectURL(blob);
            window.open(url, '_blank');
        });

        function showStatus(msg, type) {
            statusEl.textContent = msg;
            statusEl.className = 'status visible ' + type;
        }
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------
def _extract_boundary(content_type: str) -> bytes | None:
    """Extract the multipart boundary from a Content-Type header."""
    m = _re.search(r'boundary=([^\s;]+)', content_type)
    if m:
        return m.group(1).encode("ascii")
    return None


def _parse_multipart(body: bytes, boundary: bytes) -> dict[str, bytes]:
    """Minimal multipart/form-data parser (stdlib only, no cgi)."""
    parts: dict[str, bytes] = {}
    delimiter = b"--" + boundary
    segments = body.split(delimiter)
    for seg in segments:
        # Skip preamble and epilogue
        if seg in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        seg = seg.lstrip(b"\r\n")
        if seg.startswith(b"--"):
            continue
        # Split headers from body
        sep = seg.find(b"\r\n\r\n")
        if sep == -1:
            continue
        header_block = seg[:sep].decode("utf-8", errors="replace")
        payload = seg[sep + 4:]
        # Strip trailing \r\n
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        # Find field name
        m = _re.search(r'name="([^"]+)"', header_block)
        if m:
            parts[m.group(1)] = payload
    return parts


class JupyderpHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the jupyderp web interface."""

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self._send_html(200, _UPLOAD_PAGE)
        else:
            self._send_html(404, "<h1>Not Found</h1>")

    def do_POST(self):
        if self.path != "/convert":
            self._send_html(404, "<h1>Not Found</h1>")
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_html(400, "Expected multipart/form-data")
            return

        try:
            # Read the full body
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            # Extract boundary from Content-Type
            boundary = _extract_boundary(content_type)
            if boundary is None:
                self._send_html(400, "Missing multipart boundary")
                return

            parts = _parse_multipart(body, boundary)

            # Extract notebook JSON
            if "notebook" not in parts:
                self._send_html(400, "No notebook file uploaded")
                return

            nb = json.loads(parts["notebook"].decode("utf-8"))

            # Optional title
            title = None
            if "title" in parts and parts["title"]:
                title = parts["title"].decode("utf-8").strip() or None

            result_html = build_html(nb, title=title)
            self._send_html(200, result_html)

        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
            self._send_html(400, f"Invalid notebook file: {exc}")
        except Exception as exc:
            self._send_html(500, f"Conversion error: {exc}")

    def _send_html(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        encoded = body.encode("utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        print(f"[jupyderp] {args[0]}")


def start_server(port: int = 8000):
    """Launch the jupyderp web interface."""
    server = HTTPServer(("", port), JupyderpHandler)
    print(f"jupyderp web interface running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="jupyderp",
        description="Convert a Jupyter notebook to a fully accessible interactive HTML page.",
    )
    parser.add_argument(
        "notebook", nargs="?", default=None,
        help="Path to the .ipynb file (not needed with --serve)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output HTML file path (default: <notebook-stem>.html)",
    )
    parser.add_argument(
        "--title",
        help="Custom page title (default: auto-detected from first heading)",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="Launch the web interface instead of converting a file",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="Port for the web server (default: 8000)",
    )
    args = parser.parse_args()

    # --- Web server mode ---
    if args.serve:
        start_server(port=args.port)
        return

    # --- CLI conversion mode ---
    if args.notebook is None:
        parser.error("the following arguments are required: notebook (or use --serve)")

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
