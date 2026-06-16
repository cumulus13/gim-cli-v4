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

logger = logging.getLogger(__name__)

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


def _build_app(cfg: dict) -> tuple[DatabaseManager, ConfigManager, dict, GrowlNotifier]:
    """Bootstrap DB, config manager, and notifier from a db-config dict.

    ``cfg`` is already the database sub-dict returned by bootstrap_db_config()
    (keys: host/port/database/user/password).  Pass it straight through —
    do NOT call .get("database") on it again or you get the DB name string.

    Returns (db, cm, full_cfg, notifier) — full_cfg is returned so the caller
    does not need to call cm.load() a second time (which re-reads the file and
    logs "Loading config" again unnecessarily).
    """
    db = DatabaseManager(cfg)
    db.connect()

    cm = ConfigManager(db)
    full_cfg = cm.load()

    # Push any config.toml accounts/settings into DB so they survive
    cm.sync_to_db()

    notifier = GrowlNotifier(full_cfg.get("growl", {}))
    return db, cm, full_cfg, notifier


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def monitor(
    config_file: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.toml (default: ./config.toml)"
    ),
    log_level: str = typer.Option("INFO", "--log-level", "-l", help="Log level"),
    reload_interval: int = typer.Option(
        30, "--reload-interval", "-r",
        help="Seconds between config-file checks for hot-reload. 0 disables hot-reload.",
    ),
):
    """[bold green]Start monitoring all enabled accounts.[/bold green]"""
    if config_file:
        ConfigManager.CONFIG_FILE = config_file

    print_banner(__version__)

    db_cfg = bootstrap_db_config()
    # Setup logging BEFORE _build_app so early log messages (DB connect,
    # config load) go to the configured handlers, not the default stderr handler.
    _setup_logging(log_level)

    try:
        db, cm, cfg, notifier = _build_app(db_cfg)
    except Exception as exc:
        console.print(f"[red]✗ Startup failed: {exc}[/red]")
        raise typer.Exit(1)

    # cfg already loaded by _build_app — no second cm.load() needed
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
            max_emails=mon_cfg.get("max_emails_per_check", 50),
            reconnect_delay=mon_cfg.get("reconnect_delay", 30),
            max_reconnect_attempts=mon_cfg.get("max_reconnect_attempts", 0),
            mark_as_read=mon_cfg.get("mark_as_read", True),
            socket_timeout=mon_cfg.get("socket_timeout", 60),
            on_new_email=_on_new_email,
        )
        for acc in accounts
    ]

    asyncio.run(_run_monitors(monitors, db, cm, mon_cfg, notifier, reload_interval))


