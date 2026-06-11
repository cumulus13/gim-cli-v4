#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/models.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: Data models for Gmail Monitor.
# License: MIT

"""Data models for Gmail Monitor."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class EmailAccount:
    """Email account configuration."""
    email: str
    password: str
    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    enabled: bool = True

    def __repr__(self) -> str:
        return f"EmailAccount(email={self.email!r}, server={self.imap_server}:{self.imap_port})"


@dataclass
class EmailMessage:
    """Represents a single email message."""
    account: str
    from_addr: str
    to_addr: str
    subject: str
    date: datetime
    message_id: str
    body_preview: str = ""
    # MIME content types present in this message, e.g. ["text/plain", "text/html"]
    content_types: list = field(default_factory=list)
    # Attachment filenames, e.g. ["report.pdf", "photo.jpg"]
    attachments: list = field(default_factory=list)

    def __repr__(self) -> str:
        return f"EmailMessage(from={self.from_addr!r}, subject={self.subject!r})"
