#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/monitor.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: IMAP email monitor with automatic reconnection.
# License: MIT

"""IMAP email monitor with automatic reconnection."""

import asyncio
import email as _email_pkg
import email.message
import email.utils
import hashlib
import imaplib
import logging
import socket
from datetime import datetime
from email.header import decode_header
from typing import List, Optional

from .db import DatabaseManager
from .models import EmailAccount, EmailMessage
from .notifier import GrowlNotifier

logger = logging.getLogger(__name__)

# Tuple of exception types that mean "the socket is dead, reconnect"
_DEAD_SOCKET_ERRORS = (
    imaplib.IMAP4.abort,   # server closed the connection
    imaplib.IMAP4.error,   # general IMAP protocol error
    OSError,               # socket-level errors
    socket.error,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)


def _decode_header_value(raw: Optional[str]) -> str:
    """
    Safely decode an RFC 2047-encoded email header into a plain string.
    decode_header() returns a list of (bytes|str, charset|None) tuples.
    """
    if not raw:
        return ""
    parts = []
    for chunk, enc in decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _extract_body_preview(msg: email.message.Message, max_chars: int = 300) -> str:
    """Pull the first text/plain part and return up to *max_chars* chars."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")[:max_chars]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")[:max_chars]
    return ""


def _extract_mime_info(msg: email.message.Message):
    """
    Walk all MIME parts and return:
      content_types  - deduplicated list of content-type strings found
      attachments    - list of attachment filenames (decoded)
    """
    content_types = []
    attachments = []
    seen_types = set()

    parts = list(msg.walk()) if msg.is_multipart() else [msg]

    for part in parts:
        ctype = part.get_content_type()

        if ctype not in seen_types:
            seen_types.add(ctype)
            content_types.append(ctype)

        disposition = part.get_content_disposition() or ""
        filename_raw = part.get_filename()
        filename = _decode_header_value(filename_raw) if filename_raw else ""

        is_attachment = (
            disposition.lower() == "attachment"
            or (filename and ctype not in ("text/plain", "text/html"))
        )
        if is_attachment and filename:
            attachments.append(filename)
        elif is_attachment and not filename:
            attachments.append(f"<{ctype}>")

    return content_types, attachments


def _is_dead_socket_error(exc: Exception) -> bool:
    """Return True if the exception means the IMAP socket is dead."""
    if isinstance(exc, _DEAD_SOCKET_ERRORS):
        return True
    # imaplib wraps socket errors as IMAP4.error with text like
    # "connection already closed", "socket error", "EOF"
    msg = str(exc).lower()
    return any(k in msg for k in (
        "connection already closed",
        "socket error",
        "eof",
        "broken pipe",
        "connection reset",
        "connection aborted",
        "timed out",
        "getaddrinfo failed",
    ))


class EmailMonitor:
    """
    Monitors one IMAP account for new messages.

    Reconnection
    ------------
    - On any dead-socket error _inside_ _check_new_emails the exception is
      re-raised immediately so run() tears down and reconnects rather than
      continuing to loop over remaining message IDs on a dead socket.
    - _consecutive_failures is reset to 0 as soon as _connect_sync succeeds,
      not only after a successful check cycle.  This means a 6-hour network
      outage followed by recovery never exhausts the attempt counter.
    - Exponential backoff: delay = min(reconnect_delay * 2**failures, max_delay)
      so rapid flapping does not hammer the server, but a single long outage
      recovers quickly once the network is back.
    - max_reconnect_attempts = 0 means unlimited (default).

    Mark-as-read
    ------------
    When mark_as_read=True (default), each fetched message is marked Seen (flag)
    on the server immediately after a successful fetch.  Set mark_as_read=False
    in config if you want to leave messages unread on the server.
    """

    # Hard cap on backoff delay regardless of failure count
    MAX_BACKOFF_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        account: EmailAccount,
        db: DatabaseManager,
        notifier: GrowlNotifier,
        check_interval: int = 60,
        max_emails: int = 10,
        reconnect_delay: int = 30,
        max_reconnect_attempts: int = 0,
        mark_as_read: bool = True,
        on_new_email=None,
    ):
        self.account = account
        self.db = db
        self.notifier = notifier
        self.check_interval = check_interval
        self.max_emails = max_emails
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.mark_as_read = mark_as_read
        self.on_new_email = on_new_email  # callback(EmailMessage) -> None

        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._running = False
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main coroutine — call via asyncio.gather()."""
        self._running = True
        logger.info("Monitor started for %s", self.account.email)

        while self._running:
            # ── (Re)connect if we have no live socket ─────────────────
            if self._imap is None:
                connected = await self._connect()
                if not connected:
                    if (
                        self.max_reconnect_attempts > 0
                        and self._consecutive_failures >= self.max_reconnect_attempts
                    ):
                        logger.error(
                            "Giving up on %s after %d consecutive failures",
                            self.account.email,
                            self._consecutive_failures,
                        )
                        break
                    delay = self._backoff_delay()
                    logger.info(
                        "Waiting %.0fs before next reconnect attempt for %s (failure #%d)",
                        delay, self.account.email, self._consecutive_failures,
                    )
                    await asyncio.sleep(delay)
                    continue

            # ── Check for new mail ─────────────────────────────────────
            try:
                new_emails = await asyncio.to_thread(self._check_new_emails)

                for msg in new_emails:
                    if self.on_new_email:
                        self.on_new_email(msg)
                    self.notifier.notify(msg)

            except Exception as exc:
                if _is_dead_socket_error(exc):
                    logger.warning(
                        "Dead socket for %s: %s — tearing down and reconnecting",
                        self.account.email, exc,
                    )
                else:
                    logger.exception(
                        "Unexpected error for %s: %s — reconnecting",
                        self.account.email, exc,
                    )
                self._disconnect()
                self._consecutive_failures += 1
                delay = self._backoff_delay()
                await asyncio.sleep(delay)
                continue

            await asyncio.sleep(self.check_interval)

        self._disconnect()
        logger.info("Monitor stopped for %s", self.account.email)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _backoff_delay(self) -> float:
        """
        Exponential backoff capped at MAX_BACKOFF_SECONDS.
        failure 1 → base, 2 → base*2, 3 → base*4 … up to 300s.
        """
        delay = self.reconnect_delay * (2 ** max(0, self._consecutive_failures - 1))
        return float(min(delay, self.MAX_BACKOFF_SECONDS))

    async def _connect(self) -> bool:
        return await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> bool:
        try:
            self._imap = imaplib.IMAP4_SSL(
                self.account.imap_server, self.account.imap_port
            )
            self._imap.login(self.account.email, self.account.password)
            # Reset failure counter immediately on successful login —
            # not after the first successful check — so a long outage
            # never permanently exhausts the attempt counter.
            self._consecutive_failures = 0
            logger.info("Connected to %s", self.account.email)
            return True
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP login failed for %s: %s", self.account.email, exc)
            self._consecutive_failures += 1
            self._imap = None
            return False
        except Exception as exc:
            logger.error("Connection error for %s: %s", self.account.email, exc)
            self._consecutive_failures += 1
            self._imap = None
            return False

    def _disconnect(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            finally:
                self._imap = None

    def _check_new_emails(self) -> List[EmailMessage]:
        """
        Fetch unseen messages and optionally mark them Seen (IMAP flag).

        Any dead-socket error is re-raised immediately rather than being
        swallowed per-message.  This lets run() detect the dead connection
        after the very first failure instead of logging dozens of
        "connection already closed" warnings for every remaining message ID.
        """
        assert self._imap is not None
        new_emails: List[EmailMessage] = []

        self._imap.select("INBOX")
        status, data = self._imap.search(None, "UNSEEN")
        if status != "OK":
            return new_emails

        email_ids = data[0].split()
        if not email_ids:
            return new_emails

        # Process only the newest N unseen messages
        for eid in email_ids[-self.max_emails:]:
            try:
                status, raw_data = self._imap.fetch(eid, "(RFC822)")
                if status != "OK" or not raw_data or raw_data[0] is None:
                    continue

                raw_bytes: bytes = raw_data[0][1]  # type: ignore[index]

                # Mark as read on the server right after a successful fetch
                if self.mark_as_read:
                    try:
                        self._imap.store(eid, "+FLAGS", "\\Seen")
                    except Exception as flag_exc:
                        # Non-fatal: failing to set the flag does not lose the message
                        logger.debug("Could not mark %s as read: %s", eid, flag_exc)

                msg = self._parse_message(raw_bytes)
                if msg and self.db.save_email(msg):
                    new_emails.append(msg)

            except Exception as exc:
                # Re-raise dead-socket errors so run() handles reconnection.
                # Only swallow genuinely per-message parse failures.
                if _is_dead_socket_error(exc):
                    raise
                logger.warning(
                    "Error processing message %s for %s: %s",
                    eid, self.account.email, exc,
                )
                continue

        return new_emails

    def _parse_message(self, raw: bytes) -> Optional[EmailMessage]:
        try:
            parsed = _email_pkg.message_from_bytes(raw)
        except Exception as exc:
            logger.debug("Failed to parse raw email: %s", exc)
            return None

        subject   = _decode_header_value(parsed.get("Subject", ""))
        from_addr = _decode_header_value(parsed.get("From", ""))
        to_addr   = _decode_header_value(parsed.get("To", ""))
        date_str  = parsed.get("Date", "")
        msg_id    = parsed.get("Message-ID", "").strip()

        if not msg_id:
            msg_id = "sha1:" + hashlib.sha1(raw).hexdigest()

        try:
            date = email.utils.parsedate_to_datetime(date_str)
        except Exception:
            date = datetime.now().astimezone()

        body_preview  = _extract_body_preview(parsed)
        content_types, attachments = _extract_mime_info(parsed)

        return EmailMessage(
            account=self.account.email,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            date=date,
            message_id=msg_id,
            body_preview=body_preview,
            content_types=content_types,
            attachments=attachments,
        )
