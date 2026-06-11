#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/cli.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: Gmail CLI Monitor — main CLI entry point.
# License: MIT

"""
Gmail CLI Monitor — main CLI entry point.

Commands
--------
  monitor     Start monitoring all enabled accounts (default)
  list        Show recent email history
  accounts    List configured accounts
  add         Add a new account interactively
  remove      Remove an account
  init-config Write an example config.toml
  purge       Delete old emails from history
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from . import __version__
from .config import ConfigManager, bootstrap_db_config
from .db import DatabaseManager
from .display import (
    console,
    print_accounts_table,
    print_banner,
    print_email_table,
    print_new_email,
)
from .models import EmailAccount, EmailMessage
from .monitor import EmailMonitor
from .notifier import GrowlNotifier

app = typer.Typer(
    name="gmail-monitor",
    help="Multi-account email monitor with Growl notifications.",
    add_completion=False,
    rich_markup_mode="rich",
)

_email_counter = 0  # global counter for display numbering


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _setup_logging(level: str = "INFO", logfile: str = "gmail_monitor.log") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=handlers,
    )
    # Silence noisy third-party loggers
    for noisy in ("imaplib", "gntp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_app(cfg: dict) -> tuple[DatabaseManager, ConfigManager, GrowlNotifier]:
    """Bootstrap DB, config manager, and notifier from a db-config dict.

    ``cfg`` is already the database sub-dict returned by bootstrap_db_config()
    (keys: host/port/database/user/password).  Pass it straight through —
    do NOT call .get("database") on it again or you get the DB name string.
    """
    db = DatabaseManager(cfg)
    db.connect()

    cm = ConfigManager(db)
    full_cfg = cm.load()

    # Push any config.toml accounts/settings into DB so they survive
    cm.sync_to_db()

    notifier = GrowlNotifier(full_cfg.get("growl", {}))
    return db, cm, notifier


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def monitor(
    config_file: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.toml (default: ./config.toml)"
    ),
    log_level: str = typer.Option("INFO", "--log-level", "-l", help="Log level"),
):
    """[bold green]Start monitoring all enabled accounts.[/bold green]"""
    if config_file:
        ConfigManager.CONFIG_FILE = config_file

    print_banner(__version__)

    db_cfg = bootstrap_db_config()
    _setup_logging(log_level)

    try:
        db, cm, notifier = _build_app(db_cfg)
    except Exception as exc:
        console.print(f"[red]✗ Startup failed: {exc}[/red]")
        raise typer.Exit(1)

    cfg = cm.load()
    accounts = cm.get_accounts()

    if not accounts:
        console.print(
            "[yellow]⚠  No accounts configured.\n"
            "   Run [bold]gmail-monitor init-config[/bold] then edit config.toml[/yellow]"
        )
        raise typer.Exit(1)

    mon_cfg = cfg.get("monitoring", {})

    console.print(f"[green]✓ Loaded {len(accounts)} account(s)[/green]")
    if notifier.enabled:
        console.print("[green]✓ Growl notifications active[/green]")
    else:
        console.print("[yellow]⚠  Growl unavailable — terminal-only mode[/yellow]")

    monitors = [
        EmailMonitor(
            account=acc,
            db=db,
            notifier=notifier,
            check_interval=mon_cfg.get("check_interval", 60),
            max_emails=mon_cfg.get("max_emails_per_check", 10),
            reconnect_delay=mon_cfg.get("reconnect_delay", 30),
            max_reconnect_attempts=mon_cfg.get("max_reconnect_attempts", 0),
            mark_as_read=mon_cfg.get("mark_as_read", True),
            on_new_email=_on_new_email,
        )
        for acc in accounts
    ]

    asyncio.run(_run_monitors(monitors, db))


async def _run_monitors(monitors: list[EmailMonitor], db: DatabaseManager) -> None:
    """Run all monitor coroutines concurrently with clean signal handling."""
    loop = asyncio.get_running_loop()

    def _shutdown(sig_name: str):
        console.print(f"\n[yellow]Received {sig_name} — shutting down…[/yellow]")
        for m in monitors:
            m.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler for all signals
            pass

    try:
        await asyncio.gather(*[m.run() for m in monitors])
    finally:
        db.close()
        console.print("[green]✓ Goodbye![/green]")


def _on_new_email(msg: EmailMessage) -> None:
    global _email_counter
    _email_counter += 1
    print_new_email(msg, _email_counter)


# ──────────────────────────────────────────────────────────────────────────────


@app.command("list")
def list_emails(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Filter by account email"),
    limit: int = typer.Option(50, "--limit", "-n", help="Number of rows to show"),
):
    """[bold cyan]Show recent email history.[/bold cyan]"""
    db_cfg = bootstrap_db_config()
    _setup_logging("WARNING")
    db = DatabaseManager(db_cfg)
    try:
        db.connect()
        rows = db.get_history(account=account, limit=limit)
        title = f"Last {limit} emails" + (f" — {account}" if account else "")
        print_email_table(rows, title=title)
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1)
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def accounts():
    """[bold cyan]List all configured accounts.[/bold cyan]"""
    db_cfg = bootstrap_db_config()
    _setup_logging("WARNING")
    db = DatabaseManager(db_cfg)
    try:
        db.connect()
        accs = db.load_accounts()
        if not accs:
            console.print("[yellow]No accounts in database. Check config.toml.[/yellow]")
        else:
            print_accounts_table(accs)
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1)
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def add(
    email_addr: Optional[str] = typer.Option(None, "--email", "-e", help="Email address"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="IMAP password / app-password"),
    server: str = typer.Option("imap.gmail.com", "--server", "-s", help="IMAP server"),
    port: int = typer.Option(993, "--port", help="IMAP port"),
):
    """[bold cyan]Add a new email account.[/bold cyan]"""
    # Interactive fallback
    if not email_addr:
        email_addr = Prompt.ask("[cyan]Email address[/cyan]")
    if not password:
        password = Prompt.ask("[cyan]Password / App Password[/cyan]", password=True)

    account = EmailAccount(
        email=email_addr.strip(),
        password=password,
        imap_server=server,
        imap_port=port,
        enabled=True,
    )

    db_cfg = bootstrap_db_config()
    _setup_logging("WARNING")
    db = DatabaseManager(db_cfg)
    try:
        db.connect()
        db.save_account(account)
        console.print(f"[green]✓ Account [bold]{email_addr}[/bold] saved.[/green]")
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1)
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def remove(
    email_addr: str = typer.Argument(..., help="Email address to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """[bold red]Remove an account and its email history.[/bold red]"""
    if not yes:
        confirmed = Confirm.ask(
            f"[yellow]Remove [bold]{email_addr}[/bold] and all its history?[/yellow]"
        )
        if not confirmed:
            raise typer.Abort()

    db_cfg = bootstrap_db_config()
    _setup_logging("WARNING")
    db = DatabaseManager(db_cfg)
    try:
        db.connect()
        deleted = db.delete_account(email_addr)
        if deleted:
            console.print(f"[green]✓ Account [bold]{email_addr}[/bold] removed.[/green]")
        else:
            console.print(f"[yellow]Account {email_addr!r} not found.[/yellow]")
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1)
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────────


@app.command("init-config")
def init_config(
    output: Path = typer.Option(
        Path("config.toml"), "--output", "-o", help="Where to write the example config"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing file"),
):
    """[bold cyan]Write an example config.toml to get started.[/bold cyan]"""
    if output.exists() and not overwrite:
        console.print(
            f"[yellow]{output} already exists. Use --overwrite to replace it.[/yellow]"
        )
        raise typer.Exit(1)

    # Use a temporary no-op DB (won't be called) for ConfigManager
    class _FakeDB:  # noqa: D101
        pass

    cm = ConfigManager(_FakeDB())  # type: ignore[arg-type]
    cm.write_example(output)
    console.print(f"[green]✓ Example config written to [bold]{output}[/bold][/green]")
    console.print(
        "[dim]Edit the file and add your accounts, then run:[/dim]\n"
        "  [bold]gmail-monitor monitor[/bold]"
    )


# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def purge(
    days: int = typer.Option(30, "--days", "-d", help="Delete emails older than N days"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """[bold red]Purge email history older than N days.[/bold red]"""
    if not yes:
        confirmed = Confirm.ask(
            f"[yellow]Delete all emails older than [bold]{days} days[/bold]?[/yellow]"
        )
        if not confirmed:
            raise typer.Abort()

    db_cfg = bootstrap_db_config()
    _setup_logging("WARNING")
    db = DatabaseManager(db_cfg)
    try:
        db.connect()
        count = db.purge_old_emails(days)
        console.print(f"[green]✓ Deleted [bold]{count}[/bold] email(s).[/green]")
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1)
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def version():
    """Show version."""
    console.print(f"gmail-monitor [bold yellow]{__version__}[/bold yellow]")


# ──────────────────────────────────────────────────────────────────────────────


def main():
    app()


if __name__ == "__main__":
    main()
