#!/usr/bin/env python3

# File: gmail_monitor/gmail_monitor/ntfy_sender.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-06-12
# Description:
# License: MIT

"""
Send messages to an ntfy server.

Example received payload:

{
    "id":"1781145384148_MVvNCgrkIYmU",
    "topic":"androcall",
    "message":"{\"topic\":\"androcall\",\"title\":\"📱 idm.internet.download.manager.plus\",\"message\":\"test message\",\"priority\":4,\"tags\":[\"app\",\"notification\"]}",
    "priority":3,
    "expires":1781188584,
    "time":1781145384,
    "event":"message"
}

Configuration is loaded from config.toml.

Example config.toml:

[ntfy]
server = "http://localhost:8080"
topic = "androcall"
token = ""
username = ""
password = ""
priority = 3
tags = ["mail"]

Usage:

    python ntfy_sender.py \
        --title "New Gmail" \
        --message "You have a new email"

    python ntfy_sender.py \
        --topic alerts \
        --priority 5 \
        --title "Critical" \
        --message "Disk full"
"""

from __future__ import annotations

import sys
import argparse

DEBUG=False
if any(i in ('--debug', '--pydebugger') for i in sys.argv[1:]):
    DEBUG=True
    print(f"DEBUG: {DEBUG}")
try:
    from rchf import CustomRichHelpFormatter  # type: ignore
except Exception as e:
    if DEBUG:
        import traceback
        traceback.print_exc()
    CustomRichHelpFormatter = argparse.RawTextHelpFormatter
import json
from pathlib import Path
from typing import Any

import requests
import tomllib

try:
    from .config import ConfigManager as Config  # type: ignore
except:
    try:
        from config import ConfigManager as Config  # type: ignore
    except:
        if DEBUG:
            import traceback
            traceback.print_exc()  # type: ignore
        class Config:
            """Load configuration from config.toml"""

            def __init__(self, config_file: str | None):
                if config_file and Path(config_file).exists():
                    self.config_file = Path(config_file)

                    # if not self.config_file.exists():
                    #     raise FileNotFoundError(
                    #         f"Config file not found: {self.config_file}"
                    #     )

                    with open(self.config_file, "rb") as fh:
                        self.data = tomllib.load(fh)
                else:
                    print(f"Config file not found: {self.config_file} use default 'config.toml'")
                    config_file = "config.toml"
                    if config_file and Path(config_file).exists():
                        self.config_file = Path(config_file)

                    with open(self.config_file, "rb") as fh:
                        self.data = tomllib.load(fh)

            @property
            def ntfy(self) -> dict[str, Any]:
                return self.data.get("ntfy", {})

            @property
            def get(self, *args, **kwargs):
                return self.data.get(*args, **kwargs)

            @property
            def load(self):
                return self

# Use a temporary no-op DB (won't be called) for ConfigManager
class _FakeDB:  # noqa: D101
    pass

class NtfySender:
    def __init__(
        self,
        server: str | None = None,
        topic: str | None = None,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.config = Config(_FakeDB())  # type: ignore
        try:
            self.config = self.config.load()  # type: ignore
        except:
            pass

        self.server = self.config.get("ntfy", {}).get("server", server.rstrip("/") if server else "https://ntfy.sh") if self.config.get("ntfy", {}) else {} # type: ignore
        self.topic = self.config.get("ntfy", {}).get("topic", topic) if self.config.get("ntfy", {}) else {} # type: ignore
        self.token = self.config.get("ntfy", {}).get("token", token) if self.config.get("ntfy", {}) else {} # type: ignore
        self.username = self.config.get("ntfy", {}).get("username", username) if self.config.get("ntfy", {}) else {} # type: ignore
        self.password = self.config.get("ntfy", {}).get("password", password) if self.config.get("ntfy", {}) else {} # type: ignore

    @property
    def url(self) -> str:
        return f"{self.server}/{self.topic}"

    def send(
        self,
        title: str,
        message: str,
        priority: int = 3,
        tags: list[str] | None = ["mail"],
    ) -> dict:

        payload = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or [],
        }

        headers = {
            "Content-Type": "application/json",
        }

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        response = requests.post(
            self.url,
            json=payload,
            headers=headers,
            auth=(
                (self.username, self.password)
                if self.username and self.password
                else None
            ),
            timeout=30,
        )

        response.raise_for_status()

        try:
            return response.json()
        except Exception:
            return {
                "status": "ok",
                "response": response.text,
            }


class CLI:
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            description="Send notification to ntfy.",
            prog='ntfy_sender',
            formatter_class=CustomRichHelpFormatter,
        )

        self.parser.add_argument(
            "-c",
            "--config",
            default="config.toml",
            help="Path to config.toml",
        )

        self.parser.add_argument(
            "--topic",
            default="androcall",
            help="Override configured topic",
        )

        self.parser.add_argument(
            "-t",
            "--title",
            default="test",
            help="Notification title",
        )

        self.parser.add_argument(
            "-m",
            "--message",
            required=True,
            nargs='*',
            help="Notification body",
        )

        self.parser.add_argument(
            "-p",
            "--priority",
            type=int,
            choices=[1, 2, 3, 4, 5],
            default=4,
            help="Notification priority",
        )

        self.parser.add_argument(
            "-tg",
            "--tags",
            nargs="*",
            default=["test"],
            help="Notification tags",
        )

        self.parser.add_argument(
            '-d',
            "--debug",
            action='store_true',
            help="Debug/Verbose mode",
        )

    def run(self):
        if len(sys.argv) == 1:
            self.parser.print_help()

        args = self.parser.parse_args()

        cfg = Config(args.config)

        try:
            cfg = cfg.load()  # type: ignore
            ntfy_cfg = cfg.get("ntfy", {})  # type: ignore
        except:
            if DEBUG:
                import traceback
                traceback.print_exc() 
            ntfy_cfg = cfg.ntfy

        sender = NtfySender(
            server=ntfy_cfg["server"],
            topic=args.topic or ntfy_cfg["topic"],
            token=ntfy_cfg.get("token"),
            username=ntfy_cfg.get("username"),
            password=ntfy_cfg.get("password"),
        )

        result = sender.send(
            title=args.title,
            message=" ".join(args.message),
            priority=args.priority
            or ntfy_cfg.get("priority", 3),
            tags=args.tags
            or ntfy_cfg.get("tags", []),
        )

        print(json.dumps(result, indent=4))


def main():
    CLI().run()


if __name__ == "__main__":
    main()