async def _run_monitors(
    monitors: list[EmailMonitor],
    db: DatabaseManager,
    cm: ConfigManager,
    mon_cfg: dict,
    notifier: GrowlNotifier,
    reload_interval: int = 30,
) -> None:
    """
    Run all monitor coroutines concurrently with clean signal handling
    and optional hot-reload of config.toml.

    Hot-reload behaviour (reload_interval > 0)
    -------------------------------------------
    Every ``reload_interval`` seconds, check config.toml's mtime.  If it
    changed:
      - accounts added in config.toml  -> new EmailMonitor + task started
      - accounts removed / disabled    -> existing monitor stopped & dropped
      - accounts still present         -> credentials/server applied live
                                           (forces reconnect only if changed)
      - [monitoring] section changes   -> applied live to ALL monitors via
                                           update_settings() (no restart)

    reload_interval = 0 disables hot-reload entirely (legacy behaviour).
    """
    loop = asyncio.get_running_loop()
    shutdown_requested = False

    def _shutdown(sig_name: str):
        nonlocal shutdown_requested
        console.print(f"\n[yellow]Received {sig_name} — shutting down…[/yellow]")
        shutdown_requested = True
        for m in monitor_by_email.values():
            m.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler for all signals
            pass

    # Track monitors by account email so we can diff on reload
    monitor_by_email: dict[str, EmailMonitor] = {m.account.email: m for m in monitors}
    tasks: dict[str, asyncio.Task] = {
        email: asyncio.create_task(m.run(), name=f"monitor:{email}")
        for email, m in monitor_by_email.items()
    }

    last_mtime: Optional[float] = None
    try:
        cfg_path = cm.CONFIG_FILE
        if cfg_path.exists():
            last_mtime = cfg_path.stat().st_mtime
    except OSError:
        pass

    try:
        while not shutdown_requested:
            # Wait reload_interval seconds OR until all tasks finish,
            # whichever comes first.
            if not tasks:
                # No tasks running — wait a bit then check for reload or shutdown
                await asyncio.sleep(reload_interval if reload_interval > 0 else 5)
            elif reload_interval > 0:
                done, _ = await asyncio.wait(
                    tasks.values(),
                    timeout=reload_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                done, _ = await asyncio.wait(
                    tasks.values(), return_when=asyncio.ALL_COMPLETED
                )

            # Remove finished tasks (monitor gave up permanently, or stopped)
            for email, task in list(tasks.items()):
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        logger.error("Monitor for %s crashed: %s", email, exc)
                    else:
                        logger.info("Monitor for %s stopped", email)
                    del tasks[email]
                    monitor_by_email.pop(email, None)

            if shutdown_requested:
                break

            if not tasks:
                # All monitors gave up / stopped and hot-reload is off,
                # or every account was removed via hot-reload.
                if reload_interval == 0:
                    console.print("[yellow]All monitors have stopped.[/yellow]")
                    break
                # With hot-reload on, keep the process alive — the user may
                # fix config.toml (e.g. bad credentials) and we'll pick the
                # account back up on the next reload tick below.

            if reload_interval <= 0:
                continue

            # ── Hot-reload check ────────────────────────────────────────
            try:
                cfg_path = cm.CONFIG_FILE
                if not cfg_path.exists():
                    continue
                mtime = cfg_path.stat().st_mtime
                if last_mtime is not None and mtime == last_mtime:
                    continue
                last_mtime = mtime

                console.print(f"[cyan]↻ config.toml changed — reloading…[/cyan]")
                new_cfg = cm.load()
                cm.sync_to_db()
                new_mon_cfg = new_cfg.get("monitoring", {})
                new_accounts = {a.email: a for a in cm.get_accounts()}

                # 1. Stop monitors for accounts removed/disabled.
                # Signal stop() and drop from monitor_by_email immediately
                # so the active-count below is accurate; the task itself
                # is reaped from `tasks` on the next asyncio.wait() pass.
                for email in list(monitor_by_email.keys()):
                    if email not in new_accounts:
                        console.print(f"[yellow]  − removing account {email}[/yellow]")
                        monitor_by_email[email].stop()
                        del monitor_by_email[email]

                # 2. Update existing monitors (settings + possible credential change)
                for email, account in new_accounts.items():
                    if email in monitor_by_email:
                        m = monitor_by_email[email]
                        m.apply_account(account)
                        m.update_settings(
                            check_interval=new_mon_cfg.get("check_interval"),
                            max_emails=new_mon_cfg.get("max_emails_per_check"),
                            reconnect_delay=new_mon_cfg.get("reconnect_delay"),
                            max_reconnect_attempts=new_mon_cfg.get("max_reconnect_attempts"),
                            mark_as_read=new_mon_cfg.get("mark_as_read"),
                            socket_timeout=new_mon_cfg.get("socket_timeout"),
                        )

                # 3. Start monitors for newly added accounts
                for email, account in new_accounts.items():
                    if email not in monitor_by_email:
                        console.print(f"[green]  + adding account {email}[/green]")
                        new_monitor = EmailMonitor(
                            account=account,
                            db=db,
                            notifier=notifier,
                            check_interval=new_mon_cfg.get("check_interval", 60),
                            max_emails=new_mon_cfg.get("max_emails_per_check", 50),
                            reconnect_delay=new_mon_cfg.get("reconnect_delay", 30),
                            max_reconnect_attempts=new_mon_cfg.get("max_reconnect_attempts", 0),
                            mark_as_read=new_mon_cfg.get("mark_as_read", True),
                            socket_timeout=new_mon_cfg.get("socket_timeout", 60),
                            on_new_email=_on_new_email,
                        )
                        monitor_by_email[email] = new_monitor
                        tasks[email] = asyncio.create_task(
                            new_monitor.run(), name=f"monitor:{email}"
                        )

                console.print(
                    f"[cyan]↻ reload complete — {len(monitor_by_email)} account(s) active[/cyan]"
                )

            except Exception as exc:
                logger.exception("Hot-reload failed: %s", exc)
                console.print(f"[red]✗ Hot-reload error: {exc}[/red]")

        # ── Shutdown: wait for all remaining tasks to finish cleanly ──────
        if tasks:
            await asyncio.wait(tasks.values(), timeout=30)

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
