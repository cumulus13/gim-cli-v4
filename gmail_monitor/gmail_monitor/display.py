#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/display.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: Rich terminal display helpers.
# License: MIT

"""Rich terminal display helpers."""

from datetime import datetime
from typing import Dict, List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import EmailMessage

console = Console()


def print_banner(version: str = "2.0.0") -> None:
    console.print(
        Panel.fit(
            f"[bold yellow]Gmail CLI Monitor[/bold yellow]  [dim]v{version}[/dim]\n"
            "[dim white]Multi-account email monitoring with Growl notifications[/dim white]",
            border_style="yellow",
        )
    )


# ── Content-type → emoji mapping ────────────────────────────────────────────
_CTYPE_EMOJI: dict = {
    "text/plain":       ("📄", "plain"),
    "text/html":        ("🌐", "html"),
    "text/calendar":    ("📅", "calendar"),
    "text/csv":         ("📊", "csv"),
    "multipart/mixed":  ("📦", "mixed"),
    "multipart/alternative": ("🔀", "alt"),
    "multipart/related":     ("🔗", "related"),
    "image/jpeg":       ("🖼",  "jpeg"),
    "image/png":        ("🖼",  "png"),
    "image/gif":        ("🎞",  "gif"),
    "image/webp":       ("🖼",  "webp"),
    "image/svg+xml":    ("🖼",  "svg"),
    "application/pdf":  ("📕", "pdf"),
    "application/zip":  ("🗜",  "zip"),
    "application/x-zip-compressed": ("🗜", "zip"),
    "application/gzip": ("🗜",  "gz"),
    "application/msword":                                         ("📝", "doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("📝", "docx"),
    "application/vnd.ms-excel":                                   ("📊", "xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("📊", "xlsx"),
    "application/vnd.ms-powerpoint":                              ("📊", "ppt"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("📊", "pptx"),
    "audio/mpeg":       ("🎵", "mp3"),
    "audio/ogg":        ("🎵", "ogg"),
    "video/mp4":        ("🎬", "mp4"),
    "video/mpeg":       ("🎬", "mpeg"),
    "application/octet-stream": ("💾", "bin"),
}

def _ctype_badge(ctype: str) -> str:
    """Return 'emoji label' for a MIME content-type, or a generic badge."""
    entry = _CTYPE_EMOJI.get(ctype)
    if entry:
        return f"{entry[0]} {entry[1]}"
    # Generic fallback: use the subtype
    maintype, _, subtype = ctype.partition("/")
    icons = {"image": "🖼", "audio": "🎵", "video": "🎬",
             "text": "📄", "application": "💾", "multipart": "📦"}
    icon = icons.get(maintype, "❓")
    return f"{icon} {subtype}"


def _build_type_badges(content_types: list) -> str:
    """
    Build a space-separated string of badges for all content types,
    skipping multipart/* wrappers (they add noise, not info).
    """
    badges = []
    for ct in content_types:
        if ct.startswith("multipart/"):
            continue
        badges.append(_ctype_badge(ct))
    return "  ".join(badges) if badges else ""


def print_new_email(msg: EmailMessage, index: int = 0) -> None:
    """Display a new-email notification in the terminal."""
    ts = msg.date.strftime("%Y-%m-%d %H:%M:%S")

    table = Table(
        show_header=False,
        box=box.ROUNDED,
        border_style="yellow",
        padding=(0, 1),
        expand=False,
    )
    table.add_column("Label", style="bright_yellow", no_wrap=True, min_width=10)
    table.add_column("Value", style="white")

    table.add_row("Account",  f"[bold cyan]{msg.account}[/bold cyan]")
    table.add_row("From",     f"[green]{msg.from_addr}[/green]")
    table.add_row("To",       msg.to_addr)
    table.add_row("Subject",  f"[bold white]{msg.subject}[/bold white]")
    table.add_row("Received", ts)

    # Content-type badges row (skip if empty / only multiparts)
    type_badges = _build_type_badges(getattr(msg, "content_types", []))
    if type_badges:
        table.add_row("Type", f"[cyan]{type_badges}[/cyan]")

    # Attachments row
    attachments = getattr(msg, "attachments", [])
    if attachments:
        att_str = "  ".join(f"📎 [yellow]{a}[/yellow]" for a in attachments)
        table.add_row("Attach", att_str)

    if msg.body_preview:
        preview = msg.body_preview.strip().replace("\n", " ")[:120]
        if len(msg.body_preview.strip()) > 120:
            preview += "…"
        table.add_row("Preview", f"[dim]{preview}[/dim]")

    label = Text(f" ✉  #{index} NEW EMAIL ", style="bold black on yellow")
    console.print(label)
    console.print(table)
    console.print()


def print_email_table(rows: List[Dict], title: str = "Email History") -> None:
    """Render a list of email_history dicts as a Rich table."""
    if not rows:
        console.print("[yellow]No emails found.[/yellow]")
        return

    table = Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        border_style="cyan",
        show_lines=False,
        expand=True,
    )
    table.add_column("#",        style="dim",          width=4,  no_wrap=True)
    table.add_column("Account",  style="cyan",         min_width=20, no_wrap=True)
    table.add_column("From",     style="green",        min_width=24)
    table.add_column("Subject",  style="bold white",   min_width=30)
    table.add_column("Received", style="yellow",       width=19, no_wrap=True)

    for i, row in enumerate(rows, 1):
        received = row.get("received_at")
        if isinstance(received, datetime):
            ts = received.strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts = str(received or "—")

        table.add_row(
            str(i),
            str(row.get("account", "")),
            str(row.get("from_addr", "")),
            str(row.get("subject", "(no subject)")),
            ts,
        )

    console.print(table)


def print_accounts_table(accounts) -> None:
    table = Table(
        title="Configured Accounts",
        box=box.SIMPLE_HEAVY,
        border_style="cyan",
    )
    table.add_column("#",       style="dim",    width=4)
    table.add_column("Email",   style="cyan",   min_width=30)
    table.add_column("Server",  style="white",  min_width=22)
    table.add_column("Port",    style="yellow", width=6)
    table.add_column("Enabled", style="green",  width=8)

    for i, acc in enumerate(accounts, 1):
        table.add_row(
            str(i),
            acc.email,
            acc.imap_server,
            str(acc.imap_port),
            "✓" if acc.enabled else "✗",
        )

    console.print(table)
