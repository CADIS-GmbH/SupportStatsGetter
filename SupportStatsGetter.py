"""SupportStatsGetter
=====================

This module provides a structured starting point for collecting support ticket
statistics from TeamSupport or Jira Service Management. It mirrors a subset of
the functionality found in ``TeamSupportStatsGetter.py`` but replaces JSON file
output with normalized SQLite storage. The code is organised so that future
extensions such as KPI calculation, legacy data import, configurable
parameters and a GUI can be added easily.
"""

from __future__ import annotations

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
    Integer, Text, ForeignKey
)
from sqlalchemy.engine import Engine


@dataclass
class Config:
    """Runtime configuration values."""

    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    db_path: Path = Path("support_stats.sqlite")
    jira_base_url: str | None = None
    jira_user: str | None = None
    jira_token: str | None = None

    @staticmethod
    def from_args(args: argparse.Namespace) -> "Config":
        return Config(
            base_url=args.base_url,
            username=args.username,
            password=args.password,
            db_path=Path(args.db_path),
            jira_base_url=args.jira_base_url,
            jira_user=args.jira_user,
            jira_token=args.jira_token,
        )


class SQLiteStore:
    """Simple wrapper around an SQLite database using SQLAlchemy."""

    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_engine("sqlite+pysqlite:///support_stats.sqlite")
        self.meta = MetaData()
        self._prepare_schema()

    def _prepare_schema(self) -> None:
        self.tickets = Table(
            "tickets", self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_number", String),
            Column("name", Text),
            Column("status", String),
            Column("date_created", DateTime),
            Column("date_modified", DateTime),
            Column("resolved_date", DateTime, nullable=True),
        )
        self.actions = Table(
            "actions", self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", ForeignKey("tickets.id")),
            Column("description", Text),
            Column("date_created", DateTime),
            Column("is_visible", Boolean),
            Column("time_spent", Integer),
        )
        self.history = Table(
            "history", self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", ForeignKey("tickets.id")),
            Column("description", Text),
            Column("date_modified", DateTime),
        )
        self.jira_tickets = Table(
            "jira_tickets", self.meta,
            Column("id", String, primary_key=True),
            Column("key", String),
            Column("summary", Text),
            Column("status", String),
            Column("created", DateTime),
            Column("resolved", DateTime, nullable=True),
        )
        self.meta.create_all(self.engine)

    def insert_many(self, table: Table, rows: Iterable[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(table.insert().prefix_with("OR REPLACE"), list(rows))


def parse_arguments() -> argparse.Namespace:
    """Command line options mirroring the legacy script."""
    parser = argparse.ArgumentParser(description="Collect support statistics")
    parser.add_argument("--username", "-u", help="Basic auth user name")
    parser.add_argument("--password", "-p", help="Basic auth password")
    parser.add_argument("--base_url", "-b", help="Base url of the TeamSupport system")
    parser.add_argument("--data_dir", "-d", default="./cadis_data",
                        help="Directory for legacy data import")
    parser.add_argument("--company", "-c", help="Company identifier")
    parser.add_argument("--initial_start_date", "-s", default="2023-01-01",
                        help="Initial start date (unused)")
    parser.add_argument("--recalculate_only", action="store_true",
                        help="Only recalculate existing data store")
    parser.add_argument("--db_path", default="support_stats.sqlite",
                        help="SQLite database file")
    parser.add_argument("--jira_base_url", help="Jira Service Management base URL")
    parser.add_argument("--jira_user", help="Jira user/email")
    parser.add_argument("--jira_token", help="Jira API token")
    return parser.parse_args()


class SupportStatsGetter:
    """Retrieve ticket data and store it in SQLite."""

    def __init__(self, config: Config):
        self.config = config
        self.store = SQLiteStore(create_engine(f"sqlite+pysqlite:///{config.db_path}"))

    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    def fetch_jira_tickets(self, jql: str = "") -> list[dict[str, Any]]:
        """Fetch issues from Jira using the search API."""
        if not self.config.jira_base_url:
            raise ValueError("jira_base_url not configured")
        url = f"{self.config.jira_base_url}/rest/api/3/search"
        params = {"jql": jql or "", "maxResults": 50}
        r = requests.get(url, params=params, auth=self._jira_auth(), headers={"Accept": "application/json"})
        r.raise_for_status()
        issues = r.json().get("issues", [])
        for issue in issues:
            fields = issue.get("fields", {})
            issue["created"] = self._parse_ts(fields.get("created"))
            issue["resolved"] = self._parse_ts(fields.get("resolutiondate"))
        return issues

    def store_tickets(self, tickets: Iterable[dict[str, Any]]) -> None:
        records = [
            {
                "id": t["ID"],
                "ticket_number": t.get("TicketNumber"),
                "name": t.get("Name"),
                "status": t.get("Status"),
                "date_created": t.get("date_created"),
                "date_modified": t.get("date_modified"),
                "resolved_date": t.get("resolved_date"),
            }
            for t in tickets
        ]
        self.store.insert_many(self.store.tickets, records)

    def store_jira_tickets(self, issues: Iterable[dict[str, Any]]) -> None:
        records = [
            {
                "id": issue["id"],
                "key": issue.get("key"),
                "summary": issue.get("fields", {}).get("summary"),
                "status": issue.get("fields", {}).get("status", {}).get("name"),
                "created": issue.get("created"),
                "resolved": issue.get("resolved"),
            }
            for issue in issues
        ]
        self.store.insert_many(self.store.jira_tickets, records)

    @staticmethod
    def _parse_ts(value: str | None) -> datetime.datetime | None:
        if not value:
            return None
        ts = dateutil.parser.isoparse(value)
        return ts if ts.tzinfo else pytz.utc.localize(ts)


if __name__ == "__main__":
    args = parse_arguments()
    cfg = Config.from_args(args)
    getter = SupportStatsGetter(cfg)
    if cfg.jira_base_url:
        issues = getter.fetch_jira_tickets()
        getter.store_jira_tickets(issues)
        print(f"Stored {len(issues)} Jira tickets in {cfg.db_path}")
    else:
        print("No Jira base URL provided. Nothing to fetch.")
