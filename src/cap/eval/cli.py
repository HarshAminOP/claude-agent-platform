"""CLI commands for the CAP evaluation framework.

Provides `cap eval run`, `cap eval list`, and `cap eval report` commands.
Integrates with the main Click CLI via a group that can be registered.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cap.eval.framework import EvalReport, EvalSuite, discover_suites


console = Console()


# ---------------------------------------------------------------------------
# CLI Group
# ---------------------------------------------------------------------------


@click.group("eval")
def eval_group():
    """Evaluation framework for CAP components.

    Run quality and performance evaluations, view results, and export reports.
    """


# ---------------------------------------------------------------------------
# cap eval run
# ---------------------------------------------------------------------------


@eval_group.command("run")
@click.option(
    "--suite", "-s",
    multiple=True,
    help="Suite(s) to run. Omit to run all. Can be repeated: -s retrieval -s security",
)
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False),
    help="Path to write JSON report.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    help="Show individual case results.",
)
@click.option(
    "--fail-under",
    type=float,
    default=None,
    help="Exit with code 1 if overall pass rate is below this threshold (0.0-1.0).",
)
def run_cmd(suite: tuple[str, ...], output: Optional[str], verbose: bool, fail_under: Optional[float]):
    """Run evaluation suites and display results."""
    available = discover_suites()

    # Determine which suites to run
    if suite:
        selected = {}
        for name in suite:
            if name not in available:
                console.print(f"[red]Unknown suite:[/red] {name}")
                console.print(f"Available: {', '.join(available.keys())}")
                sys.exit(1)
            selected[name] = available[name]
    else:
        selected = available

    console.print()
    console.print(
        Panel(
            f"[bold]CAP Evaluation Framework[/bold]\n"
            f"Running {len(selected)} suite(s): {', '.join(selected.keys())}",
            border_style="blue",
        )
    )
    console.print()

    all_reports: list[EvalReport] = []
    total_start = time.time()

    for name, suite_cls in selected.items():
        console.print(f"[bold cyan]>>> Suite: {name}[/bold cyan]")
        console.print(f"    {suite_cls.description}")

        suite_instance: EvalSuite = suite_cls()

        try:
            report = suite_instance.run()
            all_reports.append(report)
            _display_suite_summary(report, verbose)
        except Exception as e:
            console.print(f"    [red]FAILED:[/red] {e}")
            console.print()

    total_duration = (time.time() - total_start) * 1000

    # Overall summary
    _display_overall_summary(all_reports, total_duration)

    # Export JSON
    if output:
        combined = _combine_reports(all_reports, total_duration)
        Path(output).write_text(json.dumps(combined, indent=2, default=str))
        console.print(f"\n[dim]Report saved to:[/dim] {output}")

    # Exit code for CI
    if fail_under is not None:
        total_cases = sum(r.total_cases for r in all_reports)
        total_passed = sum(r.passed for r in all_reports)
        overall_rate = total_passed / total_cases if total_cases > 0 else 0.0
        if overall_rate < fail_under:
            console.print(
                f"\n[red]FAIL:[/red] Pass rate {overall_rate:.1%} is below "
                f"threshold {fail_under:.1%}"
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# cap eval list
# ---------------------------------------------------------------------------


@eval_group.command("list")
def list_cmd():
    """List available evaluation suites."""
    available = discover_suites()

    table = Table(title="Available Eval Suites", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Categories", style="dim")

    for name, suite_cls in sorted(available.items()):
        # Instantiate briefly to get category info
        instance = suite_cls()
        try:
            instance.setup()
            cases = instance.build_cases()
            categories = sorted(set(c.category for c in cases))
            instance.teardown()
        except Exception:
            categories = ["(unable to load)"]

        table.add_row(
            name,
            suite_cls.description or "(no description)",
            ", ".join(categories),
        )

    console.print()
    console.print(table)
    console.print()
    console.print(f"[dim]Run with:[/dim] cap eval run --suite <name>")


# ---------------------------------------------------------------------------
# cap eval report
# ---------------------------------------------------------------------------


@eval_group.command("report")
@click.argument("file", type=click.Path(exists=True))
@click.option("--category", "-c", help="Filter by category.")
@click.option("--failures-only", is_flag=True, help="Show only failed cases.")
def report_cmd(file: str, category: Optional[str], failures_only: bool):
    """Display a previously saved evaluation report."""
    with open(file) as f:
        data = json.load(f)

    # Handle combined report format
    if "suites" in data:
        suites = data["suites"]
    else:
        suites = [data]

    for suite_data in suites:
        console.print(f"\n[bold cyan]Suite: {suite_data['suite_name']}[/bold cyan]")
        console.print(f"  Timestamp: {suite_data['timestamp']}")
        console.print(f"  Duration: {suite_data['duration_ms']:.0f}ms")
        console.print(
            f"  Results: {suite_data['passed']}/{suite_data['total_cases']} passed "
            f"({suite_data['pass_rate']:.1%})"
        )
        console.print(f"  Overall score: {suite_data['overall_score']:.3f}")

        # Category breakdown
        if suite_data.get("categories"):
            console.print()
            cat_table = Table(show_header=True, header_style="bold")
            cat_table.add_column("Category")
            cat_table.add_column("Pass Rate", justify="right")
            cat_table.add_column("Avg Score", justify="right")
            cat_table.add_column("P95 Latency", justify="right")

            for cat in suite_data["categories"]:
                if category and cat["category"] != category:
                    continue
                rate_color = "green" if cat["pass_rate"] >= 0.9 else "yellow" if cat["pass_rate"] >= 0.7 else "red"
                cat_table.add_row(
                    cat["category"],
                    f"[{rate_color}]{cat['pass_rate']:.0%}[/{rate_color}]",
                    f"{cat['avg_score']:.3f}",
                    f"{cat['p95_latency_ms']:.1f}ms",
                )

            console.print(cat_table)

        # Individual results
        if suite_data.get("results"):
            results = suite_data["results"]
            if category:
                results = [r for r in results if r["category"] == category]
            if failures_only:
                results = [r for r in results if not r["passed"]]

            if results:
                console.print()
                res_table = Table(show_header=True, header_style="bold", show_lines=False)
                res_table.add_column("Case", max_width=40)
                res_table.add_column("Score", justify="right")
                res_table.add_column("Threshold", justify="right")
                res_table.add_column("Status")
                res_table.add_column("Latency", justify="right")

                for r in results[:30]:  # Cap at 30 rows
                    status = "[green]PASS[/green]" if r["passed"] else "[red]FAIL[/red]"
                    res_table.add_row(
                        r["name"],
                        f"{r['score']:.3f}",
                        f"{r['threshold']:.3f}",
                        status,
                        f"{r['latency_ms']:.1f}ms",
                    )

                console.print(res_table)

                if len(suite_data["results"]) > 30:
                    console.print(f"  [dim]... and {len(suite_data['results']) - 30} more[/dim]")

        # Recommendations
        if suite_data.get("recommendations"):
            console.print()
            console.print("[bold]Recommendations:[/bold]")
            for rec in suite_data["recommendations"][:5]:
                console.print(f"  [yellow]>[/yellow] {rec}")

    console.print()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _display_suite_summary(report: EvalReport, verbose: bool) -> None:
    """Display summary for a single suite run."""
    pass_color = "green" if report.pass_rate >= 0.9 else "yellow" if report.pass_rate >= 0.7 else "red"

    console.print(
        f"    [{pass_color}]{report.passed}/{report.total_cases} passed[/{pass_color}] "
        f"({report.pass_rate:.0%}) | "
        f"Score: {report.overall_score:.3f} | "
        f"Duration: {report.duration_ms:.0f}ms"
    )

    # Category summary
    for cat in report.categories:
        cat_color = "green" if cat.pass_rate >= 0.9 else "yellow" if cat.pass_rate >= 0.7 else "red"
        console.print(
            f"      [{cat_color}]{cat.category}[/{cat_color}]: "
            f"{cat.passed}/{cat.total} | "
            f"avg={cat.avg_score:.3f} | "
            f"p95={cat.p95_latency_ms:.1f}ms"
        )

    # Show failures in verbose mode
    if verbose and report.worst_performers:
        console.print()
        for r in report.worst_performers[:3]:
            console.print(
                f"      [red]FAIL[/red] {r.case.name}: "
                f"score={r.score:.3f} (threshold={r.case.threshold}) "
                f"- {r.details.get('reason', '')}"
            )

    console.print()


def _display_overall_summary(reports: list[EvalReport], total_duration_ms: float) -> None:
    """Display combined summary across all suites."""
    total_cases = sum(r.total_cases for r in reports)
    total_passed = sum(r.passed for r in reports)
    total_failed = sum(r.failed for r in reports)
    overall_rate = total_passed / total_cases if total_cases > 0 else 0.0
    overall_score = (
        sum(r.overall_score * r.total_cases for r in reports) / total_cases
        if total_cases > 0
        else 0.0
    )

    pass_color = "green" if overall_rate >= 0.9 else "yellow" if overall_rate >= 0.7 else "red"

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("Label", style="bold")
    summary.add_column("Value")

    summary.add_row("Total Cases", str(total_cases))
    summary.add_row("Passed", f"[green]{total_passed}[/green]")
    summary.add_row("Failed", f"[red]{total_failed}[/red]" if total_failed else "0")
    summary.add_row("Pass Rate", f"[{pass_color}]{overall_rate:.1%}[/{pass_color}]")
    summary.add_row("Overall Score", f"{overall_score:.3f}")
    summary.add_row("Total Duration", f"{total_duration_ms:.0f}ms")

    console.print(Panel(summary, title="[bold]Overall Results[/bold]", border_style=pass_color))

    # Worst performers across all suites
    all_failures = []
    for report in reports:
        all_failures.extend(report.worst_performers)
    all_failures.sort(key=lambda r: r.score)

    if all_failures:
        console.print()
        console.print("[bold]Top failures across all suites:[/bold]")
        for r in all_failures[:5]:
            console.print(
                f"  [red]x[/red] [{r.case.category}] {r.case.name}: "
                f"score={r.score:.3f} (need {r.case.threshold})"
            )

    # Recommendations
    all_recs = []
    for report in reports:
        all_recs.extend(report.recommendations)
    if all_recs:
        console.print()
        console.print("[bold]Recommendations:[/bold]")
        for rec in all_recs[:5]:
            console.print(f"  [yellow]>[/yellow] {rec}")


def _combine_reports(reports: list[EvalReport], total_duration_ms: float) -> dict:
    """Combine multiple suite reports into a single JSON structure."""
    total_cases = sum(r.total_cases for r in reports)
    total_passed = sum(r.passed for r in reports)

    return {
        "framework": "cap-eval",
        "version": "1.0.0",
        "total_duration_ms": total_duration_ms,
        "total_cases": total_cases,
        "total_passed": total_passed,
        "total_failed": total_cases - total_passed,
        "overall_pass_rate": total_passed / total_cases if total_cases > 0 else 0.0,
        "suites": [r.to_dict() for r in reports],
    }
