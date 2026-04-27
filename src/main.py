"""Google Ads MCC Optimization Tool — CLI Entry Point."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

load_dotenv()

from .auth.google_ads_auth import GoogleAdsAuthenticator
from .api.mcc_client import MCCClient
from .api.account_client import AccountClient
from .api.campaign_client import CampaignClient
from .recommendations.recommendation_engine import RecommendationEngine
from .recommendations.recommendation import Recommendation, RecommendationType
from .appliers.keyword_applier import KeywordApplier
from .appliers.bid_applier import BidApplier
from .appliers.budget_applier import BudgetApplier
from .appliers.ad_applier import AdApplier
from .ui.cli import (
    display_accounts_table,
    display_recommendations,
    display_recommendation_detail,
    interactive_review,
    make_progress,
    console,
)

logger = logging.getLogger(__name__)


def _configure_logging(log_level: str = "INFO") -> None:
    """Set up logging configuration."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _get_authenticator(yaml_path: Optional[str]) -> GoogleAdsAuthenticator:
    """Build an authenticator from options or defaults."""
    use_env = not yaml_path and not os.path.exists(
        os.getenv("GOOGLE_ADS_YAML_PATH", "config/google-ads.yaml")
    )
    return GoogleAdsAuthenticator(yaml_path=yaml_path, use_env=use_env)


def _fetch_account_data(
    account_client: AccountClient,
    campaign_client: CampaignClient,
    date_range: str,
) -> Dict[str, Any]:
    """Fetch all data needed for analysis for a single account."""
    data: Dict[str, Any] = {}

    with make_progress() as progress:
        task = progress.add_task("Fetching campaigns...", total=5)
        data["campaigns"] = account_client.get_campaigns(date_range)
        progress.advance(task)

        progress.update(task, description="Fetching ad groups...")
        data["ad_groups"] = account_client.get_ad_groups(date_range)
        progress.advance(task)

        progress.update(task, description="Fetching keywords...")
        data["keywords"] = campaign_client.get_keywords(date_range)
        progress.advance(task)

        progress.update(task, description="Fetching ads...")
        data["ads"] = campaign_client.get_ads(date_range)
        progress.advance(task)

        progress.update(task, description="Fetching search terms...")
        data["search_terms"] = campaign_client.get_search_terms(date_range)
        progress.advance(task)

    return data


def _get_applier_for_recommendation(
    rec: Recommendation,
    authenticator: GoogleAdsAuthenticator,
    db_path: str,
):
    """Return the appropriate applier for a given recommendation type."""
    client = authenticator.get_client()
    keyword_types = {
        RecommendationType.KEYWORD_LOW_QUALITY_SCORE,
        RecommendationType.KEYWORD_ADD_NEGATIVE,
        RecommendationType.KEYWORD_OPPORTUNITY,
        RecommendationType.KEYWORD_DUPLICATE,
    }
    bid_types = {
        RecommendationType.BID_INCREASE,
        RecommendationType.BID_DECREASE,
    }
    budget_types = {
        RecommendationType.BUDGET_LIMITED,
        RecommendationType.BUDGET_REALLOCATION,
    }
    ad_types = {
        RecommendationType.AD_PAUSE_LOW_PERFORMER,
        RecommendationType.AD_ADD_RESPONSIVE,
    }

    rec_type = rec.rec_type
    if rec_type in keyword_types:
        return KeywordApplier(client, db_path)
    elif rec_type in bid_types:
        return BidApplier(client, db_path)
    elif rec_type in budget_types:
        return BudgetApplier(client, db_path)
    elif rec_type in ad_types:
        return AdApplier(client, db_path)
    else:
        return None


# ================================================================== #
# CLI
# ================================================================== #

@click.group()
@click.option(
    "--yaml-path",
    envvar="GOOGLE_ADS_YAML_PATH",
    default=None,
    help="Path to google-ads.yaml config file.",
)
@click.option(
    "--log-level",
    envvar="LOG_LEVEL",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging verbosity.",
)
@click.option(
    "--db-path",
    envvar="CHANGES_LOG_DB",
    default="changes_log.db",
    help="Path to SQLite changes log database.",
)
@click.pass_context
def cli(ctx: click.Context, yaml_path: Optional[str], log_level: str, db_path: str) -> None:
    """Google Ads MCC Optimization Tool.

    Fetches data from your Google Ads MCC, generates actionable recommendations,
    and lets you apply them interactively or via sub-commands.
    """
    _configure_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["yaml_path"] = yaml_path
    ctx.obj["db_path"] = db_path
    ctx.obj["log_level"] = log_level


