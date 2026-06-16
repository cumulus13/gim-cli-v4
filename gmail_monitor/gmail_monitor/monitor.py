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
from typing import List, Optional, Set

from .db import DatabaseManager
from .models import EmailAccount, EmailMessage
from .notifier import GrowlNotifier
from .ntfy_sender import NtfySender
from .config import ConfigManager

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
    seen_types: Set[str] = set()
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


class _FakeDB:  # noqa: D101
    pass

class EmailMonitor:
    """
    Monitors one IMAP account for new messages.

    Reconnection design
    -------------------
    _consecutive_failures counts failures since the last genuinely successful
    check cycle (not just a successful login).  This means:

      login OK → select() dies immediately
        → _consecutive_failures increments (NOT reset by login alone)
        → backoff grows: 30s, 60s, 120s … up to 300s cap

    Only a complete successful check cycle (select + search + return without
    error) resets the counter.  This prevents the connect→die→30s→connect→die
    loop that occurred when the counter was reset on login success.

    After login(), a NOOP command is issued to flush any pending server
    responses (BYE, capability updates etc.) that would cause the first
    real command to fail with "connection already closed".

    Missing-email fix
    -----------------
    We fetch the Message-ID header for ALL unseen messages first (cheap),
    filter out any already in the DB, then fetch full RFC822 only for new
    ones.  This guarantees no email is missed regardless of max_emails,
    while avoiding downloading bodies of already-known messages.
    """

    # Hard cap on backoff delay regardless of failure count
    MAX_BACKOFF_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        account: EmailAccount,
        db: DatabaseManager,
        notifier: GrowlNotifier,
        check_interval: int = 60,
        max_emails: int = 50,
        reconnect_delay: int = 30,
        max_reconnect_attempts: int = 0,
        mark_as_read: bool = True,
        socket_timeout: int = 60,
        on_new_email=None,
    ):
        self.account = account
        self.db = db
        self.notifier = notifier
        self.ntfy = NtfySender()  # type: ignore

        self.check_interval = check_interval
        self.max_emails = max_emails
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.mark_as_read = mark_as_read
        # CRITICAL: imaplib.IMAP4_SSL defaults to timeout=None, meaning the
        # underlying socket.recv() can block FOREVER if the connection dies
        # without a clean TCP close (very common with NAT/firewall idle
        # timeouts on long-lived IMAP IDLE-less connections).  When that
        # happens, asyncio.to_thread() never returns, the coroutine for
        # that account is permanently stuck, and that one account silently
        # stops checking mail while other accounts keep working fine.
        # Setting an explicit timeout guarantees select()/fetch() raise
        # socket.timeout (a subclass of OSError) instead of hanging forever,
        # which _is_dead_socket_error() catches and triggers reconnection.
        self.socket_timeout = socket_timeout
        self.on_new_email = on_new_email  # callback(EmailMessage) -> None

        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._running = False
        # Counts failures since the last fully successful check cycle.
        # Reset ONLY after _check_new_emails completes without error.
        # NOT reset on login success — that was the bug causing 30s loops.
        self._consecutive_failures = 0

        self.config = ConfigManager(_FakeDB())  # type: ignore
        self.config = self.config.load()

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
                            self.account.email, self._consecutive_failures,
                        )
                        break
                    delay = self._backoff_delay()
                    logger.info(
                        "Waiting %.0fs before next reconnect for %s (failure #%d)",
                        delay, self.account.email, self._consecutive_failures,
                    )
                    await asyncio.sleep(delay)
                    continue

            # ── Check for new mail ─────────────────────────────────────
            try:
                new_emails = await asyncio.to_thread(self._check_new_emails)

                # Only reset failure counter after a genuinely successful check
                self._consecutive_failures = 0

                for msg in new_emails:
                    if self.on_new_email:
                        self.on_new_email(msg)
                    self.notifier.notify(msg)
                    ntfy_msg = f"✉ {msg.account}, From: {msg.from_addr}, Subject: {msg.subject}"
                    self.ntfy.send(
                        title="gim-monitor-v4",
                        message=ntfy_msg,
                        priority=self.config.get("ntfy", {}).get("priority", 3),  # type: ignore
                        tags=self.config.get("ntfy", {}).get("tags", [])  # type: ignore
                    )

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

    def update_settings(
        self,
        check_interval: Optional[int] = None,
        max_emails: Optional[int] = None,
        reconnect_delay: Optional[int] = None,
        max_reconnect_attempts: Optional[int] = None,
        mark_as_read: Optional[bool] = None,
        socket_timeout: Optional[int] = None,
    ) -> None:
        """
        Apply new settings live, without restarting the monitor loop.

        Only fields that actually changed are updated (None = "no change").
        socket_timeout changes are applied to the live socket immediately
        via settimeout() if a connection is currently open; otherwise the
        new value takes effect on the next reconnect.
        """
        if check_interval is not None and check_interval != self.check_interval:
            logger.info("%s: check_interval %s -> %s", self.account.email, self.check_interval, check_interval)
            self.check_interval = check_interval

        if max_emails is not None and max_emails != self.max_emails:
            logger.info("%s: max_emails %s -> %s", self.account.email, self.max_emails, max_emails)
            self.max_emails = max_emails

        if reconnect_delay is not None and reconnect_delay != self.reconnect_delay:
            logger.info("%s: reconnect_delay %s -> %s", self.account.email, self.reconnect_delay, reconnect_delay)
            self.reconnect_delay = reconnect_delay
        if max_reconnect_attempts is not None and max_reconnect_attempts != self.max_reconnect_attempts:
            logger.info("%s: max_reconnect_attempts %s -> %s", self.account.email, self.max_reconnect_attempts, max_reconnect_attempts)
            self.max_reconnect_attempts = max_reconnect_attempts

        if mark_as_read is not None and mark_as_read != self.mark_as_read:
            logger.info("%s: mark_as_read %s -> %s", self.account.email, self.mark_as_read, mark_as_read)
            self.mark_as_read = mark_as_read

        if socket_timeout is not None and socket_timeout != self.socket_timeout:
            logger.info("%s: socket_timeout %s -> %s", self.account.email, self.socket_timeout, socket_timeout)
            self.socket_timeout = socket_timeout
            # Apply to the live socket immediately if connected.
            if self._imap is not None:
                try:
                    self._imap.sock.settimeout(socket_timeout)
                except Exception as exc:
                    logger.debug("%s: could not apply socket_timeout to live socket: %s", self.account.email, exc)

    def credentials_changed(self, account: EmailAccount) -> bool:
        """Return True if email/password/server/port differ from current account."""
        return (
            account.email != self.account.email
            or account.password != self.account.password
            or account.imap_server != self.account.imap_server
            or account.imap_port != self.account.imap_port
        )

    def apply_account(self, account: EmailAccount) -> None:
        """
        Replace the account object.  If credentials/server changed, force a
        reconnect on the next loop iteration by tearing down the current
        connection (it will be re-established with the new credentials).
        """
        if self.credentials_changed(account):
            logger.info("%s: credentials/server changed — forcing reconnect", self.account.email)
            self._disconnect()
        self.account = account

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
        """
        Connect, login, then send NOOP to flush any pending server responses.

        Gmail sometimes sends unsolicited data (capability updates, BYE on
        rate-limit) immediately after the login response.  If we don't consume
        it before issuing SELECT, imaplib reads that stale data and raises
        "connection already closed".  NOOP forces that flush and verifies the
        connection is truly ready.

        We do NOT reset _consecutive_failures here.  It is reset only after a
        successful _check_new_emails cycle.  This is the key fix for the
        connect→die→30s→connect→die loop: the backoff now accumulates correctly
        across connect-then-immediately-fail cycles.
        """
        try:
            self._imap = imaplib.IMAP4_SSL(
                self.account.imap_server,
                self.account.imap_port,
                timeout=self.socket_timeout,
            )
            self._imap.login(self.account.email, self.account.password)

            # Flush pending server responses and confirm connection is usable
            status, _ = self._imap.noop()
            if status != "OK":
                raise imaplib.IMAP4.error(f"NOOP returned {status}")

            logger.info("Connected to %s", self.account.email)
            return True

        except imaplib.IMAP4.error as exc:
            logger.error("IMAP login/noop failed for %s: %s", self.account.email, exc)
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
        Find and fetch all unseen messages not already in the database.

        Strategy
        --------
        1. SELECT INBOX
        2. SEARCH UNSEEN  → list of IMAP sequence numbers
        3. For each ID, fetch BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)] — very
           cheap, header-only, no body download, does not mark as read.
        4. Check each Message-ID against the database (is_known).
        5. Only for genuinely new messages: fetch full RFC822 and process.

        This guarantees every unseen message is checked regardless of
        max_emails.  max_emails now acts as a per-cycle processing cap to
        avoid a burst of thousands of messages on first run — but we iterate
        from oldest to newest, so nothing is skipped: the next check cycle
        processes the remainder.

        Any dead-socket error is re-raised immediately so run() can reconnect.
        Per-message parse errors are logged and skipped without aborting.
        """
        assert self._imap is not None
        new_emails: List[EmailMessage] = []

        status, data = self._imap.select("INBOX")
        if status != "OK":
            raise imaplib.IMAP4.error(f"SELECT INBOX returned {status}")

        status, data = self._imap.search(None, "UNSEEN")
        if status != "OK":
            return new_emails

        all_ids = data[0].split()
        if not all_ids:
            return new_emails

        # Process oldest-first so if max_emails caps us, later cycles get the rest
        process_ids = all_ids[:self.max_emails]

        for eid in process_ids:
            # ── Step 1: cheap header-only fetch to get Message-ID ────────
            try:
                status, hdr_data = self._imap.fetch(
                    eid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                )
            except Exception as exc:
                if _is_dead_socket_error(exc):
                    raise
                logger.warning("Header fetch failed for msg %s / %s: %s",
                               eid, self.account.email, exc)
                continue

            if status != "OK" or not hdr_data or hdr_data[0] is None:
                continue

            # imaplib returns fetch data as a list where each element is either
            # a tuple (flags_bytes, literal_bytes) or a plain bytes b')'.
            # We must check isinstance before indexing [1].
            first = hdr_data[0]
            if not isinstance(first, tuple) or len(first) < 2:
                continue
            raw_hdr: bytes = first[1]
            hdr_msg = _email_pkg.message_from_bytes(raw_hdr)
            msg_id = hdr_msg.get("Message-ID", "").strip()

            # Fall back: we'll compute sha1 from the full body later
            if not msg_id:
                msg_id = None

            # ── Step 2: skip if already known ────────────────────────────
            if msg_id and self.db.is_known(msg_id):
                # Already seen — mark as read on server if needed and skip
                if self.mark_as_read:
                    try:
                        self._imap.store(eid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                continue

            # ── Step 3: full RFC822 fetch ─────────────────────────────────
            try:
                status, raw_data = self._imap.fetch(eid, "(RFC822)")
            except Exception as exc:
                if _is_dead_socket_error(exc):
                    raise
                logger.warning("RFC822 fetch failed for msg %s / %s: %s",
                               eid, self.account.email, exc)
                continue

            if status != "OK" or not raw_data or raw_data[0] is None:
                continue

            first_rfc = raw_data[0]
            if not isinstance(first_rfc, tuple) or len(first_rfc) < 2:
                continue
            raw_bytes: bytes = first_rfc[1]

            if self.mark_as_read:
                try:
                    self._imap.store(eid, "+FLAGS", "\\Seen")
                except Exception as flag_exc:
                    logger.debug("Could not mark %s as read: %s", eid, flag_exc)

            try:
                msg = self._parse_message(raw_bytes)
            except Exception as exc:
                # Re-raise dead-socket errors so run() handles reconnection.
                # Only swallow genuinely per-message parse failures.
                if _is_dead_socket_error(exc):
                    raise
                logger.warning("Parse failed for msg %s / %s: %s",
                               eid, self.account.email, exc)
                continue

            if msg and self.db.save_email(msg):
                new_emails.append(msg)

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
