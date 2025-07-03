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

import json
import datetime
from dataclasses import dataclass, field
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

    base_url: str
    username: str
    password: str
    db_path: Path = Path("support_stats.sqlite")
    jira_base_url: str | None = None
    jira_user: str | None = None
    jira_token: str | None = None

    @staticmethod
    def load(path: Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(
            base_url=data["base_url"],
            username=data["username"],
            password=data["password"],
            db_path=Path(data.get("db_path", "support_stats.sqlite")),
            jira_base_url=data.get("jira_base_url"),
            jira_user=data.get("jira_user"),
            jira_token=data.get("jira_token"),
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


class SupportStatsGetter:
    """Retrieve ticket data and store it in SQLite."""

    def __init__(self, config: Config):
        self.config = config
        self.store = SQLiteStore(create_engine(f"sqlite+pysqlite:///{config.db_path}"))

    def _auth_header(self) -> dict[str, str]:
        token = f"{self.config.username}:{self.config.password}".encode("utf-8")
        return {"Authorization": "Basic " + token.decode("base64"), "accept": "application/json"}

    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    def fetch_tickets(self) -> list[dict[str, Any]]:
        url = f"{self.config.base_url}/api/json/Tickets"
        r = requests.get(url, headers=self._auth_header())
        r.raise_for_status()
        tickets = r.json().get("Tickets", [])
        for t in tickets:
            t["date_created"] = self._parse_ts(t.get("DateCreated"))
            t["date_modified"] = self._parse_ts(t.get("DateModified"))
            t["resolved_date"] = self._parse_ts(t.get("ResolvedDate"))
        return tickets

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
    cfg_path = Path("config.json")
    if cfg_path.exists():
        cfg = Config.load(cfg_path)
        getter = SupportStatsGetter(cfg)
        if cfg.jira_base_url:
            issues = getter.fetch_jira_tickets()
            getter.store_jira_tickets(issues)
            print(f"Stored {len(issues)} Jira tickets in {cfg.db_path}")
        else:
            tickets = getter.fetch_tickets()
            getter.store_tickets(tickets)
            print(f"Stored {len(tickets)} tickets in {cfg.db_path}")
    else:
        print("No configuration file found. Please create 'config.json'.")