@cli.command("list-accounts")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """List all MCC child accounts with 30-day performance summary."""
    auth = _get_authenticator(ctx.obj.get("yaml_path"))
    try:
        client = auth.get_client()
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        sys.exit(1)

    mcc_id = auth.login_customer_id
    if not mcc_id:
        console.print(
            "[red]MCC customer ID not configured. "
            "Set GOOGLE_ADS_LOGIN_CUSTOMER_ID or use google-ads.yaml.[/red]"
        )
        sys.exit(1)

    mcc_client = MCCClient(client, mcc_id)
    with console.status("Fetching accounts..."):
        accounts = mcc_client.get_accounts_with_performance()

    if not accounts:
        console.print("[yellow]No active client accounts found under MCC.[/yellow]")
        return

    display_accounts_table(accounts)
    console.print(f"\n[bold]Total: {len(accounts)} active accounts[/bold]")


@cli.command("analyze")
@click.option(
    "--account-id",
    default=None,
    help="Specific account ID to analyze (default: all accounts).",
)
@click.option(
    "--days",
    default=30,
    show_default=True,
    help="Number of days to look back for data.",
    type=click.IntRange(1, 90),
)
@click.pass_context
def analyze(ctx: click.Context, account_id: Optional[str], days: int) -> None:
    """Analyze accounts and display recommendations (no changes applied)."""
    date_range = f"LAST_{days}_DAYS" if days != 30 else "LAST_30_DAYS"
    if days == 7:
        date_range = "LAST_7_DAYS"
    elif days == 14:
        date_range = "LAST_14_DAYS"
    elif days == 30:
        date_range = "LAST_30_DAYS"

    auth = _get_authenticator(ctx.obj.get("yaml_path"))
    try:
        client = auth.get_client()
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        sys.exit(1)

    mcc_id = auth.login_customer_id
    mcc_client = MCCClient(client, mcc_id)

    # Determine which accounts to analyze
    if account_id:
        accounts = [{"id": account_id, "name": account_id, "is_manager": False, "is_test": False}]
    else:
        with console.status("Fetching account list..."):
            accounts = mcc_client.list_accounts()
        accounts = [a for a in accounts if not a.get("is_manager") and not a.get("is_test")]

    if not accounts:
        console.print("[yellow]No accounts to analyze.[/yellow]")
        return

    console.print(
        Panel(
            f"[bold cyan]Analyzing {len(accounts)} account(s)[/bold cyan]\n"
            f"Date range: {date_range}",
            border_style="cyan",
        )
    )

    engine = RecommendationEngine()
    all_recs = []

    for acc in accounts:
        cid = acc["id"]
        cname = acc.get("name", cid)
        console.print(f"\n[bold]Account:[/bold] {cname} ({cid})")

        try:
            acc_client = AccountClient(client, cid)
            camp_client = CampaignClient(client, cid)
            data = _fetch_account_data(acc_client, camp_client, date_range)
            recs = engine.analyze_account(cid, cname, data, date_range)
            display_recommendations(recs, account_name=cname)
            all_recs.extend(recs)
        except Exception as exc:
            console.print(f"[red]Error analyzing {cname}: {exc}[/red]")
            logger.error("Error analyzing account %s: %s", cid, exc, exc_info=True)

    console.print(
        f"\n[bold green]Analysis complete.[/bold green] "
        f"Total: [bold]{len(all_recs)}[/bold] recommendations across "
        f"[bold]{len(accounts)}[/bold] account(s)."
    )


@cli.command("apply")
@click.option("--account-id", required=True, help="Account ID to apply changes to.")
@click.option("--recommendation-id", required=True, help="Recommendation ID to apply.")
@click.pass_context
def apply_recommendation(
    ctx: click.Context, account_id: str, recommendation_id: str
) -> None:
    """Apply a specific recommendation by ID (requires prior analyze run).

    Note: This command applies the recommendation described by its ID.
    Use the 'interactive' command for the full workflow.
    """
    console.print(
        "[yellow]The 'apply' command is designed for scripting. "
        "For the full interactive workflow, use the 'interactive' command.[/yellow]"
    )
    console.print(
        f"To apply recommendation [bold]{recommendation_id}[/bold] for account "
        f"[bold]{account_id}[/bold], run:\n\n"
        f"  google-ads-opt interactive --account-id {account_id}"
    )


