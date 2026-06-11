#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/config.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: Configuration management: TOML file + database fallback.
# License: MIT

"""Configuration management: TOML file + database fallback."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import toml

from .db import DatabaseManager
from .models import EmailAccount

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "accounts": [],
    "database": {
        "host": "localhost",
        "port": 5432,
        "database": "gmail_monitor",
        "user": "postgres",
        "password": "",
    },
    "growl": {
        "host": "localhost",
        "port": 23053,
        "password": "",
    },
    "monitoring": {
        "check_interval": 60,
        "max_emails_per_check": 10,
        "reconnect_delay": 30,
        "max_reconnect_attempts": 0,   # 0 = unlimited
        "mark_as_read": True,
    },
    "logging": {
        "level": "INFO",
        "file": "gmail_monitor.log",
    },
}

EXAMPLE_CONFIG = """\
# Gmail CLI Monitor — config.toml
# ------------------------------------------------
# IMPORTANT: For Gmail use an App Password, not your real password.
# https://myaccount.google.com/apppasswords

# ── Accounts ────────────────────────────────────
[[accounts]]
email       = "you@gmail.com"
password    = "abcd efgh ijkl mnop"   # 16-char Google App Password
imap_server = "imap.gmail.com"
imap_port   = 993
enabled     = true

# [[accounts]]
# email       = "work@yahoo.com"
# password    = "your-app-password"
# imap_server = "imap.mail.yahoo.com"
# imap_port   = 993
# enabled     = true

# ── Database ────────────────────────────────────
[database]
host     = "localhost"
port     = 5432
database = "gmail_monitor"
user     = "postgres"
password = ""

# ── Growl Notifications ─────────────────────────
[growl]
host     = "localhost"
port     = 23053
password = ""

# ── Monitor behaviour ───────────────────────────
[monitoring]
check_interval         = 60    # seconds between IMAP checks
max_emails_per_check   = 10    # max unseen emails processed per cycle
reconnect_delay        = 30    # base seconds before reconnect (exponential backoff)
max_reconnect_attempts = 0     # 0 = unlimited retries (recommended)
mark_as_read           = true  # mark messages \\Seen on server after fetching

