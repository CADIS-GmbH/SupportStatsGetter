"""SupportStatsGetter
====================

Tool for collecting ticket statistics from **Jira Service Management**.
The data is stored in a normalized SQLite database. The code is based on
``TeamSupportStatsGetter.py`` but no longer supports TeamSupport.
"""

from __future__ import annotations

import logging
import functools
import inspect

import argparse
import json
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
import dateutil.parser
import pytz
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, DateTime, Boolean,
    Integer, Text, ForeignKey, select, func, text, inspect
)
from sqlalchemy.engine import Engine


def log_call(func):
    """Decorator to log function calls with parameters at DEBUG level."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        params = ", ".join(
            f"{k}={v!r}" for k, v in bound.arguments.items() if k != "self"
        )
        logging.getLogger(func.__module__).debug(f"{func.__name__}({params})")
        return func(*args, **kwargs)

    return wrapper


def setup_logging(level: str, log_file: Path) -> None:
    """Configure root logger with console and file handlers."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger()
    logger.setLevel(numeric)

    formatter = logging.Formatter(
        "%{asctime}s - %{name}s - %{levelname}s - %{message}s", style="%"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


@dataclass
class Config:
    """Runtime configuration values."""

    db_path: Path = Path("support_stats.sqlite")
    jira_base_url: str | None = None
    jira_user: str | None = None
    jira_token: str | None = None
    base_jql: str | None = None
    log_level: str = "INFO"
    log_file: Path = Path("support_stats.log")

    @staticmethod
    @log_call
    def load(path: Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(
            db_path=Path(data.get("db_path", "support_stats.sqlite")),
            jira_base_url=data.get("jira_base_url"),
            jira_user=data.get("jira_user"),
            jira_token=data.get("jira_token"),
            base_jql=data.get("base_jql"),
            log_level=data.get("log_level", "INFO"),
            log_file=Path(data.get("log_file", "support_stats.log")),
        )


class SQLiteStore:
    """Simple wrapper around an SQLite database using SQLAlchemy."""

    @log_call
    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_engine("sqlite+pysqlite:///support_stats.sqlite")
        self.meta = MetaData()
        self._prepare_schema()

    @log_call
    def _prepare_schema(self) -> None:
        self.jira_tickets = Table(
            "jira_tickets", self.meta,
            Column("id", String, primary_key=True),
            Column("key", String),
            Column("summary", Text),
            Column("status", String),
            Column("created", DateTime),
            Column("resolved", DateTime, nullable=True),
            Column("updated", DateTime),
        )
        self.jira_comments = Table(
            "jira_comments",
            self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("author", String),
            Column("body", Text),
            Column("public", Boolean, default=True),
            Column("created", DateTime),
            Column("updated", DateTime),
        )
        self.jira_worklogs = Table(
            "jira_worklogs",
            self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("author", String),
            Column("time_spent_seconds", Integer),
            Column("started", DateTime),
            Column("text", Text),
            Column("created", DateTime),
        )
        self.meta.create_all(self.engine)

        inspector = inspect(self.engine)
        cols = [c["name"] for c in inspector.get_columns("jira_tickets")]
        if "updated" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN updated DATETIME"))

        cols = [c["name"] for c in inspector.get_columns("jira_comments")]
        if "public" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN public BOOLEAN"))

    @log_call
    def insert_many(self, table: Table, rows: Iterable[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(table.insert().prefix_with("OR REPLACE"), list(rows))

    @log_call
    def newest_updated_ts(self) -> datetime.datetime | None:
        inspector = inspect(self.engine)
        cols = [c["name"] for c in inspector.get_columns("jira_tickets")]
        if "updated" not in cols:
            return None
        with self.engine.connect() as conn:
            result = conn.execute(select(func.max(self.jira_tickets.c.updated))).scalar()
        return result


class SupportStatsGetter:
    """Retrieve Jira ticket data and store it in SQLite."""

    @log_call
    def __init__(self, config: Config):
        self.config = config
        self.store = SQLiteStore(create_engine(f"sqlite+pysqlite:///{config.db_path}"))

    @log_call
    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    @log_call
    def latest_update_timestamp(self) -> datetime.datetime | None:
        return self.store.newest_updated_ts()

    @log_call
    def fetch_jira_comments(self, issue_id_or_key: str) -> list[dict[str, Any]]:
        """Fetch all comments for a Jira issue (handles pagination)."""
        if not self.config.jira_base_url:
            raise ValueError("jira_base_url not configured")

        url = f"{self.config.jira_base_url}/rest/api/3/issue/{issue_id_or_key}/comment"
        start_at = 0
        max_results = 50
        all_comments: list[dict[str, Any]] = []

        while True:
            params = {"startAt": start_at, "maxResults": max_results}
            r = requests.get(
                url,
                params=params,
                auth=self._jira_auth(),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
            comments = data.get("comments", [])
            for comment in comments:
                comment["created"] = self._parse_ts(comment.get("created"))
                comment["updated"] = self._parse_ts(comment.get("updated"))
                comment["public"] = comment.get("jsdPublic")
            all_comments.extend(comments)

            total = data.get("total", 0)
            if start_at + len(comments) >= total or not comments:
                break
            start_at += len(comments)

        return all_comments

    @log_call
    def fetch_jira_worklogs(self, issue_id_or_key: str) -> list[dict[str, Any]]:
        """Fetch all worklogs for a Jira issue (handles pagination)."""
        if not self.config.jira_base_url:
            raise ValueError("jira_base_url not configured")

        url = f"{self.config.jira_base_url}/rest/api/3/issue/{issue_id_or_key}/worklog"
        start_at = 0
        max_results = 50
        all_logs: list[dict[str, Any]] = []

        while True:
            params = {"startAt": start_at, "maxResults": max_results}
            r = requests.get(
                url,
                params=params,
                auth=self._jira_auth(),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
            logs = data.get("worklogs", [])
            for log in logs:
                log["created"] = self._parse_ts(log.get("created"))
                log["updated"] = self._parse_ts(log.get("updated"))
                log["started"] = self._parse_ts(log.get("started"))
            all_logs.extend(logs)

            total = data.get("total", 0)
            if start_at + len(logs) >= total or not logs:
                break
            start_at += len(logs)

        return all_logs

    @log_call
    def fetch_jira_tickets(self, jql: str = "", updated_since: datetime.datetime | None = None) -> list[dict[str, Any]]:
        """Fetch issues from Jira using the search API (handles pagination)."""
        if not self.config.jira_base_url:
            logging.getLogger(__name__).error("jira_base_url not configured")
            raise ValueError("jira_base_url not configured")

        base = self.config.base_jql or ""
        parts = []
        if base:
            parts.append(base)
        extra = jql.strip()
        if extra:
            parts.append(f"({extra})")
        if updated_since:
            ts = updated_since.astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M")
            parts.append(f"updated >= \"{ts}\"")
        final_jql = " AND ".join(parts)

        url = f"{self.config.jira_base_url}/rest/api/3/search"
        start_at = 0
        max_results = 50
        all_issues: list[dict[str, Any]] = []

        while True:
            params = {
                "jql": final_jql,
                "maxResults": max_results,
                "startAt": start_at,
            }
            logging.getLogger(__name__).info(f"GET {url} params={params}")
            r = requests.get(
                url,
                params=params,
                auth=self._jira_auth(),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
            issues = data.get("issues", [])
            for issue in issues:
                fields = issue.get("fields", {})
                issue["created"] = self._parse_ts(fields.get("created"))
                issue["resolved"] = self._parse_ts(fields.get("resolutiondate"))
                issue["updated"] = self._parse_ts(fields.get("updated"))
            all_issues.extend(issues)

            total = data.get("total", 0)
            if start_at + len(issues) >= total or not issues:
                break
            start_at += len(issues)

        return all_issues

    @log_call
    def store_jira_tickets(self, issues: Iterable[dict[str, Any]]) -> None:
        records = [
            {
                "id": issue["id"],
                "key": issue.get("key"),
                "summary": issue.get("fields", {}).get("summary"),
                "status": issue.get("fields", {}).get("status", {}).get("name"),
                "created": issue.get("created"),
                "resolved": issue.get("resolved"),
                "updated": issue.get("updated"),
            }
            for issue in issues
        ]
        self.store.insert_many(self.store.jira_tickets, records)

    def store_jira_comments(self, issue_id: str, comments: Iterable[dict[str, Any]]) -> None:
        records = []
        for comment in comments:
            body = comment.get("body")
            if isinstance(body, dict):
                body = json.dumps(body)
            records.append(
                {
                    "id": comment.get("id"),
                    "ticket_id": issue_id,
                    "author": comment.get("author", {}).get("displayName"),
                    "body": body,
                    "public": comment.get("public"),
                    "created": comment.get("created"),
                    "updated": comment.get("updated"),
                }
            )
        self.store.insert_many(self.store.jira_comments, records)

    def store_jira_worklogs(self, issue_id: str, worklogs: Iterable[dict[str, Any]]) -> None:
        records = []
        for log in worklogs:
            text = log.get("comment")
            if isinstance(text, dict):
                text = json.dumps(text)
            records.append(
                {
                    "id": log.get("id"),
                    "ticket_id": issue_id,
                    "author": log.get("author", {}).get("displayName"),
                    "time_spent_seconds": log.get("timeSpentSeconds"),
                    "started": log.get("started"),
                    "text": text,
                    "created": log.get("created"),
                }
            )
        self.store.insert_many(self.store.jira_worklogs, records)

    @staticmethod
    @log_call
    def _parse_ts(value: str | None) -> datetime.datetime | None:
        if not value:
            return None
        ts = dateutil.parser.isoparse(value)
        return ts if ts.tzinfo else pytz.utc.localize(ts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Jira ticket statistics")
    parser.add_argument(
        "--config",
        "-c",
        default="config.json",
        help="Path to the configuration JSON file",
    )
    parser.add_argument(
        "--log-level",
        help="Logging level (overrides config)",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logging.basicConfig(level=logging.ERROR)
        logging.error(f"No configuration file found at {cfg_path}.")
        raise SystemExit(1)

    cfg = Config.load(cfg_path)
    log_level = args.log_level or cfg.log_level
    setup_logging(log_level, cfg.log_file)

    getter = SupportStatsGetter(cfg)
    if cfg.jira_base_url:
        try:
            since = getter.latest_update_timestamp()
            issues = getter.fetch_jira_tickets(updated_since=since)
            getter.store_jira_tickets(issues)
            logging.info(f"Stored {len(issues)} Jira tickets in {cfg.db_path}")
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch or store Jira tickets")
        try:
            comments_total = 0
            worklogs_total = 0
            for issue in issues:
                comments = getter.fetch_jira_comments(issue["id"])
                getter.store_jira_comments(issue["id"], comments)
                comments_total += len(comments)
                worklogs = getter.fetch_jira_worklogs(issue["id"])
                getter.store_jira_worklogs(issue["id"], worklogs)
                worklogs_total += len(worklogs)
            logging.info(
                f"Stored {len(issues)} Jira tickets, {comments_total} comments and {worklogs_total} worklogs in {cfg.db_path}"
            )
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch or store Jira comments or worklogs")
    else:
        logging.error("Jira configuration missing in config file.")