@cli.command("interactive")
@click.option(
    "--account-id",
    default=None,
    help="Specific account ID (default: all accounts).",
)
@click.option(
    "--days",
    default=30,
    show_default=True,
    help="Number of days to look back.",
    type=click.IntRange(1, 90),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show recommendations but do not apply any changes.",
)
@click.pass_context
def interactive(
    ctx: click.Context,
    account_id: Optional[str],
    days: int,
    dry_run: bool,
) -> None:
    """Full interactive TUI workflow: fetch, analyze, review, and apply.

    This is the recommended way to use the tool. It will:
    1. List your MCC accounts
    2. Fetch data for the selected period
    3. Run all analyzers
    4. Present recommendations interactively
    5. Apply approved changes (unless --dry-run)
    """
    date_range = "LAST_30_DAYS"
    if days == 7:
        date_range = "LAST_7_DAYS"
    elif days == 14:
        date_range = "LAST_14_DAYS"

    if dry_run:
        console.print("[yellow]DRY RUN MODE — no changes will be applied.[/yellow]")

    auth = _get_authenticator(ctx.obj.get("yaml_path"))
    db_path = ctx.obj.get("db_path", "changes_log.db")

    try:
        client = auth.get_client()
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        console.print(
            "[dim]Make sure you have a valid config/google-ads.yaml or .env file.[/dim]"
        )
        sys.exit(1)

    mcc_id = auth.login_customer_id
    if not mcc_id:
        console.print("[red]MCC customer ID not configured.[/red]")
        sys.exit(1)

    mcc_client = MCCClient(client, mcc_id)

    # Step 1: Get account list
    with console.status("Fetching MCC accounts..."):
        if account_id:
            accounts = [{"id": account_id, "name": account_id, "is_manager": False, "is_test": False}]
        else:
            accounts = mcc_client.list_accounts()
            accounts = [
                a for a in accounts if not a.get("is_manager") and not a.get("is_test")
            ]

    if not accounts:
        console.print("[yellow]No accounts found.[/yellow]")
        return

    # Step 2: Show accounts summary
    console.print(
        Panel(
            f"[bold cyan]Google Ads MCC Optimizer[/bold cyan]\n"
            f"Found [bold]{len(accounts)}[/bold] account(s) | "
            f"Date range: [bold]{date_range}[/bold]",
            border_style="cyan",
        )
    )

    engine = RecommendationEngine()
    total_applied = total_skipped = total_failed = 0

    for acc in accounts:
        cid = acc["id"]
        cname = acc.get("name", cid)

        console.rule(f"[bold cyan]{cname}[/bold cyan] ({cid})")

        try:
            # Step 3: Fetch data
            acc_client = AccountClient(client, cid)
            camp_client = CampaignClient(client, cid)
            data = _fetch_account_data(acc_client, camp_client, date_range)

            # Step 4: Analyze
            with console.status("Analyzing..."):
                recs = engine.analyze_account(cid, cname, data, date_range)

            if not recs:
                console.print(f"[green]No optimization opportunities found for {cname}.[/green]")
                continue

            # Step 5: Display recommendations
            display_recommendations(recs, account_name=cname)

            if dry_run:
                console.print("[yellow](dry run — skipping interactive review)[/yellow]")
                continue

            # Step 6: Interactive review
            def make_apply_fn(authenticator: GoogleAdsAuthenticator, _db_path: str):
                """Closure to create a per-account apply function."""
                def apply_fn(rec: Recommendation) -> bool:
                    applier = _get_applier_for_recommendation(rec, authenticator, _db_path)
                    if applier is None:
                        console.print(
                            f"[yellow]No applier available for {rec.rec_type}[/yellow]"
                        )
                        return False
                    return applier.apply_safe(rec)
                return apply_fn

            applied, skipped, failed = interactive_review(
                recs,
                apply_fn=make_apply_fn(auth, db_path),
                account_name=cname,
            )
            total_applied += applied
            total_skipped += skipped
            total_failed += failed

        except Exception as exc:
            console.print(f"[red]Error processing account {cname}: {exc}[/red]")
            logger.error("Error processing account %s: %s", cid, exc, exc_info=True)

    # Final summary
    if not dry_run:
        console.print(
            Panel(
                f"[bold]All Accounts Summary[/bold]\n\n"
                f"[green]Applied:[/green]  {total_applied}\n"
                f"[yellow]Skipped:[/yellow]  {total_skipped}\n"
                f"[red]Failed:[/red]   {total_failed}\n"
                f"\n[dim]Changes logged to: {db_path}[/dim]",
                title="[bold green]Session Complete[/bold green]",
                border_style="green" if total_failed == 0 else "yellow",
            )
        )


def main() -> None:
    """Entry point for the CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()
