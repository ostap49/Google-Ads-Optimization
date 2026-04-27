"""Rich terminal UI for reviewing and acting on recommendations."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich import box

from ..recommendations.recommendation import (
    Recommendation,
    RecommendationBatch,
    RecommendationPriority,
    RecommendationStatus,
    RecommendationType,
)

logger = logging.getLogger(__name__)
console = Console()

# ------------------------------------------------------------------ #
# Priority color mapping
# ------------------------------------------------------------------ #
PRIORITY_COLORS = {
    RecommendationPriority.CRITICAL: "bold red",
    RecommendationPriority.HIGH: "red",
    RecommendationPriority.MEDIUM: "yellow",
    RecommendationPriority.LOW: "green",
}

REC_TYPE_ICONS = {
    RecommendationType.KEYWORD_LOW_QUALITY_SCORE: "★",
    RecommendationType.KEYWORD_DUPLICATE: "⊗",
    RecommendationType.KEYWORD_ADD_NEGATIVE: "−",
    RecommendationType.KEYWORD_OPPORTUNITY: "+",
    RecommendationType.BID_INCREASE: "↑",
    RecommendationType.BID_DECREASE: "↓",
    RecommendationType.BUDGET_LIMITED: "!",
    RecommendationType.BUDGET_REALLOCATION: "⇄",
    RecommendationType.AD_PAUSE_LOW_PERFORMER: "⏸",
    RecommendationType.AD_ADD_RESPONSIVE: "R",
}


def _priority_text(priority: str) -> Text:
    """Render a priority badge with color."""
    color = PRIORITY_COLORS.get(priority, "white")
    return Text(priority, style=color)


def _impact_bar(score: float) -> str:
    """Render a simple ASCII impact bar."""
    filled = int(score)
    return "█" * filled + "░" * (10 - filled)


# ------------------------------------------------------------------ #
# Public display functions
# ------------------------------------------------------------------ #

def display_accounts_table(accounts: List[Dict[str, Any]]) -> None:
    """Render a Rich table showing all MCC child accounts and their metrics."""
    table = Table(
        title="[bold cyan]MCC Client Accounts — Last 30 Days[/bold cyan]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )

    table.add_column("Account ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Currency", justify="center")
    table.add_column("Impressions", justify="right")
    table.add_column("Clicks", justify="right")
    table.add_column("CTR", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Conv.", justify="right")
    table.add_column("ROAS", justify="right")

    for acc in accounts:
        cost = acc.get("cost", 0.0)
        roas = acc.get("roas", 0.0)
        ctr = acc.get("ctr", 0.0)
        conv = acc.get("conversions", 0.0)

        roas_style = "green" if roas >= 2.0 else "yellow" if roas >= 1.0 else "red"
        ctr_style = "green" if ctr >= 0.05 else "yellow" if ctr >= 0.02 else "white"

        table.add_row(
            acc.get("id", ""),
            acc.get("name", "Unknown"),
            acc.get("currency", ""),
            f"{acc.get('impressions', 0):,}",
            f"{acc.get('clicks', 0):,}",
            Text(f"{ctr * 100:.2f}%", style=ctr_style),
            f"${cost:,.2f}",
            f"{conv:.1f}",
            Text(f"{roas:.2f}x", style=roas_style),
        )

    console.print(table)


def display_recommendations(
    recommendations: List[Recommendation],
    account_name: str = "",
    max_display: int = 50,
) -> None:
    """Render a Rich table of recommendations for an account."""
    if not recommendations:
        console.print(
            f"[green]No recommendations for {account_name or 'this account'}.[/green]"
        )
        return

    title = f"Recommendations: {account_name}" if account_name else "Recommendations"
    table = Table(
        title=f"[bold cyan]{title}[/bold cyan]",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )

    table.add_column("#", style="dim", width=4)
    table.add_column("ID", style="dim", width=8)
    table.add_column("Priority", width=10)
    table.add_column("Type", width=12)
    table.add_column("Title", ratio=3)
    table.add_column("Impact", justify="center", width=16)
    table.add_column("Est. ΔCost", justify="right", width=12)

    for idx, rec in enumerate(recommendations[:max_display], start=1):
        priority_color = PRIORITY_COLORS.get(rec.priority, "white")
        icon = REC_TYPE_ICONS.get(rec.rec_type, "•")
        rec_type_short = rec.rec_type.replace("KEYWORD_", "KW_").replace("BUDGET_", "BUD_")

        cost_delta = rec.estimated_cost_delta
        if cost_delta is not None:
            cost_str = (
                f"[green]-${abs(cost_delta):.2f}[/green]"
                if cost_delta < 0
                else f"[red]+${cost_delta:.2f}[/red]"
            )
        else:
            cost_str = ""

        table.add_row(
            str(idx),
            rec.id,
            Text(rec.priority, style=priority_color),
            f"{icon} {rec_type_short}",
            rec.title[:80],
            _impact_bar(rec.impact_score) + f" {rec.impact_score:.1f}",
            Text(cost_str),
        )

    if len(recommendations) > max_display:
        console.print(
            f"[dim]... and {len(recommendations) - max_display} more recommendations[/dim]"
        )

    console.print(table)
    console.print(
        f"[bold]Total: {len(recommendations)} recommendations[/bold] | "
        f"[red]{sum(1 for r in recommendations if r.priority in ('CRITICAL', 'HIGH'))} high priority[/red] | "
        f"[yellow]{sum(1 for r in recommendations if r.priority == 'MEDIUM')} medium[/yellow]"
    )


def display_recommendation_detail(rec: Recommendation) -> None:
    """Show detailed information about a single recommendation."""
    priority_color = PRIORITY_COLORS.get(rec.priority, "white")

    content = (
        f"[bold]Type:[/bold] {rec.rec_type}\n"
        f"[bold]Priority:[/bold] [{priority_color}]{rec.priority}[/{priority_color}]\n"
        f"[bold]Impact Score:[/bold] {rec.impact_score:.1f}/10\n\n"
        f"[bold]Description:[/bold]\n{rec.description}\n\n"
        f"[bold]Rationale:[/bold]\n{rec.rationale}\n"
    )

    if rec.campaign_name:
        content += f"\n[bold]Campaign:[/bold] {rec.campaign_name}"
    if rec.ad_group_name:
        content += f"\n[bold]Ad Group:[/bold] {rec.ad_group_name}"
    if rec.estimated_cost_delta is not None:
        delta = rec.estimated_cost_delta
        delta_str = f"[green]-${abs(delta):.2f}[/green]" if delta < 0 else f"[red]+${delta:.2f}[/red]"
        content += f"\n[bold]Est. Cost Delta:[/bold] {delta_str}"
    if rec.estimated_conversion_delta is not None:
        content += f"\n[bold]Est. Conversion Delta:[/bold] +{rec.estimated_conversion_delta:.1f}"

    console.print(Panel(content, title=f"[bold]{rec.title[:80]}[/bold]", border_style="cyan"))


def interactive_review(
    recommendations: List[Recommendation],
    apply_fn: Callable[[Recommendation], bool],
    account_name: str = "",
) -> Tuple[int, int, int]:
    """Interactive TUI loop for reviewing and applying recommendations.

    Args:
        recommendations: List of recommendations to review.
        apply_fn:        Callable that applies a recommendation and returns success bool.
        account_name:    Human-readable account name for display.

    Returns:
        Tuple of (applied_count, skipped_count, failed_count).
    """
    pending = [r for r in recommendations if r.status == RecommendationStatus.PENDING]

    if not pending:
        console.print("[yellow]No pending recommendations to review.[/yellow]")
        return 0, 0, 0

    applied = skipped = failed = 0

    console.print(
        Panel(
            f"[bold cyan]Interactive Review[/bold cyan]\n"
            f"Account: [white]{account_name}[/white]\n"
            f"Pending recommendations: [bold]{len(pending)}[/bold]\n\n"
            f"For each recommendation:\n"
            f"  [green][A]pply[/green] - Apply this change\n"
            f"  [yellow][S]kip[/yellow]  - Skip this recommendation\n"
            f"  [red][Q]uit[/red]  - Stop reviewing\n"
            f"  [blue][D]etail[/blue] - Show full details",
            title="[bold]Recommendation Review[/bold]",
            border_style="cyan",
        )
    )

    for idx, rec in enumerate(pending, start=1):
        console.rule(f"[bold]Recommendation {idx}/{len(pending)}[/bold]")

        priority_color = PRIORITY_COLORS.get(rec.priority, "white")
        icon = REC_TYPE_ICONS.get(rec.rec_type, "•")

        console.print(
            f"  [{priority_color}][{rec.priority}][/{priority_color}] "
            f"{icon} [bold]{rec.title[:80]}[/bold]"
        )
        console.print(f"  [dim]{rec.description[:200]}[/dim]")

        impact_bar = _impact_bar(rec.impact_score)
        console.print(f"  Impact: {impact_bar} {rec.impact_score:.1f}/10")

        if rec.estimated_cost_delta is not None:
            delta = rec.estimated_cost_delta
            delta_str = f"[green]-${abs(delta):.2f}[/green]" if delta < 0 else f"[red]+${delta:.2f}[/red]"
            console.print(f"  Est. cost delta: {delta_str}")

        while True:
            choice = Prompt.ask(
                "\n  [green][A]pply[/green] / [yellow][S]kip[/yellow] / [red][Q]uit[/red] / [blue][D]etail[/blue]",
                choices=["a", "s", "q", "d", "A", "S", "Q", "D"],
                default="s",
            ).lower()

            if choice == "d":
                display_recommendation_detail(rec)
                continue
            break

        if choice == "q":
            console.print("[bold red]Quitting review.[/bold red]")
            break
        elif choice == "a":
            with console.status("[bold green]Applying...[/bold green]"):
                success = apply_fn(rec)
            if success:
                applied += 1
                console.print(f"  [green]Applied successfully.[/green]")
            else:
                failed += 1
                console.print(
                    f"  [red]Failed to apply.[/red] "
                    f"{rec.error_message or 'See logs for details.'}"
                )
        else:
            rec.mark_skipped()
            skipped += 1
            console.print("  [yellow]Skipped.[/yellow]")

    # Summary
    console.print()
    console.print(
        Panel(
            f"[bold]Review Complete[/bold]\n\n"
            f"[green]Applied:[/green]  {applied}\n"
            f"[yellow]Skipped:[/yellow]  {skipped}\n"
            f"[red]Failed:[/red]   {failed}\n"
            f"[dim]Total reviewed: {applied + skipped + failed}[/dim]",
            title="Summary",
            border_style="green" if failed == 0 else "yellow",
        )
    )

    return applied, skipped, failed


def make_progress() -> Progress:
    """Create a Rich progress bar for data fetching."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    )
