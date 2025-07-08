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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
import fnmatch

# Email patterns of internal agents used to detect response times. The patterns
# are loaded from the ``internal_users`` table at runtime.
INTERNAL_EMAIL_PATTERNS: list[str] = []

# Mapping of priority names to numeric positions
PRIORITY_POS_MAP = {
    "C": 100,
    "B": 200,
    "A": 300,
    "AA": 400,
}

# Order of labels used to determine assigned group
GROUP_PRIORITY = [
    "1stLevel",
    "2ndLevel",
    "DIS-Lobster",
    "hardware",
    "product-team",
    "operation",
]

# Statuses that do not count towards adjusted resolution time
STATUS_TO_EXCLUDE_FROM_RESOLUTION_TIME = {
    "CLOSED",
    "CANCELLED",
    "RESOLVED",
    "WAITING FOR CUSTOMER",
    "PENDING",
}

import requests
import dateutil.parser
import pytz
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, DateTime, Boolean,
    Integer, Float, Text, ForeignKey, select, func, text, delete,
    inspect as sa_inspect
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
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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


@dataclass
class JiraCalendarRecord:
    """Per-day statistics for a Jira ticket."""

    date: datetime.date | datetime.datetime
    ticket_id: str
    ticket_key: str
    comments: int = 0
    public_comments: int = 0
    start_assigned_group: str = "1stLevel"
    end_assigned_group: str = "1stLevel"
    start_assigned_user: str = "Unassigned"
    end_assigned_user: str = "Unassigned"
    did_escalate_priority: bool = False
    did_escalate_cs: bool = False
    did_escalate_management: bool = False
    start_priority_txt: str = ""
    start_priority_pos: int = 0
    end_priority_txt: str = ""
    end_priority_pos: int = 0
    time_spent_min: int = 0
    start_status: str = "New"
    end_status: str = "New"
    did_resolve: bool = False
    total_days_open: int = 0
    weekdays_days_since_last_contact: int = 0
    adjusted_resolution_days: int = 0
    adjusted_resolution_weekdays: int = 0
    is_junk: bool = False

    cw: str = field(init=False)
    weekday: int = field(init=False)
    record_id: str = field(init=False)
    date_modified: datetime.datetime = field(default_factory=lambda: datetime.datetime.utcnow())

    def __post_init__(self) -> None:
        if isinstance(self.date, datetime.datetime):
            d = self.date.date()
        else:
            d = self.date
        self.date = d
        self.record_id = f"{self.ticket_key}_{d.isoformat()}"
        self.cw = cw_string_from_datetime(datetime.datetime.combine(d, datetime.time())) or ""
        self.weekday = d.isoweekday()


def cw_string_from_datetime(dt: datetime.datetime | None) -> str | None:
    """Return ISO calendar week string as YYYY.WW."""
    if not dt:
        return None
    return f"{dt.isocalendar().year}.{dt.isocalendar().week:02}"


def active_weeks(start: datetime.datetime | None, end: datetime.datetime | None) -> str | None:
    """Return comma separated list of ISO weeks between two dates."""
    if not start or not end:
        return None
    weeks: set[str] = set()
    d = start
    while d <= end:
        weeks.add(cw_string_from_datetime(d))
        d += datetime.timedelta(days=7)
    weeks.add(cw_string_from_datetime(end))
    return ",".join(sorted(weeks))


def is_internal_email(email: str | None) -> bool:
    """Return True if the email matches any internal pattern."""
    if not email:
        return False
    for pattern in INTERNAL_EMAIL_PATTERNS:
        if fnmatch.fnmatch(email.lower(), pattern.lower()):
            return True
    return False


def group_from_labels(labels: list[str]) -> str:
    """Return the first matching group label or ``1stLevel`` if none found."""
    for grp in GROUP_PRIORITY:
        if grp in labels:
            return grp
    return "1stLevel"


def extract_resolved_date(changelog: list[dict[str, Any]]) -> datetime.datetime | None:
    """Return the date when status changed to Closed, Resolved or Cancelled."""
    closing = {"Closed", "Resolved", "Cancelled"}
    histories = sorted(changelog, key=lambda h: h.get("created") or datetime.datetime.min)
    for hist in histories:
        for item in hist.get("items", []):
            if item.get("field") == "status" and item.get("toString") in closing:
                return hist.get("created")
    return None


