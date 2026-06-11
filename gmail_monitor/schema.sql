-- Gmail Monitor — manual schema reference
-- The application auto-creates all of this on first run.
-- Only use this file if you prefer to set up the schema manually.

-- Run as superuser to create the database:
-- CREATE DATABASE gmail_monitor;
-- GRANT ALL PRIVILEGES ON DATABASE gmail_monitor TO your_user;

-- Then connect and run:
-- \c gmail_monitor

CREATE TABLE IF NOT EXISTS accounts (
    id          SERIAL       PRIMARY KEY,
    email       VARCHAR(255) UNIQUE NOT NULL,
    password    TEXT         NOT NULL,
    imap_server VARCHAR(255) NOT NULL DEFAULT 'imap.gmail.com',
    imap_port   INTEGER      NOT NULL DEFAULT 993,
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_history (
    id           SERIAL       PRIMARY KEY,
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
);

CREATE TABLE IF NOT EXISTS app_config (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT         NOT NULL,
    updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_eh_account    ON email_history(account);
CREATE INDEX IF NOT EXISTS idx_eh_received   ON email_history(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_eh_message_id ON email_history(message_id);

-- Seed default settings
INSERT INTO app_config (key, value) VALUES
    ('check_interval',         '60'),
    ('max_emails_per_check',   '10'),
    ('reconnect_delay',        '30'),
    ('max_reconnect_attempts', '5'),
    ('growl_host',             'localhost'),
    ('growl_port',             '23053'),
    ('growl_password',         '')
ON CONFLICT (key) DO NOTHING;
