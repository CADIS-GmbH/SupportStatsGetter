"""SupportStatsGetter
====================

Tool for collecting ticket statistics from **Jira Service Management**.
The data is stored in a normalized SQLite database. The code is based on
``TeamSupportStatsGetter.py`` but no longer supports TeamSupport.
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

    db_path: Path = Path("support_stats.sqlite")
    jira_base_url: str | None = None
    jira_user: str | None = None
    jira_token: str | None = None
    base_jql: str | None = None

    @staticmethod
    def load(path: Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(
            db_path=Path(data.get("db_path", "support_stats.sqlite")),
            jira_base_url=data.get("jira_base_url"),
            jira_user=data.get("jira_user"),
            jira_token=data.get("jira_token"),
            base_jql=data.get("base_jql"),
        )


class SQLiteStore:
    """Simple wrapper around an SQLite database using SQLAlchemy."""

    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_engine("sqlite+pysqlite:///support_stats.sqlite")
        self.meta = MetaData()
        self._prepare_schema()

    def _prepare_schema(self) -> None:
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
    """Retrieve Jira ticket data and store it in SQLite."""

    def __init__(self, config: Config):
        self.config = config
        self.store = SQLiteStore(create_engine(f"sqlite+pysqlite:///{config.db_path}"))

    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    def fetch_jira_tickets(self, jql: str = "") -> list[dict[str, Any]]:
        """Fetch issues from Jira using the search API (handles pagination)."""
        if not self.config.jira_base_url:
            raise ValueError("jira_base_url not configured")

        base = self.config.base_jql or ""
        extra = jql.strip()
        if base and extra:
            final_jql = f"{base} AND ({extra})"
        else:
            final_jql = base or extra

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
            all_issues.extend(issues)

            total = data.get("total", 0)
            if start_at + len(issues) >= total or not issues:
                break
            start_at += len(issues)

        return all_issues

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
    parser = argparse.ArgumentParser(description="Collect Jira ticket statistics")
    parser.add_argument(
        "--config",
        "-c",
        default="config.json",
        help="Path to the configuration JSON file",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = Config.load(cfg_path)
        getter = SupportStatsGetter(cfg)
        if cfg.jira_base_url:
            issues = getter.fetch_jira_tickets()
            getter.store_jira_tickets(issues)
            print(f"Stored {len(issues)} Jira tickets in {cfg.db_path}")
        else:
            print("Jira configuration missing in config file.")
    else:
        print(f"No configuration file found at {cfg_path}.")