def extract_first_response_date(comments: list[dict[str, Any]]) -> datetime.datetime | None:
    """Return timestamp of first public comment from an internal user."""
    if not comments:
        return None
    comments = sorted(comments, key=lambda c: c.get("created"))
    idx = 0
    for c in comments:
        author_email = c.get("author", {}).get("emailAddress")
        if (
            idx > 0
            and c.get("public")
            and is_internal_email(author_email)
        ):
            return c.get("created")
        idx += 1
    return None


def extract_last_response_date(comments: list[dict[str, Any]]) -> datetime.datetime | None:
    """Return timestamp of last public comment from an internal user."""
    if not comments:
        return None
    comments = sorted(comments, key=lambda c: c.get("created"), reverse=True)
    idx = len(comments) - 1
    for c in comments:
        author_email = c.get("author", {}).get("emailAddress")
        if (
            idx > 0
            and c.get("public")
            and is_internal_email(author_email)
        ):
            return c.get("created")
        idx -= 1
    return None


def calculate_kpi_fields(issue: dict[str, Any], comments: list[dict[str, Any]], changelog: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute KPI fields for a Jira ticket."""
    result = issue.copy()
    created = result.get("created")
    updated = result.get("updated")
    resolved = result.get("resolved") or extract_resolved_date(changelog)
    result["resolved"] = resolved

    result["CreatedWeek"] = cw_string_from_datetime(created)
    result["ResolvedWeek"] = cw_string_from_datetime(resolved)
    result["ActiveWeeks"] = active_weeks(created, resolved or updated)
    fields = result.get("fields", {})
    result["TicketSource"] = (
        fields.get("request-channel-source")
        or fields.get("request-channel-type")
    )
    result["FirstResponseDate"] = extract_first_response_date(comments)
    result["LastResponseDate"] = extract_last_response_date(comments)
    result["UpdatedWeek"] = cw_string_from_datetime(updated)
    result["FirstResponseDateWeek"] = cw_string_from_datetime(result["FirstResponseDate"])
    result["LastResponseDateWeek"] = cw_string_from_datetime(result["LastResponseDate"])
    if result["FirstResponseDate"] and created:
        delta = result["FirstResponseDate"] - created
        result["FirstResponseTimeMinutes"] = int(delta.total_seconds() // 60)
    else:
        result["FirstResponseTimeMinutes"] = None
    if resolved and created:
        delta = resolved - created
        result["ResolutionTimeDays"] = delta.total_seconds() / 86400
    else:
        result["ResolutionTimeDays"] = None
    return result


def make_jira_ticket_calendar(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    worklogs: list[dict[str, Any]],
    changelog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return per-day statistics for a Jira issue."""

    created = issue.get("created")
    if not created:
        return []
    end_dt = issue.get("resolved") or issue.get("updated") or datetime.datetime.utcnow()

    fields = issue.get("fields", {})
    start_status = fields.get("status", {}).get("name", "Open")
    assigned_user = fields.get("assignee", {}).get("displayName")
    labels = fields.get("labels", []) or []
    assigned_group = group_from_labels(labels)
    priority_txt = fields.get("priority", {}).get("name") or ""
    priority_pos = PRIORITY_POS_MAP.get(priority_txt, 0)

    adjusted_days = 0
    adjusted_weekdays = 0
    days_open = 0
    days_since_contact = 0

    events: list[tuple[datetime.datetime, str, Any]] = []
    for c in comments:
        ts = c.get("created")
        if ts:
            events.append((ts, "comment", c))
    for w in worklogs:
        ts = w.get("started")
        if ts:
            events.append((ts, "worklog", w))
    for h in changelog:
        ts = h.get("created")
        if ts:
            events.append((ts, "history", h))
    events.sort(key=lambda x: x[0])

    results: list[dict[str, Any]] = []
    cur = created.date()
    end_date = end_dt.date()
    idx = 0
    while cur <= end_date:
        day_actions = 0
        day_public = 0
        day_time = 0
        day_start_status = start_status
        day_start_user = assigned_user
        day_start_group = assigned_group
        day_start_priority_txt = priority_txt
        day_start_priority_pos = priority_pos

        while idx < len(events) and events[idx][0].date() == cur:
            ts, etype, data = events[idx]
            if etype == "comment":
                day_actions += 1
                author_email = data.get("author", {}).get("emailAddress")
                if data.get("public") and is_internal_email(author_email):
                    day_public += 1
                    days_since_contact = 0
            elif etype == "worklog":
                day_time += int((data.get("timeSpentSeconds") or 0) // 60)
            elif etype == "history":
                for item in data.get("items", []):
                    field = item.get("field")
                    to_string = item.get("toString")
                    if field and field.lower() == "status":
                        start_status = to_string or start_status
                    elif field and field.lower() == "assignee":
                        assigned_user = to_string or assigned_user
                    elif field and field.lower() == "priority":
                        priority_txt = to_string or priority_txt
                        priority_pos = PRIORITY_POS_MAP.get(priority_txt, 0)
                    elif field and field.lower() == "labels":
                        labels = [l.strip() for l in (to_string or "").split(",") if l.strip()]
                        assigned_group = group_from_labels(labels) or assigned_group
            idx += 1

        record = JiraCalendarRecord(
            date=cur,
            ticket_id=issue.get("id"),
            ticket_key=issue.get("key"),
            comments=day_actions,
            public_comments=day_public,
            start_assigned_group=day_start_group or "1stLevel",
            end_assigned_group=assigned_group or "1stLevel",
            start_assigned_user=day_start_user or "Unassigned",
            end_assigned_user=assigned_user or "Unassigned",
            did_escalate_priority=day_start_priority_pos < priority_pos,
            start_priority_txt=day_start_priority_txt,
            start_priority_pos=day_start_priority_pos,
            end_priority_txt=priority_txt,
            end_priority_pos=priority_pos,
            start_status=day_start_status,
            end_status=start_status,
            time_spent_min=day_time,
            total_days_open=days_open,
            weekdays_days_since_last_contact=days_since_contact,
            adjusted_resolution_days=adjusted_days,
            adjusted_resolution_weekdays=adjusted_weekdays,
            is_junk=start_status.lower() == "cancelled",
        )
        record.did_resolve = (
            not day_start_status.lower().startswith("closed")
            and record.end_status.lower() in {"resolved", "closed", "cancelled"}
        )
        results.append(record.__dict__)

        if start_status.upper() not in STATUS_TO_EXCLUDE_FROM_RESOLUTION_TIME:
            adjusted_days += 1
            if cur.isoweekday() < 6:
                adjusted_weekdays += 1
        if start_status.lower() not in {"resolved", "closed", "cancelled"}:
            days_open += 1
        if day_public == 0 and cur.isoweekday() < 6:
            days_since_contact += 1

        cur += datetime.timedelta(days=1)

    return results


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
            "jira_tickets",
            self.meta,
            Column("id", String, primary_key=True),
            Column("key", String),
            Column("summary", Text),
            Column("status", String),
            Column("assignee", String),
            Column("labels", Text),
            Column("priority", String),
            Column("created", DateTime),
            Column("resolved", DateTime, nullable=True),
            Column("updated", DateTime),
            Column("ticket_source", String),
            Column("created_week", String),
            Column("resolved_week", String),
            Column("updated_week", String),
            Column("active_weeks", Text),
            Column("first_response_date", DateTime),
            Column("last_response_date", DateTime),
            Column("first_response_date_week", String),
            Column("last_response_date_week", String),
            Column("first_response_time_minutes", Integer),
            Column("resolution_time_days", Float),
        )
        self.jira_comments = Table(
            "jira_comments",
            self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("issue_key", String),
            Column("author", String),
            Column("type", String),
            Column("body", Text),
            Column("public", Boolean, default=True),
            Column("internal_user", Boolean),
            Column("created", DateTime),
            Column("updated", DateTime),
        )
        self.jira_worklogs = Table(
            "jira_worklogs",
            self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("author", String),
            Column("time_spent_minutes", Integer),
            Column("started", DateTime),
            Column("started_week", String),
            Column("text", Text),
            Column("created", DateTime),
            Column("created_week", String),
        )
        self.jira_changelog = Table(
            "jira_changelog",
            self.meta,
            Column("id", String, primary_key=True),
            Column("history_id", String),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("author", String),
            Column("created", DateTime),
            Column("field", String),
            Column("fieldtype", String),
            Column("from_value", String),
            Column("from_string", String),
            Column("to_value", String),
            Column("to_string", String),
        )
        self.jira_calendar = Table(
            "jira_calendar",
            self.meta,
            Column("record_id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("ticket_key", String),
            Column("date", DateTime),
            Column("comments", Integer),
            Column("public_comments", Integer),
            Column("start_assigned_group", String),
            Column("end_assigned_group", String),
            Column("start_assigned_user", String),
            Column("end_assigned_user", String),
            Column("did_escalate_priority", Boolean),
            Column("did_escalate_cs", Boolean),
            Column("did_escalate_management", Boolean),
            Column("start_priority_txt", String),
            Column("start_priority_pos", Integer),
            Column("end_priority_txt", String),
            Column("end_priority_pos", Integer),
            Column("time_spent_min", Integer),
            Column("start_status", String),
            Column("end_status", String),
            Column("did_resolve", Boolean),
            Column("total_days_open", Integer),
            Column("weekdays_days_since_last_contact", Integer),
            Column("adjusted_resolution_days", Integer),
            Column("adjusted_resolution_weekdays", Integer),
            Column("is_junk", Boolean),
            Column("cw", String),
            Column("weekday", Integer),
            Column("date_modified", DateTime),
        )
        self.internal_users = Table(
            "internal_users",
            self.meta,
            Column("pattern", String, primary_key=True),
        )
        self.week_reporting = Table(
            "week_reporting",
            self.meta,
            Column("calendar_week", String, primary_key=True),
            Column("reporting_month", String),
            Column("reporting_quarter", String),
        )
        self.meta.create_all(self.engine)

        # populate week to reporting month/quarter lookup
        with self.engine.begin() as conn:
            count = conn.execute(select(func.count()).select_from(self.week_reporting)).scalar()
            if count == 0:
                rows = []
                for week in range(1, 53):
                    if week <= 5:
                        month = 1
                    elif week <= 9:
                        month = 2
                    elif week <= 13:
                        month = 3
                    elif week <= 17:
                        month = 4
                    elif week <= 22:
                        month = 5
                    elif week <= 26:
                        month = 6
                    elif week <= 31:
                        month = 7
                    elif week <= 35:
                        month = 8
                    elif week <= 39:
                        month = 9
                    elif week <= 44:
                        month = 10
                    elif week <= 48:
                        month = 11
                    else:
                        month = 12
                    quarter = (month - 1) // 3 + 1
                    rows.append(
                        {
                            "calendar_week": f"2025.{week:02}",
                            "reporting_month": f"2025.{month:02}",
                            "reporting_quarter": f"2025.Q{quarter}",
                        }
                    )
                conn.execute(self.week_reporting.insert(), rows)

        # create a view exposing reporting month and quarter columns
        view_sql = """
            CREATE VIEW IF NOT EXISTS jira_tickets_reporting AS
            SELECT
                jt.*, 
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jt.created_week) AS created_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jt.created_week) AS created_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jt.resolved_week) AS resolved_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jt.resolved_week) AS resolved_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jt.updated_week) AS updated_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jt.updated_week) AS updated_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jt.first_response_date_week) AS first_response_date_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jt.first_response_date_week) AS first_response_date_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jt.last_response_date_week) AS last_response_date_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jt.last_response_date_week) AS last_response_date_reportingquarter
            FROM jira_tickets jt
        """
        with self.engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS jira_tickets_reporting"))
            conn.execute(text(view_sql))

        worklog_view_sql = """
            CREATE VIEW IF NOT EXISTS jira_worklogs_reporting AS
            SELECT
                jw.*,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jw.started_week) AS started_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jw.started_week) AS started_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jw.created_week) AS created_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jw.created_week) AS created_reportingquarter
            FROM jira_worklogs jw
        """
        with self.engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS jira_worklogs_reporting"))
            conn.execute(text(worklog_view_sql))

        inspector = sa_inspect(self.engine)
        cols = [c["name"] for c in inspector.get_columns("jira_tickets")]
        if "updated" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN updated DATETIME"))
        if "ticket_source" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN ticket_source VARCHAR"))
        if "assignee" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN assignee VARCHAR"))
        if "labels" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN labels TEXT"))
        if "priority" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN priority VARCHAR"))
        if "created_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN created_week VARCHAR"))
        if "resolved_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN resolved_week VARCHAR"))
        if "updated_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN updated_week VARCHAR"))
        if "active_weeks" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN active_weeks TEXT"))
        if "first_response_date" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN first_response_date DATETIME"))
        if "last_response_date" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN last_response_date DATETIME"))
        if "first_response_date_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN first_response_date_week VARCHAR"))
        if "last_response_date_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN last_response_date_week VARCHAR"))
        if "first_response_time_minutes" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN first_response_time_minutes INTEGER"))
        if "resolution_time_days" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN resolution_time_days FLOAT"))

        cols = [c["name"] for c in inspector.get_columns("jira_comments")]
        if "public" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN public BOOLEAN"))
        if "issue_key" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN issue_key VARCHAR"))
        if "type" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN type VARCHAR"))
        if "internal_user" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN internal_user BOOLEAN"))

        cols = [c["name"] for c in inspector.get_columns("jira_worklogs")]
        if "started_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_worklogs ADD COLUMN started_week VARCHAR"))
        if "created_week" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_worklogs ADD COLUMN created_week VARCHAR"))
        if "time_spent_minutes" not in cols and "time_spent_seconds" in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_worklogs RENAME COLUMN time_spent_seconds TO time_spent_minutes"))
        elif "time_spent_minutes" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_worklogs ADD COLUMN time_spent_minutes INTEGER"))

    @log_call
    def insert_many(self, table: Table, rows: Iterable[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(table.insert().prefix_with("OR REPLACE"), list(rows))

    @log_call
    def newest_updated_ts(self) -> datetime.datetime | None:
        inspector = sa_inspect(self.engine)
        cols = [c["name"] for c in inspector.get_columns("jira_tickets")]
        if "updated" not in cols:
            return None
        with self.engine.connect() as conn:
            result = conn.execute(select(func.max(self.jira_tickets.c.updated))).scalar()
        return result

    @log_call
    def load_internal_patterns(self) -> list[str]:
        inspector = sa_inspect(self.engine)
        if "internal_users" not in inspector.get_table_names():
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.internal_users.c.pattern)).fetchall()
        return [r[0] for r in rows]

    @log_call
    def get_ticket_with_related(self, ticket_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Load a ticket and related records from the database."""
        with self.engine.connect() as conn:
            t = conn.execute(
                select(self.jira_tickets).where(self.jira_tickets.c.id == ticket_id)
            ).mappings().fetchone()
            if not t:
                return {}, [], [], []
            comments = conn.execute(
                select(self.jira_comments).where(self.jira_comments.c.ticket_id == ticket_id)
            ).mappings().fetchall()
            worklogs = conn.execute(
                select(self.jira_worklogs).where(self.jira_worklogs.c.ticket_id == ticket_id)
            ).mappings().fetchall()
            changelog = conn.execute(
                select(self.jira_changelog).where(self.jira_changelog.c.ticket_id == ticket_id)
            ).mappings().fetchall()
        return dict(t), [dict(r) for r in comments], [dict(r) for r in worklogs], [dict(r) for r in changelog]


class SupportStatsGetter:
    """Retrieve Jira ticket data and store it in SQLite."""

    @log_call
    def __init__(self, config: Config):
        self.config = config
        self.store = SQLiteStore(create_engine(f"sqlite+pysqlite:///{config.db_path}"))
        global INTERNAL_EMAIL_PATTERNS
        INTERNAL_EMAIL_PATTERNS = self.store.load_internal_patterns()

    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    @log_call
    def latest_update_timestamp(self) -> datetime.datetime | None:
        return self.store.newest_updated_ts()

    @log_call
    def recalculate_all(self) -> None:
        """Recalculate KPI fields and calendar for all stored tickets."""
        with self.store.engine.connect() as conn:
            tickets = conn.execute(select(self.store.jira_tickets.c.id)).fetchall()
        for (ticket_id,) in tickets:
            issue_row, comments, worklogs, changelog = self.store.get_ticket_with_related(ticket_id)
            if not issue_row:
                continue
            fields = {
                "summary": issue_row.get("summary"),
                "status": {"name": issue_row.get("status")},
            }
            if issue_row.get("assignee"):
                fields["assignee"] = {"displayName": issue_row.get("assignee")}
            if issue_row.get("labels"):
                fields["labels"] = [l for l in issue_row.get("labels", "").split(",") if l]
            if issue_row.get("priority"):
                fields["priority"] = {"name": issue_row.get("priority")}
            issue = {
                "id": issue_row["id"],
                "key": issue_row.get("key"),
                "created": issue_row.get("created"),
                "resolved": issue_row.get("resolved"),
                "updated": issue_row.get("updated"),
                "fields": fields,
            }
            processed_issue = calculate_kpi_fields(issue, comments, changelog)
            self.store_jira_tickets([processed_issue])
            calendar_rows = make_jira_ticket_calendar(processed_issue, comments, worklogs, changelog)
            with self.store.engine.begin() as conn:
                conn.execute(delete(self.store.jira_calendar).where(self.store.jira_calendar.c.ticket_id == ticket_id))
            self.store.insert_many(self.store.jira_calendar, calendar_rows)

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
            params = {
                "startAt": start_at,
                "maxResults": max_results,
                "expand": "renderedBody",
            }
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
    def fetch_jira_changelog(self, issue_id_or_key: str) -> list[dict[str, Any]]:
        """Fetch the changelog for a Jira issue (handles pagination)."""
        if not self.config.jira_base_url:
            raise ValueError("jira_base_url not configured")

        url = f"{self.config.jira_base_url}/rest/api/3/issue/{issue_id_or_key}/changelog"
        start_at = 0
        max_results = 50
        all_changes: list[dict[str, Any]] = []

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
            histories = data.get("values") or data.get("histories", [])
            for hist in histories:
                hist["created"] = self._parse_ts(hist.get("created"))
            all_changes.extend(histories)

            total = data.get("total", 0)
            if start_at + len(histories) >= total or not histories:
                break
            start_at += len(histories)

        return all_changes

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
        records = []
        for issue in issues:
            fields = issue.get("fields", {})
            records.append(
                {
                    "id": issue["id"],
                    "key": issue.get("key"),
                    "summary": fields.get("summary"),
                    "status": fields.get("status", {}).get("name"),
                    "assignee": fields.get("assignee", {}).get("displayName"),
                    "labels": ",".join(fields.get("labels", []) or []),
                    "priority": fields.get("priority", {}).get("name"),
                    "created": issue.get("created"),
                    "resolved": issue.get("resolved"),
                    "updated": issue.get("updated"),
                    "ticket_source": issue.get("TicketSource"),
                    "created_week": issue.get("CreatedWeek"),
                    "resolved_week": issue.get("ResolvedWeek"),
                    "updated_week": issue.get("UpdatedWeek"),
                    "active_weeks": issue.get("ActiveWeeks"),
                    "first_response_date": issue.get("FirstResponseDate"),
                    "last_response_date": issue.get("LastResponseDate"),
                    "first_response_date_week": issue.get("FirstResponseDateWeek"),
                    "last_response_date_week": issue.get("LastResponseDateWeek"),
                    "first_response_time_minutes": issue.get("FirstResponseTimeMinutes"),
                    "resolution_time_days": issue.get("ResolutionTimeDays"),
                }
            )
        
        self.store.insert_many(self.store.jira_tickets, records)

    def store_jira_comments(self, issue_id: str, issue_key: str, comments: Iterable[dict[str, Any]]) -> None:
        records = []
        for comment in comments:
            body = comment.get("renderedBody") or comment.get("body")
            if isinstance(body, dict):
                body = json.dumps(body)
            comment_type = None
            if isinstance(comment.get("body"), dict):
                comment_type = comment.get("body", {}).get("type")
            else:
                comment_type = comment.get("type")
            records.append(
                {
                    "id": comment.get("id"),
                    "ticket_id": issue_id,
                    "issue_key": issue_key,
                    "author": comment.get("author", {}).get("displayName"),
                    "type": comment_type,
                    "body": body,
                    "public": comment.get("public"),
                    "internal_user": is_internal_email(comment.get("author", {}).get("emailAddress")),
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
                text = self._adf_to_text(text)
            records.append(
                {
                    "id": log.get("id"),
                    "ticket_id": issue_id,
                    "author": log.get("author", {}).get("displayName"),
                    "time_spent_minutes": int((log.get("timeSpentSeconds") or 0) // 60),
                    "started": log.get("started"),
                    "started_week": cw_string_from_datetime(log.get("started")),
                    "text": text,
                    "created": log.get("created"),
                    "created_week": cw_string_from_datetime(log.get("created")),
                }
            )
        self.store.insert_many(self.store.jira_worklogs, records)

    def store_jira_changelog(self, issue_id: str, changelog: Iterable[dict[str, Any]]) -> None:
        records = []
        for hist in changelog:
            history_id = hist.get("id")
            author = hist.get("author", {}).get("displayName")
            created = hist.get("created")
            items = hist.get("items", [])
            for idx, item in enumerate(items):
                records.append(
                    {
                        "id": f"{history_id}-{idx}",
                        "history_id": history_id,
                        "ticket_id": issue_id,
                        "author": author,
                        "created": created,
                        "field": item.get("field"),
                        "fieldtype": item.get("fieldtype"),
                        "from_value": item.get("from"),
                        "from_string": item.get("fromString"),
                        "to_value": item.get("to"),
                        "to_string": item.get("toString"),
                    }
                )
        self.store.insert_many(self.store.jira_changelog, records)

    @staticmethod
    def _adf_to_text(node: Any) -> str:
        """Return plain text from an Atlassian Document Format structure."""

        def _inner(n: Any) -> str:
            if isinstance(n, dict):
                if n.get("type") == "text":
                    return n.get("text", "")
                return "".join(_inner(c) for c in n.get("content", []))
            if isinstance(n, list):
                return "".join(_inner(c) for c in n)
            if isinstance(n, str):
                return n
            return ""

        return _inner(node).strip()

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
            raw_issues = getter.fetch_jira_tickets(updated_since=since)
            comments_total = 0
            worklogs_total = 0
            changelog_total = 0
            calendar_total = 0
            processed: list[dict[str, Any]] = []
            for issue in raw_issues:
                comments = getter.fetch_jira_comments(issue["id"])
                worklogs = getter.fetch_jira_worklogs(issue["id"])
                changelog = getter.fetch_jira_changelog(issue["id"])

                processed_issue = calculate_kpi_fields(issue, comments, changelog)
                processed.append(processed_issue)

                getter.store_jira_comments(issue["id"], issue.get("key"), comments)
                getter.store_jira_worklogs(issue["id"], worklogs)
                getter.store_jira_changelog(issue["id"], changelog)

                calendar_rows = make_jira_ticket_calendar(issue, comments, worklogs, changelog)
                getter.store.insert_many(getter.store.jira_calendar, calendar_rows)
                calendar_total += len(calendar_rows)

                comments_total += len(comments)
                worklogs_total += len(worklogs)
                for hist in changelog:
                    changelog_total += len(hist.get("items", []))

            getter.store_jira_tickets(processed)
            getter.recalculate_all()
            logging.info(
                f"Stored {len(processed)} Jira tickets, {comments_total} comments, {worklogs_total} worklogs, {changelog_total} changelog items and {calendar_total} calendar rows in {cfg.db_path}"
            )
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch or store Jira data")
    else:
        logging.error("Jira configuration missing in config file.")
