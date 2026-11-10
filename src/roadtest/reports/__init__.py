"""Run results: saving and comparing runs, and showing them in the terminal, in Markdown and as an HTML report."""

from .report import markdown_summary, print_comparison, print_summary, result_line, write_html
from .runs import compare_runs, find_run, list_runs, save_baseline, save_run, summarize

__all__ = [
    "compare_runs",
    "find_run",
    "list_runs",
    "markdown_summary",
    "print_comparison",
    "print_summary",
    "result_line",
    "save_baseline",
    "save_run",
    "summarize",
    "write_html",
]