# ── Logging ─────────────────────────────────────
[logging]
level = "INFO"          # DEBUG | INFO | WARNING | ERROR
file  = "gmail_monitor.log"
"""


def _find_config_file() -> Path:
    """
    Search for config.toml in order:
      1. CWD (project root when running python -m gmail_monitor)
      2. The package directory itself  (gmail_monitor/config.toml)
      3. The directory containing this source file
    Returns the first match, or CWD/config.toml as the default write target.
    """
    candidates = [
        Path("config.toml"),                          # CWD
        Path(__file__).parent / "config.toml",        # package dir
        Path(__file__).parent.parent / "config.toml", # one level up from package
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # default write location = CWD


class ConfigManager:
    """Load, validate, merge, and persist configuration."""

    CONFIG_FILE: Path = _find_config_file()

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._cfg: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """
        Priority order:
        1. config.toml in the working directory
        2. accounts + settings stored in the database
        3. Built-in defaults
        """
        if self.CONFIG_FILE.exists():
            logger.info("Loading config from %s", self.CONFIG_FILE)
            raw = toml.load(self.CONFIG_FILE)
            self._cfg = self._merge_defaults(raw)
        else:
            logger.info("config.toml not found — loading from database")
            self._cfg = self._load_from_db()

        self._validate()
        return self._cfg

    def save_to_file(self) -> None:
        """Write current config back to config.toml."""
        with self.CONFIG_FILE.open("w") as fh:
            toml.dump(self._cfg, fh)
        logger.info("Config written to %s", self.CONFIG_FILE)

    def sync_to_db(self) -> None:
        """Push accounts and settings into the database."""
        for acc_dict in self._cfg.get("accounts", []):
            # Only known fields — avoids surprise kwargs
            self.db.save_account(
                EmailAccount(
                    email=acc_dict["email"],
                    password=acc_dict["password"],
                    imap_server=acc_dict.get("imap_server", "imap.gmail.com"),
                    imap_port=int(acc_dict.get("imap_port", 993)),
                    enabled=acc_dict.get("enabled", True),
                )
            )
        mon = self._cfg.get("monitoring", {})
        self.db.set_config("check_interval", str(mon.get("check_interval", 60)))
        self.db.set_config("max_emails_per_check", str(mon.get("max_emails_per_check", 10)))
        self.db.set_config("reconnect_delay", str(mon.get("reconnect_delay", 30)))
        self.db.set_config("max_reconnect_attempts", str(mon.get("max_reconnect_attempts", 5)))
        growl = self._cfg.get("growl", {})
        self.db.set_config("growl_host", growl.get("host", "localhost"))
        self.db.set_config("growl_port", str(growl.get("port", 23053)))
        self.db.set_config("growl_password", growl.get("password", ""))
        logger.debug("Config synced to database")

    def get_accounts(self) -> List[EmailAccount]:
        accs = []
        for acc_dict in self._cfg.get("accounts", []):
            if not acc_dict.get("enabled", True):
                continue
            accs.append(
                EmailAccount(
                    email=acc_dict["email"],
                    password=acc_dict["password"],
                    imap_server=acc_dict.get("imap_server", "imap.gmail.com"),
                    imap_port=int(acc_dict.get("imap_port", 993)),
                    enabled=True,
                )
            )
        return accs

    def write_example(self, path: Path = Path("config.toml.example")) -> None:
        path.write_text(EXAMPLE_CONFIG)
        logger.info("Example config written to %s", path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _merge_defaults(self, raw: Dict) -> Dict:
        """Deep-merge raw config over the built-in defaults."""
        import copy
        result = copy.deepcopy(DEFAULT_CONFIG)
        for section, values in raw.items():
            if isinstance(values, dict) and section in result and isinstance(result[section], dict):
                result[section].update(values)
            else:
                result[section] = values
        return result

    def _load_from_db(self) -> Dict:
        """Reconstruct config dict from database rows."""
        import copy
        cfg = copy.deepcopy(DEFAULT_CONFIG)

        accounts = self.db.load_accounts()
        cfg["accounts"] = [
            {
                "email": a.email,
                "password": a.password,
                "imap_server": a.imap_server,
                "imap_port": a.imap_port,
                "enabled": a.enabled,
            }
            for a in accounts
        ]

        def _get(key, default):
            v = self.db.get_config(key)
            return default if v is None else v

        cfg["monitoring"]["check_interval"] = int(_get("check_interval", 60))
        cfg["monitoring"]["max_emails_per_check"] = int(_get("max_emails_per_check", 10))
        cfg["monitoring"]["reconnect_delay"] = int(_get("reconnect_delay", 30))
        cfg["monitoring"]["max_reconnect_attempts"] = int(_get("max_reconnect_attempts", 5))
        cfg["growl"]["host"] = _get("growl_host", "localhost")
        cfg["growl"]["port"] = int(_get("growl_port", 23053))
        cfg["growl"]["password"] = _get("growl_password", "")
        return cfg

    def _validate(self) -> None:
        """Raise ValueError for obviously bad config."""
        for acc in self._cfg.get("accounts", []):
            if not acc.get("email"):
                raise ValueError("Account entry missing 'email'")
            if not acc.get("password"):
                raise ValueError(f"Account {acc.get('email')} has no password set")


def bootstrap_db_config() -> Dict[str, Any]:
    """
    Return DB connection params using config.toml if present,
    otherwise fall back to environment variables, then defaults.
    Intentionally does NOT require the DB to be up yet.
    """
    if ConfigManager.CONFIG_FILE.exists():
        raw = toml.load(ConfigManager.CONFIG_FILE)
        return raw.get("database", DEFAULT_CONFIG["database"])

    return {
        "host":     os.getenv("DB_HOST", "localhost"),
        "port":     int(os.getenv("DB_PORT", "5432")),
        "database": os.getenv("DB_NAME", "gmail_monitor"),
        "user":     os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
    }
