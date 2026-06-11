# Gmail CLI Monitor

Multi-account IMAP email monitor with PostgreSQL-backed history and Growl desktop notifications.

## Features

- Monitor multiple IMAP accounts concurrently (Gmail, Yahoo, any IMAP server)
- Persistent email history stored in PostgreSQL
- Growl/GNTP desktop notifications (gracefully degraded — works without Growl too)
- Rich terminal UI with colour-coded output
- Full CLI: `monitor`, `list`, `accounts`, `add`, `remove`, `purge`, `init-config`
- Automatic IMAP reconnection with configurable retry limits
- Config via `config.toml` **or** database — whichever is present

---

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Generate a starter config
gmail-monitor init-config

# 3. Edit config.toml — add your email + App Password
#    (Gmail requires an App Password, not your real password)
#    https://myaccount.google.com/apppasswords

# 4. Start monitoring
gmail-monitor monitor
```

---

## Commands

| Command | Description |
|---------|-------------|
| `monitor` | Start monitoring all enabled accounts |
| `list` | Show recent email history (`-n 100` for more, `-a email` to filter) |
| `accounts` | List accounts stored in the database |
| `add` | Add a new account (interactive or `--email`/`--password` flags) |
| `remove <email>` | Remove an account and its history |
| `init-config` | Write an example `config.toml` |
| `purge` | Delete old email history (`--days 30`) |
| `version` | Print version |

---

## config.toml reference

```toml
[[accounts]]
email       = "you@gmail.com"
password    = "abcd efgh ijkl mnop"   # Google App Password
imap_server = "imap.gmail.com"
imap_port   = 993
enabled     = true

[database]
host     = "localhost"
port     = 5432
database = "gmail_monitor"
user     = "postgres"
password = ""

[growl]
host     = "localhost"
port     = 23053
password = ""

[monitoring]
check_interval         = 60   # seconds between IMAP polls
max_emails_per_check   = 10
reconnect_delay        = 30   # seconds before reconnect attempt
max_reconnect_attempts = 5    # 0 = unlimited

[logging]
level = "INFO"
file  = "gmail_monitor.log"
```

---

## PostgreSQL setup

The application creates the database automatically if the PostgreSQL user has `CREATEDB` privileges. Otherwise create it manually:

```sql
CREATE DATABASE gmail_monitor;
GRANT ALL PRIVILEGES ON DATABASE gmail_monitor TO your_user;
```

---

## Gmail App Password

1. Enable 2-factor authentication on your Google account
2. Go to <https://myaccount.google.com/apppasswords>
3. Create an app password for "Mail"
4. Use the 16-character password in `config.toml`

---

## Requirements

- Python 3.11+
- PostgreSQL 12+
- Growl (optional — Windows: Growl for Windows, macOS: Growl/Notificaton Center bridge)

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)