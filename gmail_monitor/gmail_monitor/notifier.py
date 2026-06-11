#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/notifier.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: Growl/GNTP desktop notification support.
# License: MIT

"""Growl/GNTP desktop notification support."""

import logging
from pathlib import Path
from typing import Dict

import gntp.notifier

from .models import EmailMessage

logger = logging.getLogger(__name__)

_APP_NAME = "Gmail Monitor"
_NOTE_TYPE = "New Email"


class GrowlNotifier:
    """
    Send desktop notifications via Growl / GNTP.

    Growl being unavailable is non-fatal: the monitor keeps running,
    notifications just won't fire.
    """

    def __init__(self, config: Dict):
        self._enabled = False
        self._growl: gntp.notifier.GrowlNotifier | None = None
        self._icon: str | None = self._load_icon()
        self._setup(config)

    # ------------------------------------------------------------------

    def _load_icon(self) -> str | None:
        for name in ("gim.png", "icon.png", "mail.png"):
            p = Path(name)
            if p.exists():
                return (Path(__file__).parent / name).as_uri()
                # try:
                #     return p.read_bytes()
                # except OSError:
                #     pass
        return None

    def _setup(self, config: Dict) -> None:
        try:
            self._growl = gntp.notifier.GrowlNotifier(
                applicationName=_APP_NAME,
                notifications=[_NOTE_TYPE],
                defaultNotifications=[_NOTE_TYPE],
                hostname=config.get("host", "localhost"),
                port=int(config.get("port", 23053)),
                password=config.get("password") or None,
            )
            self._growl.register()
            self._enabled = True
            logger.info("Growl notifier registered on %s:%s",
                        config.get("host", "localhost"), config.get("port", 23053))
        except Exception as exc:
            logger.warning("Growl not available (%s) — running without notifications", exc)
            self._enabled = False

    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def notify(self, msg: EmailMessage) -> None:
        """
        Fire a Growl notification.  Errors are logged but never raised,
        so a Growl glitch cannot crash the monitor loop.
        """
        if not self._enabled or self._growl is None:
            return
        try:
            logger.debug(f"self._icon: {self._icon}")
            self._growl.notify(
                noteType=_NOTE_TYPE,
                title=f"✉  {msg.account}",
                description=(
                    f"From:    {msg.from_addr}\n"
                    f"Subject: {msg.subject}"
                ),
                icon=self._icon,
                sticky=False,
                priority=1
            )
        except Exception as exc:
            logger.debug("Growl notification failed: %s", exc)
