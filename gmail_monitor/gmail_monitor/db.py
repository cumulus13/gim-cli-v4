#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/db.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description: PostgreSQL database manager for Gmail Monitor.
# License: MIT

"""PostgreSQL database manager for Gmail Monitor."""

import logging
from typing import Dict, List, Optional

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import RealDictCursor

try:
    from .models import EmailAccount, EmailMessage
except:
    from models import EmailAccount, EmailMessage  # type: ignore

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manage all PostgreSQL database operations."""

    def __init__(self, db_config: Dict):
        self.config = db_config
        self.conn: Optional[psycopg2.extensions.connection] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to PostgreSQL, creating the database if it doesn't exist."""
        db_name = self.config.get("database", "gmail_monitor")
        self._ensure_database_exists(db_name)

        self.conn = psycopg2.connect(
            host=self.config.get("host", "localhost"),
            port=self.config.get("port", 5432),
            database=db_name,
            user=self.config.get("user", "postgres"),
            password=self.config.get("password", ""),
        )
        logger.info("Connected to database '%s'", db_name)
        self._init_tables()

    def _ensure_database_exists(self, db_name: str) -> None:
        """Create the target database if it doesn't exist yet."""
        try:
            tmp = psycopg2.connect(
                host=self.config.get("host", "localhost"),
                port=self.config.get("port", 5432),
                database="postgres",
                user=self.config.get("user", "postgres"),
                password=self.config.get("password", ""),
            )
            tmp.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with tmp.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
                )
                if not cur.fetchone():
                    cur.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
                    )
                    logger.info("Created database '%s'", db_name)
                else:
                    logger.debug("Database '%s' already exists", db_name)
            tmp.close()
        except Exception as exc:
            logger.error("Could not ensure database exists: %s", exc)
            raise

    def _init_tables(self) -> None:
        """Create all tables and indexes if they don't already exist."""
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id          SERIAL PRIMARY KEY,
                    email       VARCHAR(255) UNIQUE NOT NULL,
                    password    TEXT         NOT NULL,
                    imap_server VARCHAR(255) NOT NULL DEFAULT 'imap.gmail.com',
                    imap_port   INTEGER      NOT NULL DEFAULT 993,
                    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
                    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS email_history (
                    id           SERIAL      PRIMARY KEY,
                    account      VARCHAR(255) NOT NULL,
                    message_id   VARCHAR(500) UNIQUE NOT NULL,
                    from_addr    TEXT         NOT NULL,
                    to_addr      TEXT         NOT NULL,
                    subject      TEXT,
                    body_preview TEXT,
                    received_at  TIMESTAMP    NOT NULL,
                    notified_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_account
                        FOREIGN KEY (account) REFERENCES accounts(email)
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    key        VARCHAR(100) PRIMARY KEY,
                    value      TEXT         NOT NULL,
                    updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Indexes
            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_eh_account    ON email_history(account)",
                "CREATE INDEX IF NOT EXISTS idx_eh_received   ON email_history(received_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_eh_message_id ON email_history(message_id)",
            ):
                cur.execute(idx_sql)

            self.conn.commit()
        logger.debug("Database tables initialised")

    def close(self) -> None:
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.debug("Database connection closed")

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def save_account(self, account: EmailAccount) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (email, password, imap_server, imap_port, enabled)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    password    = EXCLUDED.password,
                    imap_server = EXCLUDED.imap_server,
                    imap_port   = EXCLUDED.imap_port,
                    enabled     = EXCLUDED.enabled
                """,
                (
                    account.email,
                    account.password,
                    account.imap_server,
                    account.imap_port,
                    account.enabled,
                ),
            )
            self.conn.commit()

    def load_accounts(self) -> List[EmailAccount]:
        """Return all enabled accounts. Only maps known EmailAccount fields."""
        assert self.conn is not None
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT email, password, imap_server, imap_port, enabled "
                "FROM accounts WHERE enabled = TRUE ORDER BY email"
            )
            rows = cur.fetchall()
        return [
            EmailAccount(
                email=row["email"],
                password=row["password"],
                imap_server=row["imap_server"],
                imap_port=row["imap_port"],
                enabled=row["enabled"],
            )
            for row in rows
        ]

    def delete_account(self, email: str) -> bool:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE email = %s", (email,))
            self.conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Email history
    # ------------------------------------------------------------------

    def is_known(self, message_id: str) -> bool:
        """Return True if this message_id is already in email_history."""
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM email_history WHERE message_id = %s LIMIT 1",
                (message_id,),
            )
            return cur.fetchone() is not None

    def save_email(self, msg: EmailMessage) -> bool:
        """
        Persist a message.  Returns True if it was new, False if duplicate.
        Never raises — integrity errors are absorbed gracefully.
        """
        assert self.conn is not None
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_history
                        (account, message_id, from_addr, to_addr,
                         subject, body_preview, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        msg.account,
                        msg.message_id,
                        msg.from_addr,
                        msg.to_addr,
                        msg.subject,
                        msg.body_preview,
                        msg.date,
                    ),
                )
                self.conn.commit()
                return True
        except psycopg2.IntegrityError:
            self.conn.rollback()
            return False  # already seen
        except Exception as exc:
            self.conn.rollback()
            logger.error("Failed to save email %s: %s", msg.message_id, exc)
            return False

    def get_history(
        self,
        account: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        assert self.conn is not None
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            if account:
                cur.execute(
                    "SELECT * FROM email_history WHERE account = %s "
                    "ORDER BY received_at DESC LIMIT %s",
                    (account, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM email_history ORDER BY received_at DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    def purge_old_emails(self, days: int = 30) -> int:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            # psycopg2 cannot parameterise inside a string literal like
            # INTERVAL '%s days', so use format() for the integer only.
            # days is always an int (typed), so this is safe.
            cur.execute(
                f"DELETE FROM email_history "
                f"WHERE received_at < NOW() - INTERVAL '{int(days)} days'"
            )
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # App config key/value store
    # ------------------------------------------------------------------

    def get_config(self, key: str) -> Optional[str]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_config (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            self.conn.commit()
