"""SupportStatsGetter
====================

Tool for collecting ticket statistics from **Jira Service Management**.
The data is stored in a normalized SQLite database.
"""

from __future__ import annotations

import logging
import functools
import inspect

import argparse
import json
import datetime
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Pattern, Mapping, Literal
import re
import shutil

# Email patterns of internal agents used to detect response times. The patterns
# are loaded from the ``internal_users`` table at runtime.
INTERNAL_EMAIL_PATTERNS: list[Pattern[str]] = []
# Patterns used to map organization names to a primary organization. Loaded
# from the ``org_lookup`` table at runtime.
ORG_PATTERNS: list[tuple[Pattern[str], str, int]] = []

# Default mapping of priority names to numeric positions. Positions above 0
# count as high priority. Can be overridden with ``priority_positions`` in the
# config file.
DEFAULT_PRIORITY_POSITIONS = {
    "Lowest": -200,
    "Low": -100,
    "Medium": 0,
    "Normal": 0,
    "High": 100,
    "Highest": 200,
    "Critical": 200,
}
# Mapping used at runtime, loaded from the config.
PRIORITY_POS_MAP: dict[str, int] = DEFAULT_PRIORITY_POSITIONS.copy()

# Status lists below are matched case-insensitively and can be set in the
# config file under the key named in each comment.

# Statuses in which a ticket counts as closed: resolved date, open days and
# open ticket counts. Config: ``closed_statuses``.
DEFAULT_CLOSED_STATUSES: list[str] = ["Resolved", "Closed", "Cancelled"]

# Statuses that mark a ticket as cancelled (is_junk) and exclude it from the
# summary KPIs. Config: ``cancelled_statuses``.
DEFAULT_CANCELLED_STATUSES: list[str] = ["Cancelled"]

# Statuses that do not count towards adjusted resolution time.
# Config: ``resolution_time_excluded_statuses``.
DEFAULT_RESOLUTION_TIME_EXCLUDED_STATUSES: list[str] = [
    "Closed",
    "Cancelled",
    "Resolved",
    "Waiting for customer",
    "Pending",
]

# Day thresholds of the backlog KPIs: open longer than ``aged_days_short`` /
# ``aged_days_long`` days (columns aged_5_plus / aged_30_plus) and more than
# ``no_contact_days`` weekdays without contact (over5_days_no_contact). The
# column names stay the same when the thresholds change.
DEFAULT_AGED_DAYS_SHORT = 5
DEFAULT_AGED_DAYS_LONG = 30
DEFAULT_NO_CONTACT_DAYS = 5

# Statuses that are treated as closed for the "over 5 days no contact" KPI.
# Config: ``no_contact_ignore_statuses``.
DEFAULT_NO_CONTACT_IGNORE_STATUSES: list[str] = [
    "Closed",
    "Resolved",
    "Cancelled",
    "Pending",
]

# Lower-cased status sets used at runtime, loaded from the config.
CLOSED_STATUSES: set[str] = {s.lower() for s in DEFAULT_CLOSED_STATUSES}
CANCELLED_STATUSES: set[str] = {s.lower() for s in DEFAULT_CANCELLED_STATUSES}
RESOLUTION_TIME_EXCLUDED_STATUSES: set[str] = {
    s.lower() for s in DEFAULT_RESOLUTION_TIME_EXCLUDED_STATUSES
}

# Request types excluded from the support KPIs. Can be set with
# ``non_support_request_types`` in the config file.
DEFAULT_NON_SUPPORT_REQUEST_TYPES: list[str] = []

# Issue types counted in the support summary views. Can be set with
# ``support_issue_types`` in the config file. An empty list counts all types.
DEFAULT_SUPPORT_ISSUE_TYPES: list[str] = ["Support"]

# Request type categories used for the per-category columns in the summary
# views: category -> patterns. A pattern is either a SQL LIKE pattern matched
# against the request type, or a mapping with ``issue_type`` and/or
# ``request_type`` LIKE patterns that must all match. The first matching
# category wins; unmatched tickets get the default category. None gives one
# category per issue type (the ``support_issue_types``, or every stored issue
# type when that list is empty).
# Can be set with ``request_type_categories`` and
# ``request_type_category_default`` in the config file.
CategoryPattern = str | dict[str, str]
DEFAULT_REQUEST_TYPE_CATEGORIES: dict[str, list[CategoryPattern]] | None = None
DEFAULT_REQUEST_TYPE_CATEGORY: str | None = None
# pattern key -> jira_tickets column
CATEGORY_PATTERN_COLUMNS = {"issue_type": "issuetype", "request_type": "request_type"}


def normalize_category_pattern(pattern: Any) -> CategoryPattern:
    """Validate a request type category pattern from the config."""
    if isinstance(pattern, Mapping):
        unknown = sorted(set(pattern) - set(CATEGORY_PATTERN_COLUMNS))
        if unknown or not pattern:
            raise ValueError(
                f"Invalid request_type_categories pattern {dict(pattern)!r}: "
                f"use the keys {', '.join(CATEGORY_PATTERN_COLUMNS)}"
            )
        return {str(k): str(v) for k, v in pattern.items()}
    return str(pattern)

# Columns of the summary views shown in the dashboard KPI table:
# (column, label, number format). Columns missing in a view are skipped.
DASHBOARD_METRICS: list[tuple[str, str, str]] = [
    ("new_tickets", "New", "0"),
    ("closed_tickets", "Closed", "0"),
    ("ongoing_tickets", "Active", "0"),
    ("close_rate", "Close rate", "0%"),
    ("median_first_reply_minutes", "Median first reply (min)", "0"),
    ("avg_resolution_days", "Avg resolution (days)", "0.0"),
    ("high_or_critical_tickets", "High / critical", "0"),
    ("escalated_tickets", "Priority escalated", "0"),
    ("overall_open", "Open on Fridays", "0.0"),
    # {aged_days_short}, {aged_days_long} and {no_contact_days} come from the config
    ("aged_5_plus", "Open > {aged_days_short} days", "0"),
    ("aged_30_plus", "Open > {aged_days_long} days", "0"),
    ("unassigned", "Unassigned", "0.0"),
    ("over5_days_no_contact", "> {no_contact_days} days no contact", "0.0"),
    ("csat_score", "CSAT score", "0.00"),
    ("csat_answers", "CSAT answers", "0"),
]
# Metrics without a value for periods without data; shown as #N/A so charts
# leave a gap instead of dropping to 0.
DASHBOARD_NULLABLE_METRICS = {
    "median_first_reply_minutes",
    "avg_resolution_days",
    "close_rate",
    "csat_score",
}
# Dashboard charts: (title, chart type, columns). "open_*" / "closed_*" expand
# to the per-category columns.
DASHBOARD_CHARTS: list[tuple[str, str, list[str]]] = [
    ("New vs. closed tickets", "col", ["new_tickets", "closed_tickets"]),
    ("Close rate (closed / new)", "line", ["close_rate"]),
    ("Open backlog (Fridays)", "line", ["overall_open", "aged_5_plus", "aged_30_plus"]),
    ("Unassigned and no contact (Fridays)", "line", ["unassigned", "over5_days_no_contact"]),
    ("Median first reply (minutes)", "line", ["median_first_reply_minutes"]),
    ("Average resolution time (days)", "line", ["avg_resolution_days"]),
    ("Active tickets by category", "stacked", ["open_*"]),
    ("Closed tickets by category", "stacked", ["closed_*"]),
    ("High priority and escalations", "col", ["high_or_critical_tickets", "escalated_tickets"]),
    ("CSAT score", "line", ["csat_score"]),
]
DASHBOARD_TOP_CUSTOMERS = 15

#the script will pull Jira tickets starting from the lastest update timestamp in the db minus this time in minutes.
JIRA_PULL_OVERLAP_MIN = 5 #60*24*7  #5

import requests
import dateutil.parser
import pytz
import yaml
import pandas as pd
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, DateTime, Boolean,
    Integer, Float, Text, ForeignKey, select, func, text, delete,
    inspect as sa_inspect
)
from sqlalchemy.engine import Engine
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.chart import BarChart, LineChart
from openpyxl.chart.data_source import AxDataSource, NumDataSource, NumRef, StrRef
from openpyxl.chart.series import Series, SeriesLabel
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation


def log_call(func):
    """Decorator to log function calls with parameters at DEBUG level."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        params = ", ".join(
            f"{k}={ascii(v)}" for k, v in bound.arguments.items() if k != "self"
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
    output_excel: Path | None = None
    excel_write_mode: Literal[
        "overwrite_with_backup", "overwrite", "no_overwrite"
    ] = "overwrite_with_backup"
    # first quarter shown on the Excel dashboard, e.g. "2025.Q1"; None = one year back
    dashboard_start_quarter: str | None = None
    custom_fields: dict[str, str] = field(default_factory=dict)
    non_support_request_types: list[str] = field(
        default_factory=lambda: DEFAULT_NON_SUPPORT_REQUEST_TYPES.copy()
    )
    priority_deescalation_ignore_minutes: int = 5
    priority_positions: dict[str, int] = field(
        default_factory=lambda: DEFAULT_PRIORITY_POSITIONS.copy()
    )
    support_issue_types: list[str] = field(
        default_factory=lambda: DEFAULT_SUPPORT_ISSUE_TYPES.copy()
    )
    # None = one category per issue type
    request_type_categories: dict[str, list[CategoryPattern]] | None = None
    request_type_category_default: str | None = DEFAULT_REQUEST_TYPE_CATEGORY
    closed_statuses: list[str] = field(
        default_factory=lambda: DEFAULT_CLOSED_STATUSES.copy()
    )
    cancelled_statuses: list[str] = field(
        default_factory=lambda: DEFAULT_CANCELLED_STATUSES.copy()
    )
    resolution_time_excluded_statuses: list[str] = field(
        default_factory=lambda: DEFAULT_RESOLUTION_TIME_EXCLUDED_STATUSES.copy()
    )
    no_contact_ignore_statuses: list[str] = field(
        default_factory=lambda: DEFAULT_NO_CONTACT_IGNORE_STATUSES.copy()
    )
    aged_days_short: int = DEFAULT_AGED_DAYS_SHORT
    aged_days_long: int = DEFAULT_AGED_DAYS_LONG
    no_contact_days: int = DEFAULT_NO_CONTACT_DAYS

    @staticmethod
    @log_call
    def load(path: Path) -> "Config":
        """Load a YAML (.yaml/.yml) or JSON config file."""
        with open(path, "r", encoding="utf-8") as f:
            if Path(path).suffix.lower() in (".yaml", ".yml"):
                data = yaml.safe_load(f) or {}
            else:
                data = json.load(f)
        unknown = sorted(set(data) - {option.name for option in fields(Config)})
        if unknown:
            logging.warning("Unknown config options in %s: %s", path, ", ".join(unknown))

        def as_list(key: str, default: list[str]) -> list[str]:
            # a key without entries (YAML null) is an empty list
            value = data.get(key, default)
            return [] if value is None else list(value)

        categories = data.get("request_type_categories", DEFAULT_REQUEST_TYPE_CATEGORIES)
        return Config(
            db_path=Path(data.get("db_path", "support_stats.sqlite")),
            jira_base_url=(data.get("jira_base_url") or "").rstrip("/") or None,
            jira_user=data.get("jira_user"),
            jira_token=data.get("jira_token"),
            base_jql=data.get("base_jql"),
            log_level=data.get("log_level", "INFO"),
            log_file=Path(data.get("log_file", "support_stats.log")),
            output_excel=Path(data["output_excel"]) if data.get("output_excel") else None,
            excel_write_mode=data.get("excel_write_mode", "overwrite_with_backup"),
            dashboard_start_quarter=data.get("dashboard_start_quarter"),
            custom_fields=data.get("custom_fields", {}),
            non_support_request_types=as_list(
                "non_support_request_types", DEFAULT_NON_SUPPORT_REQUEST_TYPES
            ),
            priority_deescalation_ignore_minutes=data.get(
                "priority_deescalation_ignore_minutes", 5
            ),
            priority_positions={
                str(k): int(v)
                for k, v in data.get(
                    "priority_positions", DEFAULT_PRIORITY_POSITIONS
                ).items()
            },
            support_issue_types=as_list(
                "support_issue_types", DEFAULT_SUPPORT_ISSUE_TYPES
            ),
            request_type_categories=None if categories is None else {
                str(k): [normalize_category_pattern(p) for p in v]
                for k, v in categories.items()
            },
            request_type_category_default=data.get(
                "request_type_category_default", DEFAULT_REQUEST_TYPE_CATEGORY
            ),
            closed_statuses=as_list("closed_statuses", DEFAULT_CLOSED_STATUSES),
            cancelled_statuses=as_list(
                "cancelled_statuses", DEFAULT_CANCELLED_STATUSES
            ),
            resolution_time_excluded_statuses=as_list(
                "resolution_time_excluded_statuses",
                DEFAULT_RESOLUTION_TIME_EXCLUDED_STATUSES,
            ),
            no_contact_ignore_statuses=as_list(
                "no_contact_ignore_statuses", DEFAULT_NO_CONTACT_IGNORE_STATUSES
            ),
            aged_days_short=int(data.get("aged_days_short", DEFAULT_AGED_DAYS_SHORT)),
            aged_days_long=int(data.get("aged_days_long", DEFAULT_AGED_DAYS_LONG)),
            no_contact_days=int(data.get("no_contact_days", DEFAULT_NO_CONTACT_DAYS)),
        )


@dataclass
class JiraCalendarRecord:
    """Per-day statistics for a Jira ticket."""

    date: datetime.date | datetime.datetime
    ticket_id: str
    ticket_key: str
    comments: int = 0
    public_comments: int = 0
    start_assigned_user: str | None = None
    end_assigned_user: str | None = None
    start_assigned_user_email: str | None = None
    end_assigned_user_email: str | None = None
    start_request_type: str | None = None
    end_request_type: str | None = None
    start_issuetype: str | None = None
    end_issuetype: str | None = None
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
    date_modified: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )

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


def is_internal_user(author: Mapping[str, Any] | None) -> bool:
    """Return True if the author appears to be an internal user.

    The email address is checked first. If present and matching one of the
    configured internal patterns, the author is considered internal. If the
    email address is present but does not match any pattern, the author is
    treated as external. If no email address is available, the Jira
    ``accountType`` property is inspected. A value of ``customer`` means the
    author is external, any other value is assumed to be internal.
    """

    if not author:
        return False

    email = author.get("emailAddress")
    if email:
        for pattern in INTERNAL_EMAIL_PATTERNS:
            if pattern.search(email):
                return True
        return False

    account_type = author.get("accountType")
    if account_type in ("customer", "app"):
        return False
    if account_type:
        return True
    return False


def primary_org_from_names(names: list[str]) -> str | None:
    """Return the primary organization for a list of organization names.

    If no match is found via ``ORG_PATTERNS`` and ``names`` is not empty,
    the first organization name is returned instead of ``None``.
    """
    best_primary = None
    best_priority = -1
    best_index = len(names)
    for idx, name in enumerate(names):
        org_best_primary = None
        org_best_prio = -1
        for pattern, primary, priority in ORG_PATTERNS:
            if pattern.search(name):
                if priority > org_best_prio:
                    org_best_prio = priority
                    org_best_primary = primary
        if org_best_primary is None:
            continue
        if org_best_prio > best_priority or (
            org_best_prio == best_priority and idx < best_index
        ):
            best_primary = org_best_primary
            best_priority = org_best_prio
            best_index = idx
    if best_primary is not None:
        return best_primary
    return names[0] if names else None


def extract_resolved_date(changelog: list[dict[str, Any]]) -> datetime.datetime | None:
    """Return the date when status first changed to one of ``CLOSED_STATUSES``."""
    histories = sorted(changelog, key=lambda h: h.get("created") or datetime.datetime.min)
    for hist in histories:
        for item in hist.get("items", []):
            if (
                item.get("field") == "status"
                and (item.get("toString") or "").lower() in CLOSED_STATUSES
            ):
                return hist.get("created")
    return None


def extract_first_response_date(comments: list[dict[str, Any]]) -> datetime.datetime | None:
    """Return timestamp of first public comment from an internal user."""
    if not comments:
        return None
    comments = sorted(comments, key=lambda c: c.get("created"))
    idx = 0
    for c in comments:
        author = c.get("author")
        internal = c.get("internal_user")
        if internal is None:
            internal = is_internal_user(author if isinstance(author, dict) else None)
        if c.get("public") and internal:
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
        author = c.get("author")
        internal = c.get("internal_user")
        if internal is None:
            internal = is_internal_user(author if isinstance(author, dict) else None)
        if idx > 0 and c.get("public") and internal:
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
    request_type_field: str | None = None,
    priority_deescalation_ignore_minutes: int = 5,
) -> list[dict[str, Any]]:
    """Return per-day statistics for a Jira issue."""

    req_field_lower = (request_type_field or "request type").lower()
    issuetype_field_lower = "issuetype"

    created = issue.get("created")
    if not created:
        return []
    end_dt = (
        issue.get("resolved")
        or datetime.datetime.now(datetime.UTC)
    )

    fields = issue.get("fields") or {}

    def derive_initial_state() -> tuple[
        str,
        str | None,
        str | None,
        str,
        int,
        str | None,
        str | None,
    ]:
        """Reconstruct field values at ticket creation from the changelog."""

        status = (fields.get("status") or {}).get("name", "Open")
        user = (fields.get("assignee") or {}).get("displayName")
        user_email = (fields.get("assignee") or {}).get("emailAddress")
        priority = (fields.get("priority") or {}).get("name") or ""

        prio_pos = PRIORITY_POS_MAP.get(priority, 0)
        req_type_raw = (
            fields.get("requestType")
            or fields.get("request_type")
            or fields.get(request_type_field or "")
        )
        req_type = SupportStatsGetter._simple_value(req_type_raw)
        issuetype_raw = (fields.get("issuetype") or {}).get("name")
        issuetype = SupportStatsGetter._simple_value(issuetype_raw) or "Support"

        histories = sorted(
            changelog, key=lambda h: h.get("created") or datetime.datetime.min, reverse=True
        )
        for hist in histories:
            for item in hist.get("items", []):
                field = (item.get("field") or "").lower()
                prev_val = item.get("fromString")
                if prev_val is None:
                    prev_val = item.get("from")
                if prev_val is None:
                    continue
                if field == "status":
                    status = str(prev_val)
                elif field == "assignee":
                    user = str(prev_val)
                    user_email = None
                elif field == "priority":
                    priority = str(prev_val)
                    prio_pos = PRIORITY_POS_MAP.get(priority, 0)
                elif field in {req_field_lower, "request type"}:
                    req_type = SupportStatsGetter._simple_value(prev_val)
                elif field in {issuetype_field_lower, "issue type"}:
                    issuetype = SupportStatsGetter._simple_value(prev_val) or "Support"

        return (
            status,
            user,
            user_email,
            priority,
            prio_pos,
            req_type,
            issuetype,
        )

    (
        start_status,
        assigned_user,
        assigned_user_email,
        priority_txt,
        priority_pos,
        request_type,
        issuetype,
    ) = derive_initial_state()

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
    escalation_grace = datetime.timedelta(
        minutes=priority_deescalation_ignore_minutes
    )
    while cur <= end_date:
        day_actions = 0
        day_public = 0
        day_time = 0
        day_start_status = start_status
        day_start_user = assigned_user
        day_start_user_email = assigned_user_email
        day_start_priority_txt = priority_txt
        day_start_priority_pos = priority_pos
        day_start_request_type = request_type
        day_start_issuetype = issuetype or "Support"
        day_escalated_priority = False
        pending_escalation_ts: datetime.datetime | None = None

        while idx < len(events) and events[idx][0].date() == cur:
            ts, etype, data = events[idx]
            if etype == "comment":
                day_actions += 1
                author = data.get("author")
                internal = data.get("internal_user")
                if internal is None:
                    internal = is_internal_user(author if isinstance(author, dict) else None)
                if data.get("public") and internal:
                    day_public += 1
                    days_since_contact = 0
            elif etype == "worklog":
                if "time_spent_minutes" in data:
                    day_time += int(data.get("time_spent_minutes") or 0)
                else:
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
                        prev_priority_pos = priority_pos
                        priority_txt = to_string or priority_txt
                        priority_pos = PRIORITY_POS_MAP.get(priority_txt, 0)
                        delta = priority_pos - prev_priority_pos
                        if delta > 0 and priority_pos > 0: #only count if the ne prority is higher than 0 (A, AA)
                            pending_escalation_ts = ts
                        elif delta < 0 and pending_escalation_ts:
                            if ts - pending_escalation_ts <= escalation_grace:
                                pending_escalation_ts = None
                            else:
                                if prev_priority_pos > 0:    #only count if the escalated priority was higher than 0 (A, AA)
                                    day_escalated_priority = True
                                pending_escalation_ts = None
                    elif field and field.lower() in {req_field_lower, "request type"}:
                        request_type = to_string or request_type
                    elif field and field.lower() in {issuetype_field_lower, "issue type"}:
                        to_val = to_string if to_string is not None else item.get("to")
                        issuetype = SupportStatsGetter._simple_value(to_val) or "Support"
            idx += 1

        if pending_escalation_ts:
            day_escalated_priority = True

        record = JiraCalendarRecord(
            date=cur,
            ticket_id=issue.get("id"),
            ticket_key=issue.get("key"),
            comments=day_actions,
            public_comments=day_public,
            start_assigned_user=day_start_user,
            end_assigned_user=assigned_user,
            start_assigned_user_email=day_start_user_email,
            end_assigned_user_email=assigned_user_email,
            did_escalate_priority=day_escalated_priority,
            start_priority_txt=day_start_priority_txt,
            start_priority_pos=day_start_priority_pos,
            end_priority_txt=priority_txt,
            end_priority_pos=priority_pos,
            start_request_type=day_start_request_type,
            end_request_type=request_type,
            start_issuetype=day_start_issuetype or "Support",
            end_issuetype=issuetype or "Support",
            start_status=day_start_status,
            end_status=start_status,
            time_spent_min=day_time,
            total_days_open=days_open,
            weekdays_days_since_last_contact=days_since_contact,
            adjusted_resolution_days=adjusted_days,
            adjusted_resolution_weekdays=adjusted_weekdays,
            is_junk=start_status.lower() in CANCELLED_STATUSES,
        )
        record.did_resolve = (
            day_start_status.lower() not in CLOSED_STATUSES
            and record.end_status.lower() in CLOSED_STATUSES
        )
        results.append(record.__dict__)

        if start_status.lower() not in RESOLUTION_TIME_EXCLUDED_STATUSES:
            adjusted_days += 1
            if cur.isoweekday() < 6:
                adjusted_weekdays += 1
        if start_status.lower() not in CLOSED_STATUSES:
            days_open += 1
        if day_public == 0 and cur.isoweekday() < 6:
            days_since_contact += 1

        cur += datetime.timedelta(days=1)

    return results


class DatabaseStore:
    """Simple wrapper around a database using SQLAlchemy."""

    @log_call
    def __init__(
        self,
        engine: Engine | None = None,
        non_support_request_types: Iterable[str] | None = None,
        support_issue_types: Iterable[str] | None = None,
        request_type_categories: Mapping[str, Iterable[CategoryPattern]] | None = None,
        request_type_category_default: str | None = DEFAULT_REQUEST_TYPE_CATEGORY,
        closed_statuses: Iterable[str] | None = None,
        cancelled_statuses: Iterable[str] | None = None,
        no_contact_ignore_statuses: Iterable[str] | None = None,
        aged_days_short: int = DEFAULT_AGED_DAYS_SHORT,
        aged_days_long: int = DEFAULT_AGED_DAYS_LONG,
        no_contact_days: int = DEFAULT_NO_CONTACT_DAYS,
    ):
        if engine is None:
            engine = create_engine("sqlite+pysqlite:///support_stats.sqlite")
        self.engine = engine
        self.meta = MetaData()
        if non_support_request_types is None:
            self.non_support_request_types = DEFAULT_NON_SUPPORT_REQUEST_TYPES.copy()
        else:
            self.non_support_request_types = [
                str(value) for value in non_support_request_types
            ]
        if support_issue_types is None:
            self.support_issue_types = DEFAULT_SUPPORT_ISSUE_TYPES.copy()
        else:
            self.support_issue_types = [str(value) for value in support_issue_types]
        # None = one category per issue type, resolved when the views are created
        self.request_type_categories = None if request_type_categories is None else {
            str(k): [normalize_category_pattern(p) for p in v]
            for k, v in request_type_categories.items()
        }
        self.request_type_category_default = request_type_category_default
        self.closed_statuses = [
            str(value)
            for value in (
                DEFAULT_CLOSED_STATUSES if closed_statuses is None else closed_statuses
            )
        ]
        self.cancelled_statuses = [
            str(value)
            for value in (
                DEFAULT_CANCELLED_STATUSES
                if cancelled_statuses is None
                else cancelled_statuses
            )
        ]
        self.no_contact_ignore_statuses = [
            str(value)
            for value in (
                DEFAULT_NO_CONTACT_IGNORE_STATUSES
                if no_contact_ignore_statuses is None
                else no_contact_ignore_statuses
            )
        ]
        self.aged_days_short = int(aged_days_short)
        self.aged_days_long = int(aged_days_long)
        self.no_contact_days = int(no_contact_days)
        self._prepare_schema()

    def _drop_view_if_exists(self, conn, view_name: str) -> None:
        """Drop a SQL view if it exists for the current connection."""
        inspector = sa_inspect(conn)
        if view_name in inspector.get_view_names():
            conn.execute(text(f"DROP VIEW {view_name}"))

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
            Column("assignee_email", String),
            Column("creator", String),
            Column("creator_email", String),
            Column("request_participants", Text),
            Column("labels", Text),
            Column("priority", String),
            Column("priority_pos", Integer),
            Column("created", DateTime),
            Column("resolved", DateTime, nullable=True),
            Column("updated", DateTime),
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
            Column("request_type", String),
            Column("issuetype", String),
            Column("deployment_type", String),
            Column("organizations", Text),
            Column("primary_organization", String),
            Column("resolution", String),
            Column("satisfaction", String),
            Column("satisfaction_ts", DateTime),
            Column("work_category", String),
            Column("sentiment", String),
        )
        self.jira_comments = Table(
            "jira_comments",
            self.meta,
            Column("id", String, primary_key=True),
            Column("ticket_id", String, ForeignKey("jira_tickets.id")),
            Column("issue_key", String),
            Column("author", String),
            Column("author_email", String),
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
            Column("author_email", String),
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
            Column("author_email", String),
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
            Column("start_assigned_user", String),
            Column("end_assigned_user", String),
            Column("start_assigned_user_email", String),
            Column("end_assigned_user_email", String),
            Column("start_request_type", String),
            Column("end_request_type", String),
            Column("start_issuetype", String),
            Column("end_issuetype", String),
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
        self.org_lookup = Table(
            "org_lookup",
            self.meta,
            Column("regex", String, primary_key=True),
            Column("primary_org", String),
            Column("priority", Integer),
        )
        self.week_reporting = Table(
            "week_reporting",
            self.meta,
            Column("calendar_week", String, primary_key=True),
            Column("reporting_month", String),
            Column("reporting_quarter", String),
        )
        self.meta.create_all(self.engine)

        # week to reporting month/quarter lookup, covering every year from the
        # oldest stored ticket to the current year; extended by insert_many
        self._reporting_years: set[int] = set()
        current_year = datetime.date.today().isocalendar().year
        first_year = min(self._first_data_year() or current_year, current_year)
        self._seed_week_reporting(range(first_year, current_year + 1))

        self._create_views()

    def _first_data_year(self) -> int | None:
        """Return the year of the oldest stored ticket or calendar day."""
        with self.engine.connect() as conn:
            values = [
                conn.execute(select(func.min(self.jira_tickets.c.created))).scalar(),
                conn.execute(select(func.min(self.jira_calendar.c.date))).scalar(),
            ]
        years = [int(str(v)[:4]) for v in values if v]
        return min(years) if years else None

    def _seed_week_reporting(self, years: Iterable[int]) -> None:
        """Add week_reporting rows for the given years; existing rows are kept."""
        years = [y for y in years if y not in self._reporting_years]
        if not years:
            return
        rows = []
        for year in years:
            weeks_in_year = datetime.date(year, 12, 28).isocalendar().week
            for week in range(1, weeks_in_year + 1):
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
                        "calendar_week": f"{year}.{week:02}",
                        "reporting_month": f"{year}.{month:02}",
                        "reporting_quarter": f"{year}.Q{quarter}",
                    }
                )
        with self.engine.begin() as conn:
            conn.execute(self.week_reporting.insert().prefix_with("OR IGNORE"), rows)
        self._reporting_years.update(years)

    def _category_issue_types(self) -> list[str]:
        """Issue types that get a category when no categories are configured."""
        if self.support_issue_types:
            return list(dict.fromkeys(self.support_issue_types))
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    select(self.jira_tickets.c.issuetype)
                    .where(self.jira_tickets.c.issuetype.is_not(None))
                    .distinct()
                    .order_by(self.jira_tickets.c.issuetype)
                ).scalars()
            )

    def _create_views(self) -> None:
        """Create the reporting views and add columns missing in older databases."""
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "reporting_weeks")
            conn.execute(text("CREATE VIEW reporting_weeks AS SELECT * FROM week_reporting"))

        def sql_str(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        def sql_status_list(statuses: Iterable[str]) -> str:
            # lower-cased for comparison with LOWER(status); SQLite accepts an empty list
            return ", ".join(sql_str(s.lower()) for s in statuses)

        cancelled = sql_status_list(self.cancelled_statuses)
        closed_statuses = sql_status_list(self.closed_statuses)

        def not_cancelled(status_col: str) -> str:
            return f"LOWER(COALESCE({status_col}, '')) NOT IN ({cancelled})"

        is_junk_expr = f"CASE WHEN LOWER(jt.status) IN ({cancelled}) THEN 1 ELSE 0 END"

        def category_condition(pattern: CategoryPattern) -> str:
            # a plain pattern matches the request type; a mapping ANDs its patterns
            if isinstance(pattern, str):
                pattern = {"request_type": pattern}
            return " AND ".join(
                f"jt.{CATEGORY_PATTERN_COLUMNS[key]} LIKE {sql_str(value)}"
                for key, value in pattern.items()
            )

        request_type_categories = self.request_type_categories
        if request_type_categories is None:
            request_type_categories = {
                issuetype: [{"issue_type": issuetype}]
                for issuetype in self._category_issue_types()
            }
        when_clauses = [
            f"WHEN {category_condition(pattern)} THEN {sql_str(category)}"
            for category, patterns in request_type_categories.items()
            for pattern in patterns
        ]
        default_category = (
            sql_str(self.request_type_category_default)
            if self.request_type_category_default
            else "NULL"
        )
        if when_clauses:
            request_type_expr = (
                "CASE " + " ".join(when_clauses) + f" ELSE {default_category} END"
            )
        else:
            request_type_expr = default_category

        # one open_<category> / closed_<category> column per category
        categories = list(request_type_categories)
        if (
            self.request_type_category_default
            and self.request_type_category_default not in categories
        ):
            categories.append(self.request_type_category_default)
        category_cols = [
            (sql_str(category), re.sub(r"\W+", "_", category.lower()).strip("_"))
            for category in categories
        ]
        active_category_cols = "".join(
            f",\n                        COUNT(DISTINCT CASE WHEN request_type_category = {value} THEN id END) AS open_{suffix}"
            for value, suffix in category_cols
        )
        closed_category_cols = "".join(
            f"                        COUNT(DISTINCT CASE WHEN request_type_category = {value} THEN id END) AS closed_{suffix},\n"
            for value, suffix in category_cols
        )
        summary_category_cols = "".join(
            f"                COALESCE(active.open_{suffix}, 0) AS open_{suffix},\n"
            for _, suffix in category_cols
        ) + "".join(
            f"                COALESCE(closed.closed_{suffix}, 0) AS closed_{suffix},\n"
            for _, suffix in category_cols
        )
        if self.non_support_request_types:
            excluded_values = ", ".join(
                "'" + value.replace("'", "''") + "'"
                for value in self.non_support_request_types
            )
            aged_request_type_filter = (
                " AND (jc.end_request_type IS NULL OR jc.end_request_type NOT IN ("
                + excluded_values
                + "))"
            )
            calendar_request_type_filter = (
                " AND (COALESCE(jt.request_type, jc.end_request_type) IS NULL"
                " OR COALESCE(jt.request_type, jc.end_request_type) NOT IN ("
                + excluded_values
                + "))"
            )
            tickets_request_type_filter = (
                " AND (jt.request_type IS NULL OR jt.request_type NOT IN ("
                + excluded_values
                + "))"
            )
            reporting_request_type_filter = (
                " AND (jtr.request_type IS NULL OR jtr.request_type NOT IN ("
                + excluded_values
                + "))"
            )
            active_request_type_filter = (
                " AND (jtaw.request_type IS NULL OR jtaw.request_type NOT IN ("
                + excluded_values
                + "))"
            )
        else:
            aged_request_type_filter = ""
            calendar_request_type_filter = ""
            tickets_request_type_filter = ""
            reporting_request_type_filter = ""
            active_request_type_filter = ""

        if self.support_issue_types:
            issue_type_values = ", ".join(
                "'" + value.lower().replace("'", "''") + "'"
                for value in self.support_issue_types
            )
            calendar_support_filter = (
                " AND LOWER(COALESCE(jt.issuetype, jc.end_issuetype, jc.start_issuetype, '')) IN ("
                + issue_type_values
                + ")"
            )
            tickets_support_filter = (
                " AND LOWER(COALESCE(jt.issuetype, '')) IN (" + issue_type_values + ")"
            )
            reporting_support_filter = (
                " AND LOWER(COALESCE(jtr.issuetype, '')) IN (" + issue_type_values + ")"
            )
            active_support_filter = (
                " AND LOWER(COALESCE(jtaw.issuetype, '')) IN (" + issue_type_values + ")"
            )
        else:
            calendar_support_filter = ""
            tickets_support_filter = ""
            reporting_support_filter = ""
            active_support_filter = ""

        # create a view exposing reporting month and quarter columns
        view_sql = f"""
            CREATE VIEW jira_tickets_reporting AS
            SELECT
                jt.*,
                {is_junk_expr} AS is_junk,
                {request_type_expr} AS request_type_category,
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
            self._drop_view_if_exists(conn, "jira_tickets_reporting")
            conn.execute(text(view_sql))

        worklog_view_sql = f"""
            CREATE VIEW jira_worklogs_reporting AS
            SELECT
                jw.*,
                {is_junk_expr} AS is_junk,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jw.started_week) AS started_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jw.started_week) AS started_reportingquarter,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jw.created_week) AS created_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jw.created_week) AS created_reportingquarter
            FROM jira_worklogs jw
            LEFT JOIN jira_tickets jt ON jt.id = jw.ticket_id
        """
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "jira_worklogs_reporting")
            conn.execute(text(worklog_view_sql))

        calendar_view_sql = f"""
            CREATE VIEW jira_calendar_reporting AS
            SELECT
                jc.record_id,
                jc.ticket_id,
                jc.ticket_key,
                jc.date,
                jc.comments,
                jc.public_comments,
                jc.start_assigned_user,
                jc.end_assigned_user,
                jc.start_assigned_user_email,
                jc.end_assigned_user_email,
                jc.start_request_type,
                jc.end_request_type,
                jc.start_issuetype,
                jc.end_issuetype,
                jc.did_escalate_priority,
                jc.did_escalate_cs,
                jc.did_escalate_management,
                jc.start_priority_txt,
                jc.start_priority_pos,
                jc.end_priority_txt,
                jc.end_priority_pos,
                jc.time_spent_min,
                jc.start_status,
                jc.end_status,
                jc.did_resolve,
                jc.total_days_open,
                jc.weekdays_days_since_last_contact,
                jc.adjusted_resolution_days,
                jc.adjusted_resolution_weekdays,
                {is_junk_expr} AS is_junk,
                jc.cw,
                jc.weekday,
                jc.date_modified,
                (SELECT reporting_month FROM week_reporting WHERE calendar_week = jc.cw) AS cw_reportingmonth,
                (SELECT reporting_quarter FROM week_reporting WHERE calendar_week = jc.cw) AS cw_reportingquarter
            FROM jira_calendar jc
            LEFT JOIN jira_tickets jt ON jc.ticket_id = jt.id
        """
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "jira_calendar_reporting")
            conn.execute(text(calendar_view_sql))

        activeweeks_view_sql = f"""
            CREATE VIEW jira_tickets_active_weeks AS
            SELECT
                jt.*,
                {request_type_expr} AS request_type_category,
                {is_junk_expr} AS is_junk,
                aw.value AS active_week,
                wr.reporting_month AS active_reportingmonth,
                wr.reporting_quarter AS active_reportingquarter
            FROM jira_tickets jt
            JOIN json_each('["' || replace(jt.active_weeks, ',', '","') || '"]') aw
                ON 1=1
            LEFT JOIN week_reporting wr ON wr.calendar_week = aw.value
        """
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "jira_tickets_active_weeks")
            conn.execute(text(activeweeks_view_sql))

        ticket_worklogs_view_sql = f"""
            CREATE VIEW jira_tickets_worklogs AS
            SELECT
                jt.*,
                {request_type_expr} AS request_type_category,
                {is_junk_expr} AS is_junk,
                jw.*
            FROM jira_tickets jt
            LEFT JOIN jira_worklogs jw ON jt.id = jw.ticket_id
        """
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "jira_tickets_worklogs")
            conn.execute(text(ticket_worklogs_view_sql))

        # per customer, week and request type category, counted like the summary views
        customer_view_sql = f"""
            CREATE VIEW customer_weekly_summary AS
            WITH
                support_tickets AS (
                    SELECT
                        jt.id,
                        jt.created_week,
                        jt.resolved_week,
                        COALESCE(NULLIF(jt.primary_organization, ''), '(none)') AS organization,
                        COALESCE({request_type_expr}, '(none)') AS request_type_category
                    FROM jira_tickets jt
                    WHERE {not_cancelled("jt.status")}
                      {tickets_request_type_filter}
                      {tickets_support_filter}
                ),
                created AS (
                    SELECT created_week AS week, organization, request_type_category,
                           COUNT(*) AS new_tickets
                    FROM support_tickets
                    GROUP BY created_week, organization, request_type_category
                ),
                resolved AS (
                    SELECT resolved_week AS week, organization, request_type_category,
                           COUNT(*) AS closed_tickets
                    FROM support_tickets
                    WHERE resolved_week IS NOT NULL
                    GROUP BY resolved_week, organization, request_type_category
                ),
                logged AS (
                    SELECT jw.started_week AS week, st.organization, st.request_type_category,
                           SUM(jw.time_spent_minutes) / 60.0 AS hours_logged
                    FROM jira_worklogs jw
                    JOIN support_tickets st ON st.id = jw.ticket_id
                    GROUP BY jw.started_week, st.organization, st.request_type_category
                ),
                keys AS (
                    SELECT week, organization, request_type_category FROM created
                    UNION SELECT week, organization, request_type_category FROM resolved
                    UNION SELECT week, organization, request_type_category FROM logged
                )
            SELECT
                k.week,
                wr.reporting_month AS month,
                wr.reporting_quarter AS quarter,
                k.organization,
                k.request_type_category,
                COALESCE(c.new_tickets, 0) AS new_tickets,
                COALESCE(r.closed_tickets, 0) AS closed_tickets,
                COALESCE(l.hours_logged, 0) AS hours_logged
            FROM keys k
            JOIN week_reporting wr ON wr.calendar_week = k.week
            LEFT JOIN created c USING (week, organization, request_type_category)
            LEFT JOIN resolved r USING (week, organization, request_type_category)
            LEFT JOIN logged l USING (week, organization, request_type_category)
            ORDER BY k.week, k.organization, k.request_type_category
        """
        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "customer_weekly_summary")
            conn.execute(text(customer_view_sql))

        statuses = sql_status_list(self.no_contact_ignore_statuses)

        summary_view_template = """
            CREATE VIEW {view_name} AS
            WITH
                active AS (
                    SELECT
                        wr.{period_col} AS period,
                        COUNT(DISTINCT id) AS ongoing{active_category_cols}
                    FROM jira_tickets_active_weeks jtaw
                    JOIN week_reporting wr ON wr.calendar_week = active_week
                    WHERE {not_cancelled_jtaw}
                      {active_request_type_filter}
                      {active_support_filter}
                    GROUP BY wr.{period_col}
                ),
                friday_week AS (
                    SELECT
                        jc.cw AS cw,
                        COUNT(DISTINCT jc.ticket_id) AS overall_open,
                        COUNT(DISTINCT CASE
                            WHEN jc.total_days_open > {aged_days_short} THEN jc.ticket_id
                        END) AS aged_5,
                        COUNT(DISTINCT CASE
                            WHEN jc.total_days_open > {aged_days_long} THEN jc.ticket_id
                        END) AS aged_30,
                        COUNT(DISTINCT CASE WHEN jc.end_assigned_user IS NULL OR jc.end_assigned_user = '' THEN jc.ticket_id END) AS unassigned,
                        COUNT(DISTINCT CASE WHEN LOWER(jc.end_status) NOT IN ({statuses}) AND jc.weekdays_days_since_last_contact > {no_contact_days} THEN jc.ticket_id END) AS over5_no_contact
                    FROM jira_calendar jc
                    JOIN jira_tickets jt ON jt.id = jc.ticket_id
                    WHERE jc.weekday = 5
                      AND LOWER(jc.end_status) NOT IN ({closed_statuses})
                      AND {not_cancelled_jt}
                      AND jc.is_junk != 1
                      {calendar_request_type_filter}
                      {calendar_support_filter}
                    GROUP BY jc.cw
                ),
                friday AS (
                    SELECT
                        wr.{period_col} AS period,
                        AVG(fw.overall_open) AS overall_open,
                        ({aged_30_subquery}) AS aged_30,
                        ({aged_5_subquery}) AS aged_5,
                        AVG(fw.unassigned) AS unassigned,
                        AVG(fw.over5_no_contact) AS over5_no_contact
                    FROM friday_week fw
                    JOIN week_reporting wr ON wr.calendar_week = fw.cw
                    GROUP BY wr.{period_col}
                ),
                highprio AS (
                    SELECT wr.{period_col} AS period, COUNT(DISTINCT jc.ticket_id) AS high_critical
                    FROM jira_calendar jc
                    JOIN jira_tickets jt ON jt.id = jc.ticket_id
                    JOIN week_reporting wr ON wr.calendar_week = jc.cw
                    WHERE jc.end_priority_pos > 0
                      AND {not_cancelled_jt}
                      AND jc.is_junk != 1
                      {calendar_request_type_filter}
                      {calendar_support_filter}
                    GROUP BY wr.{period_col}
                ),
                escalated AS (
                    SELECT wr.{period_col} AS period, COUNT(DISTINCT jc.ticket_id) AS escalated
                    FROM jira_calendar jc
                    JOIN jira_tickets jt ON jt.id = jc.ticket_id
                    JOIN week_reporting wr ON wr.calendar_week = jc.cw
                    WHERE jc.did_escalate_priority = 1
                      AND jc.end_priority_pos > jc.start_priority_pos
                      AND {not_cancelled_jt}
                      AND jc.is_junk != 1
                      {calendar_request_type_filter}
                      {calendar_support_filter}
                    GROUP BY wr.{period_col}
                ),
                newt AS (
                    SELECT wr.{period_col} AS period, COUNT(*) AS new_tickets
                    FROM jira_tickets jt
                    JOIN week_reporting wr ON wr.calendar_week = jt.created_week
                    WHERE {not_cancelled_jt}
                      {tickets_request_type_filter}
                      {tickets_support_filter}
                    GROUP BY wr.{period_col}
                ),
                closed AS (
                    SELECT
                        wr.{period_col} AS period,
                        COUNT(*) AS closed_tickets,
{closed_category_cols}                        AVG(resolution_time_days) AS avg_resolution_days,
                        AVG(CAST(satisfaction AS REAL)) AS csat_score,
                        COUNT(satisfaction) AS csat_answers,
                        COUNT(CASE WHEN satisfaction = '1' THEN 1 END) AS csat_1,
                        COUNT(CASE WHEN satisfaction = '2' THEN 1 END) AS csat_2,
                        COUNT(CASE WHEN satisfaction = '3' THEN 1 END) AS csat_3,
                        COUNT(CASE WHEN satisfaction = '4' THEN 1 END) AS csat_4,
                        COUNT(CASE WHEN satisfaction = '5' THEN 1 END) AS csat_5
                    FROM jira_tickets_reporting jtr
                    JOIN week_reporting wr ON wr.calendar_week = jtr.resolved_week
                    WHERE jtr.resolved_week IS NOT NULL
                      AND {not_cancelled_jtr}
                      {reporting_request_type_filter}
                      {reporting_support_filter}
                    GROUP BY wr.{period_col}
                ),
                median_fr AS (
                    SELECT period, AVG(first_response_time_minutes) AS median_first_reply
                    FROM (
                        SELECT
                            wr.{period_col} AS period,
                            jt.first_response_time_minutes,
                            ROW_NUMBER() OVER (PARTITION BY wr.{period_col} ORDER BY jt.first_response_time_minutes) AS rn,
                            COUNT(*) OVER (PARTITION BY wr.{period_col}) AS cnt
                        FROM jira_tickets jt
                        JOIN week_reporting wr ON wr.calendar_week = jt.first_response_date_week
                        WHERE jt.first_response_time_minutes IS NOT NULL
                          AND {not_cancelled_jt}
                          {tickets_request_type_filter}
                          {tickets_support_filter}
                    ) t
                    WHERE rn IN ((cnt + 1)/2, (cnt + 2)/2)
                    GROUP BY period
                )
            SELECT
                {select_cols}
                COALESCE(active.ongoing, 0) AS ongoing_tickets,
                COALESCE(friday.overall_open, 0) AS fri_open_tickets,
                COALESCE(highprio.high_critical, 0) AS high_or_critical_tickets,
                COALESCE(escalated.escalated, 0) AS escalated_tickets,
                COALESCE(newt.new_tickets, 0) AS new_tickets,
                COALESCE(closed.closed_tickets, 0) AS closed_tickets,
                median_fr.median_first_reply AS median_first_reply_minutes,
                closed.avg_resolution_days AS avg_resolution_days,
                CASE WHEN newt.new_tickets IS NULL OR newt.new_tickets = 0 THEN NULL
                     ELSE ROUND(1.0 * closed.closed_tickets / newt.new_tickets, 2)
                END AS close_rate,
                closed.csat_score AS csat_score,
                COALESCE(closed.csat_answers, 0) AS csat_answers,
                COALESCE(closed.csat_1, 0) AS csat_1,
                COALESCE(closed.csat_2, 0) AS csat_2,
                COALESCE(closed.csat_3, 0) AS csat_3,
                COALESCE(closed.csat_4, 0) AS csat_4,
                COALESCE(closed.csat_5, 0) AS csat_5,
{summary_category_cols}                COALESCE(friday.aged_30, 0) AS aged_30_plus,
                COALESCE(friday.aged_5, 0) AS aged_5_plus,
                COALESCE(friday.overall_open, 0) AS overall_open,
                COALESCE(friday.unassigned, 0) AS unassigned,
                COALESCE(friday.over5_no_contact, 0) AS over5_days_no_contact
            FROM week_reporting wr
            LEFT JOIN active ON active.period = wr.{period_col}
            LEFT JOIN friday ON friday.period = wr.{period_col}
            LEFT JOIN highprio ON highprio.period = wr.{period_col}
            LEFT JOIN escalated ON escalated.period = wr.{period_col}
            LEFT JOIN newt ON newt.period = wr.{period_col}
            LEFT JOIN closed ON closed.period = wr.{period_col}
            LEFT JOIN median_fr ON median_fr.period = wr.{period_col}
            GROUP BY wr.{period_col}
            ORDER BY wr.{period_col}
        """

        def build_summary_view_sql(view_name: str, period_col: str, select_cols: str) -> str:
            aged_30_subquery = f"""
                SELECT fw2.aged_30
                FROM friday_week fw2
                JOIN week_reporting wr2 ON wr2.calendar_week = fw2.cw
                WHERE wr2.{period_col} = wr.{period_col}
                ORDER BY fw2.cw DESC
                LIMIT 1
            """
            aged_5_subquery = f"""
                SELECT fw2.aged_5
                FROM friday_week fw2
                JOIN week_reporting wr2 ON wr2.calendar_week = fw2.cw
                WHERE wr2.{period_col} = wr.{period_col}
                ORDER BY fw2.cw DESC
                LIMIT 1
            """

            return summary_view_template.format(
                view_name=view_name,
                period_col=period_col,
                select_cols=select_cols,
                statuses=statuses,
                aged_days_short=self.aged_days_short,
                aged_days_long=self.aged_days_long,
                no_contact_days=self.no_contact_days,
                closed_statuses=closed_statuses,
                not_cancelled_jt=not_cancelled("jt.status"),
                not_cancelled_jtr=not_cancelled("jtr.status"),
                not_cancelled_jtaw=not_cancelled("status"),
                aged_30_subquery=aged_30_subquery,
                aged_5_subquery=aged_5_subquery,
                aged_request_type_filter=aged_request_type_filter,
                calendar_request_type_filter=calendar_request_type_filter,
                calendar_support_filter=calendar_support_filter,
                tickets_request_type_filter=tickets_request_type_filter,
                tickets_support_filter=tickets_support_filter,
                reporting_request_type_filter=reporting_request_type_filter,
                reporting_support_filter=reporting_support_filter,
                active_request_type_filter=active_request_type_filter,
                active_support_filter=active_support_filter,
                active_category_cols=active_category_cols,
                closed_category_cols=closed_category_cols,
                summary_category_cols=summary_category_cols,
            )

        weekly_report_view_sql = build_summary_view_sql(
            "weekly_support_summary",
            "calendar_week",
            "wr.calendar_week AS week,\n                wr.reporting_month AS month,\n                wr.reporting_quarter AS quarter,",
        )

        monthly_report_view_sql = build_summary_view_sql(
            "monthly_support_summary",
            "reporting_month",
            "wr.reporting_month AS month,\n                wr.reporting_quarter AS quarter,",
        )

        quarterly_report_view_sql = build_summary_view_sql(
            "quarterly_support_summary",
            "reporting_quarter",
            "wr.reporting_quarter AS quarter,",
        )

        with self.engine.begin() as conn:
            self._drop_view_if_exists(conn, "weekly_support_summary")
            self._drop_view_if_exists(conn, "monthly_support_summary")
            self._drop_view_if_exists(conn, "quarterly_support_summary")
            conn.execute(text(weekly_report_view_sql))
            conn.execute(text(monthly_report_view_sql))
            conn.execute(text(quarterly_report_view_sql))

        inspector = sa_inspect(self.engine)
        cols = [c["name"] for c in inspector.get_columns("jira_tickets")]
        if "updated" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN updated DATETIME"))
        if "assignee" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN assignee VARCHAR"))
        if "assignee_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN assignee_email VARCHAR"))
        if "creator" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN creator VARCHAR"))
        if "creator_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN creator_email VARCHAR"))
        if "request_participants" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN request_participants TEXT"))
        if "labels" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN labels TEXT"))
        if "priority" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN priority VARCHAR"))
        if "priority_pos" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN priority_pos INTEGER"))
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
        if "request_type" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN request_type VARCHAR"))
        if "issuetype" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN issuetype VARCHAR"))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE jira_tickets SET issuetype = 'Support' WHERE issuetype IS NULL"))
        if "deployment_type" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN deployment_type VARCHAR"))
        if "organizations" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN organizations TEXT"))
        if "primary_organization" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN primary_organization VARCHAR"))
        if "resolution" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN resolution VARCHAR"))
        if "satisfaction" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN satisfaction VARCHAR"))
        if "satisfaction_ts" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN satisfaction_ts DATETIME"))
        if "work_category" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN work_category VARCHAR"))
        if "sentiment" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_tickets ADD COLUMN sentiment VARCHAR"))

        cols = [c["name"] for c in inspector.get_columns("jira_calendar")]
        if "start_assigned_user_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN start_assigned_user_email VARCHAR"))
        if "end_assigned_user_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN end_assigned_user_email VARCHAR"))
        if "start_request_type" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN start_request_type VARCHAR"))
        if "end_request_type" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN end_request_type VARCHAR"))
        if "start_issuetype" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN start_issuetype VARCHAR"))
        if "end_issuetype" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_calendar ADD COLUMN end_issuetype VARCHAR"))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE jira_calendar SET start_issuetype = 'Support' WHERE start_issuetype IS NULL"))
            conn.execute(text("UPDATE jira_calendar SET end_issuetype = 'Support' WHERE end_issuetype IS NULL"))

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
        if "author_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_comments ADD COLUMN author_email VARCHAR"))

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
        if "author_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_worklogs ADD COLUMN author_email VARCHAR"))

        cols = [c["name"] for c in inspector.get_columns("jira_changelog")]
        if "author_email" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE jira_changelog ADD COLUMN author_email VARCHAR"))

    @log_call
    def insert_many(self, table: Table, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        if table is self.jira_tickets or table is self.jira_calendar:
            self._extend_week_reporting(rows)
        with self.engine.begin() as conn:
            conn.execute(table.insert().prefix_with("OR REPLACE"), rows)

    def _extend_week_reporting(self, rows: list[dict[str, Any]]) -> None:
        """Seed week_reporting so it spans the years of the inserted rows."""
        years = {
            value.isocalendar().year
            for row in rows
            for value in (row.get("created"), row.get("date"))
            if isinstance(value, datetime.date)
        }
        if not years or years <= self._reporting_years:
            return
        known = years | self._reporting_years
        self._seed_week_reporting(range(min(known), max(known) + 1))

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
    def load_internal_patterns(self) -> list[Pattern[str]]:
        inspector = sa_inspect(self.engine)
        if "internal_users" not in inspector.get_table_names():
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.internal_users.c.pattern)).fetchall()
        return [re.compile(r[0], re.IGNORECASE) for r in rows]

    @log_call
    def load_org_patterns(self) -> list[tuple[Pattern[str], str, int]]:
        inspector = sa_inspect(self.engine)
        if "org_lookup" not in inspector.get_table_names():
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.org_lookup.c.regex,
                                       self.org_lookup.c.primary_org,
                                       self.org_lookup.c.priority)).fetchall()
        compiled: list[tuple[Pattern[str], str, int]] = []
        for regex, primary, prio in rows:
            try:
                pat = re.compile(regex, re.IGNORECASE)
            except re.error:
                continue
            compiled.append((pat, primary, int(prio or 0)))
        return compiled

    @log_call
    def get_ticket_with_related(self, ticket_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Load a ticket and related records from the database."""
        with self.engine.connect() as conn:
            t = (
                conn.execute(
                    select(self.jira_tickets).where(self.jira_tickets.c.id == ticket_id)
                )
                .mappings()
                .fetchone()
            )
            if not t:
                return {}, [], [], []
            comments = (
                conn.execute(
                    select(self.jira_comments).where(self.jira_comments.c.ticket_id == ticket_id)
                )
                .mappings()
                .fetchall()
            )
            worklogs = (
                conn.execute(
                    select(self.jira_worklogs).where(self.jira_worklogs.c.ticket_id == ticket_id)
                )
                .mappings()
                .fetchall()
            )
            changelog_rows = (
                conn.execute(
                    select(self.jira_changelog).where(self.jira_changelog.c.ticket_id == ticket_id)
                )
                .mappings()
                .fetchall()
            )

        changelog: list[dict[str, Any]] = []
        histories: dict[str, dict[str, Any]] = {}
        for row in changelog_rows:
            r = dict(row)
            hid = r.get("history_id")
            if hid not in histories:
                histories[hid] = {
                    "id": hid,
                    "author": r.get("author"),
                    "author_email": r.get("author_email"),
                    "created": r.get("created"),
                    "items": [],
                }
            histories[hid]["items"].append(
                {
                    "field": r.get("field"),
                    "fieldtype": r.get("fieldtype"),
                    "from": r.get("from_value"),
                    "fromString": r.get("from_string"),
                    "to": r.get("to_value"),
                    "toString": r.get("to_string"),
                }
            )
        changelog = list(histories.values())

        return (
            dict(t),
            [dict(r) for r in comments],
            [dict(r) for r in worklogs],
            changelog,
        )


class SupportStatsGetter:
    """Retrieve Jira ticket data and store it in the configured database."""

    @log_call
    def __init__(self, config: Config):
        self.config = config
        engine = create_engine(f"sqlite+pysqlite:///{config.db_path}")
        self.store = DatabaseStore(
            engine,
            non_support_request_types=config.non_support_request_types,
            support_issue_types=config.support_issue_types,
            request_type_categories=config.request_type_categories,
            request_type_category_default=config.request_type_category_default,
            closed_statuses=config.closed_statuses,
            cancelled_statuses=config.cancelled_statuses,
            no_contact_ignore_statuses=config.no_contact_ignore_statuses,
            aged_days_short=config.aged_days_short,
            aged_days_long=config.aged_days_long,
            no_contact_days=config.no_contact_days,
        )
        global CLOSED_STATUSES, CANCELLED_STATUSES, RESOLUTION_TIME_EXCLUDED_STATUSES
        CLOSED_STATUSES = {s.lower() for s in config.closed_statuses}
        CANCELLED_STATUSES = {s.lower() for s in config.cancelled_statuses}
        RESOLUTION_TIME_EXCLUDED_STATUSES = {
            s.lower() for s in config.resolution_time_excluded_statuses
        }
        global INTERNAL_EMAIL_PATTERNS
        INTERNAL_EMAIL_PATTERNS = self.store.load_internal_patterns()
        global ORG_PATTERNS
        ORG_PATTERNS = self.store.load_org_patterns()
        global PRIORITY_POS_MAP
        PRIORITY_POS_MAP = dict(config.priority_positions)

    def _field_value(
        self,
        fields: Mapping[str, Any],
        logical: str,
        fallbacks: Iterable[str] | None = None,
    ) -> Any:
        """Return the value for a logical field name respecting custom mapping."""
        name = self.config.custom_fields.get(logical)
        if name and name in fields:
            return fields.get(name)
        fallbacks = fallbacks or []
        for key in fallbacks:
            if key in fields:
                return fields.get(key)
        return None

    def _jira_auth(self) -> tuple[str, str] | None:
        if self.config.jira_user and self.config.jira_token:
            return (self.config.jira_user, self.config.jira_token)
        return None

    @log_call
    def latest_update_timestamp(self) -> datetime.datetime | None:
        return self.store.newest_updated_ts()

    @log_call
    def export_views_to_excel(
        self,
        output_file: Path,
        mode: Literal["overwrite_with_backup", "overwrite", "no_overwrite"] = "overwrite_with_backup",
    ) -> None:
        """Write all database views into an Excel workbook.

        Parameters
        ----------
        output_file:
            Target workbook path.
        mode:
            Behaviour when the target file already exists.
        """
        if (
            self.store.request_type_categories is None
            and not self.store.support_issue_types
        ):
            # one category per stored issue type: pick up types fetched in this run
            self.store._create_views()
        inspector = sa_inspect(self.store.engine)
        views = inspector.get_view_names()
        output_file = Path(output_file)

        if mode == "overwrite_with_backup":
            if output_file.exists():
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = output_file.with_name(
                    f"{output_file.stem}_{timestamp}{output_file.suffix}"
                )
                shutil.copy2(output_file, backup)
            wb = Workbook()
            wb.remove(wb.active)
        elif mode == "overwrite":
            wb = Workbook()
            wb.remove(wb.active)
        elif mode == "no_overwrite":
            if output_file.exists():
                raise FileExistsError(output_file)
            wb = Workbook()
            wb.remove(wb.active)
        else:
            raise ValueError(f"Invalid mode: {mode}")
        total_views = len(views)
        logging.info("Exporting %d views to Excel workbook", total_views)

        def sanitize_value(v: Any) -> Any:
            if isinstance(v, str):
                v = ILLEGAL_CHARACTERS_RE.sub("", v)
                if v.startswith("="):
                    v = f"'{v}"
            return v

        exported: dict[str, tuple[str, pd.DataFrame]] = {}
        for idx, view in enumerate(views, start=1):
            logging.info("Writing view %d/%d: %s", idx, total_views, view)
            df = pd.read_sql_query(f'SELECT * FROM "{view}"', self.store.engine)
            for col in df.columns:
                if not re.search(r"(week|month|quarter|cw)", col, re.IGNORECASE):
                    try:
                        df[col] = pd.to_numeric(df[col])
                    except (ValueError, TypeError):
                        pass
                df[col] = df[col].apply(sanitize_value)
            safe_name = re.sub(r'[/\\*?:\[\]]', '_', view)[:31]
            exported[view] = (safe_name, df)
            if safe_name in wb.sheetnames:
                ws = wb[safe_name]
                if ws.tables:
                    table = next(iter(ws.tables.values()))
                    start_cell, old_end = table.ref.split(":")
                    header_row = ws[start_cell].row
                    first_row = header_row + 1
                    start_col = ws[start_cell].column
                    row_idx = first_row
                    for data_row in dataframe_to_rows(df, index=False, header=False):
                        for col_idx, value in enumerate(data_row, start=start_col):
                            ws.cell(row=row_idx, column=col_idx, value=sanitize_value(value))
                        row_idx += 1
                    old_last_row = ws[old_end].row
                    num_cols = len(df.columns)
                    for r in range(row_idx, old_last_row + 1):
                        for c in range(start_col, start_col + num_cols):
                            ws.cell(row=r, column=c, value=None)
                    end_col = get_column_letter(start_col + num_cols - 1)
                    table.ref = f"{start_cell}:{end_col}{row_idx - 1}"
                else:
                    ws.delete_rows(1, ws.max_row)
                    for row in dataframe_to_rows(df, index=False, header=True):
                        ws.append([sanitize_value(v) for v in row])
            else:
                ws = wb.create_sheet(title=safe_name)
                for row in dataframe_to_rows(df, index=False, header=True):
                    ws.append([sanitize_value(v) for v in row])

        self._write_dashboard(wb, exported)
        wb.save(output_file)
        logging.info("Finished exporting Excel workbook to %s", output_file)

    def _write_dashboard(
        self,
        wb: Workbook,
        exported: Mapping[str, tuple[str, pd.DataFrame]],
    ) -> None:
        """Add a formula-driven KPI dashboard in front of the exported view sheets.

        The dashboard shows the summary views from a selectable start quarter up
        to the current period. Start quarter, granularity and whether the
        current (incomplete) period is included are input cells; tables and
        charts follow them through formulas and OFFSET-based names, so no pivot
        tables or refresh are needed.
        """
        period_views = {
            "weekly_support_summary": "week",
            "monthly_support_summary": "month",
            "quarterly_support_summary": "quarter",
        }
        if any(v not in exported or exported[v][1].empty for v in period_views):
            logging.info("Summary views missing or empty, skipping dashboard")
            return
        weekly = exported["weekly_support_summary"][1]

        def rng(view: str, col: str) -> str:
            sheet, df = exported[view]
            letter = get_column_letter(df.columns.get_loc(col) + 1)
            return f"'{sheet}'!${letter}$2:${letter}${len(df) + 1}"

        def choose(cols: Mapping[str, str]) -> str:
            ranges = ",".join(rng(view, cols[view]) for view in period_views)
            return f"CHOOSE(dashboard_calc!$B$5,{ranges})"

        def in_all_views(col: str) -> bool:
            return all(col in exported[v][1].columns for v in period_views)

        # "now" is the export date, mapped to its reporting month and quarter
        today = datetime.date.today()
        today_week = cw_string_from_datetime(datetime.datetime.combine(today, datetime.time()))
        weeks = [str(w) for w in weekly["week"]]
        now_week = max((w for w in weeks if w <= today_week), default=weeks[0])
        now_row = weekly[weekly["week"] == now_week].iloc[0]
        now_month, now_quarter = str(now_row["month"]), str(now_row["quarter"])
        quarters = [
            str(q) for q in exported["quarterly_support_summary"][1]["quarter"]
            if str(q) <= now_quarter
        ]
        start_quarter = self.config.dashboard_start_quarter
        if start_quarter not in quarters:
            start_quarter = quarters[max(0, quarters.index(now_quarter) - 4)]

        period_by_view = dict(period_views)
        quarter_by_view = {v: "quarter" for v in period_views}

        # hidden helper sheet with the window calculation and dropdown lists
        calc = wb.create_sheet("dashboard_calc")
        calc.sheet_state = "hidden"
        calc_rows = [
            ("now_week", now_week),
            ("now_month", now_month),
            ("now_quarter", now_quarter),
            ("start_quarter", '=""&Dashboard!$C$4'),
            ("granularity", '=MATCH(Dashboard!$C$5,{"Week","Month","Quarter"},0)'),
            ("first_row", f"=MATCH(B4,{choose(quarter_by_view)},0)"),
            (
                "last_row",
                f"=MATCH(CHOOSE(B5,B1,B2,B3),{choose(period_by_view)},0)"
                '-IF(Dashboard!$C$6="Yes",0,1)',
            ),
            ("periods", "=IFERROR(MAX(0,B7-B6+1),0)"),
            (
                "last_full_period",
                f"=IFERROR(INDEX({choose(period_by_view)},"
                f'MATCH(CHOOSE(B5,B1,B2,B3),{choose(period_by_view)},0)-1),"")',
            ),
            ("first_period", f'=IF(B8>0,INDEX({choose(period_by_view)},B6),"")'),
            ("last_period", f'=IF(B8>0,INDEX({choose(period_by_view)},B6+B8-1),"")'),
        ]
        for row, (label, value) in enumerate(calc_rows, start=1):
            calc.cell(row=row, column=1, value=label)
            calc.cell(row=row, column=2, value=value)
        calc["D1"] = "quarters"
        for row, quarter in enumerate(quarters, start=2):
            calc.cell(row=row, column=4, value=quarter)

        dash = wb.create_sheet("Dashboard")
        wb.move_sheet(dash, offset=-wb.index(dash))
        for ws in wb.worksheets:
            ws.sheet_view.tabSelected = ws is dash
        wb.active = 0
        wb.calculation.fullCalcOnLoad = True

        input_fill = PatternFill("solid", fgColor="FFF2CC")
        header_fill = PatternFill("solid", fgColor="DDEBF7")
        thin = Side(style="thin", color="A6A6A6")
        box = Border(left=thin, right=thin, top=thin, bottom=thin)

        dash["A1"] = "Support KPI dashboard"
        dash["A1"].font = Font(bold=True, size=16)
        dash["A2"] = (
            f"Data as of {today.isoformat()} (week {now_week}). Change the yellow "
            "cells; tables and charts follow automatically."
        )
        dash["A2"].font = Font(italic=True, color="595959")
        controls = [
            ("Start quarter", start_quarter, f"dashboard_calc!$D$2:$D${len(quarters) + 1}"),
            ("Granularity", "Week", '"Week,Month,Quarter"'),
            # the current period is incomplete and would pull every chart down at the end
            ("Include current period", "No", '"Yes,No"'),
        ]
        for row, (label, value, options) in enumerate(controls, start=4):
            dash.cell(row=row, column=1, value=label).font = Font(bold=True)
            cell = dash.cell(row=row, column=3, value=value)
            cell.fill, cell.border, cell.font = input_fill, box, Font(bold=True)
            dv = DataValidation(type="list", formula1=options, allow_blank=False)
            dv.add(cell)
            dash.add_data_validation(dv)
        dash["E4"] = "Showing:"
        dash["E4"].font = Font(bold=True)
        dash["F4"] = (
            '=IF(dashboard_calc!$B$8>0,dashboard_calc!$B$10&" to "&dashboard_calc!$B$11'
            '&" ("&dashboard_calc!$B$8&" periods)","no data for this selection")'
        )

        # KPI table columns: period, metrics, per-category active/closed
        thresholds = {
            "aged_days_short": self.config.aged_days_short,
            "aged_days_long": self.config.aged_days_long,
            "no_contact_days": self.config.no_contact_days,
        }
        columns: list[tuple[str, str, str]] = [
            (col, label.format(**thresholds), fmt)
            for col, label, fmt in DASHBOARD_METRICS
            if in_all_views(col)
        ]
        for prefix, label in (("open_", "Active"), ("closed_", "Closed")):
            columns += [
                (col, f"{label} {col[len(prefix):].upper()}", "0")
                for col in weekly.columns
                if col.startswith(prefix) and col != "closed_tickets" and in_all_views(col)
            ]

        charts = []
        for title, kind, wanted in DASHBOARD_CHARTS:
            cols = []
            for want in wanted:
                if want.endswith("*"):
                    cols += [
                        c for c, _, _ in columns
                        if c.startswith(want[:-1]) and c != "closed_tickets"
                    ]
                elif any(c == want for c, _, _ in columns):
                    cols.append(want)
            if cols:
                charts.append((title, kind, cols))

        customers = exported.get("customer_weekly_summary")
        has_customers = customers is not None and not customers[1].empty
        # per-customer measures: (chart title or None, table header, column, category)
        cust_measures: list[tuple[str | None, str, str, str | None]] = []
        if has_customers:
            cats = sorted({str(c) for c in customers[1]["request_type_category"]})
            cust_measures.append(("New tickets per customer", "New", "new_tickets", None))
            if len(cats) > 1:
                cust_measures += [
                    (f"New {cat} tickets per customer", f"New {cat}", "new_tickets", cat)
                    for cat in cats
                ]
            cust_measures += [
                (None, "Closed", "closed_tickets", None),
                ("Hours logged per customer", "Hours logged", "hours_logged", None),
            ]
        cust_charts = [m for m in cust_measures if m[0]]
        cust_row = 8 + 15 * ((len(charts) + 1) // 2)
        cust_table_row = cust_row + 1 + 15 * ((len(cust_charts) + 1) // 2)
        table_row = (
            cust_table_row + DASHBOARD_TOP_CUSTOMERS + 3 if has_customers else cust_row
        )
        first = table_row + 1
        max_rows = len(weekly)

        dash.column_dimensions["A"].width = 22 if has_customers else 12
        for idx in range(2, len(columns) + 2):
            dash.column_dimensions[get_column_letter(idx)].width = 11

        dash.cell(row=table_row, column=1, value="Period")
        for idx, (col, label, fmt) in enumerate(columns, start=2):
            dash.cell(row=table_row, column=idx, value=label)
        for idx in range(1, len(columns) + 2):
            cell = dash.cell(row=table_row, column=idx)
            cell.font, cell.fill, cell.border = Font(bold=True), header_fill, box
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        dash.row_dimensions[table_row].height = 30

        sources = [("period", period_by_view, "@", False)] + [
            (col, {v: col for v in period_views}, fmt, col in DASHBOARD_NULLABLE_METRICS)
            for col, _, fmt in columns
        ]
        for idx, (_, cols, fmt, nullable) in enumerate(sources, start=1):
            src = choose(cols)
            for k in range(1, max_rows + 1):
                value = f"INDEX({src},dashboard_calc!$B$6+{k - 1})"
                if nullable:
                    value = f'IF({value}="",NA(),{value})'
                cell = dash.cell(
                    row=first + k - 1,
                    column=idx,
                    value=f'=IF({k}>dashboard_calc!$B$8,"",{value})',
                )
                cell.number_format = fmt
        last_col = get_column_letter(len(columns) + 1)
        dash.conditional_formatting.add(
            f"A{first}:{last_col}{first + max_rows - 1}",
            FormulaRule(formula=[f"ISNA(A{first})"], font=Font(color="BFBFBF")),
        )

        def add_name(name: str, column: int, top: int, count: str, sheet: str = "Dashboard") -> str:
            letter = get_column_letter(column)
            dash.defined_names[name] = DefinedName(
                name, attr_text=f"OFFSET({sheet}!${letter}${top},0,0,MAX(1,{count}),1)"
            )
            return f"Dashboard!{name}"

        refs = {
            col: add_name(f"Dash_{col}", idx, first, "dashboard_calc!$B$8")
            for idx, (col, *_) in enumerate(sources, start=1)
        }
        labels = {col: label for col, label, _ in columns}
        formats = {col: fmt for col, _, fmt in columns}

        def series(values: str, categories: str, title: str) -> Series:
            s = Series()
            s.val = NumDataSource(numRef=NumRef(f=values))
            s.cat = AxDataSource(strRef=StrRef(f=categories))
            s.tx = SeriesLabel(v=title)
            s.smooth = False
            return s

        def new_chart(kind: str, title: str):
            if kind == "line":
                chart = LineChart()
            else:
                chart = BarChart()
                chart.type = "bar" if kind == "bar" else "col"
                chart.gapWidth = 60
                if kind == "stacked":
                    chart.grouping, chart.overlap = "stacked", 100
            chart.varyColors = False
            chart.title = title
            chart.title.overlay = False
            chart.width, chart.height = 16, 7.5
            chart.x_axis.delete = chart.y_axis.delete = False
            chart.y_axis.number_format = "General"
            chart.legend.position = "b"
            chart.legend.overlay = False
            return chart

        for idx, (title, kind, cols) in enumerate(charts):
            chart = new_chart(kind, title)
            for col in cols:
                chart.series.append(series(refs[col], refs["period"], labels[col]))
            if len(cols) == 1:
                chart.legend = None
                if formats[cols[0]] == "0%":
                    chart.y_axis.number_format = "0%"
            anchor_col = "A" if idx % 2 == 0 else "J"
            dash.add_chart(chart, f"{anchor_col}{8 + 15 * (idx // 2)}")

        if not has_customers:
            return

        # customers in the last complete period of the selected granularity
        cust_sheet, cust_df = customers
        cust_ranges = {}
        for col in (
            "week", "month", "quarter", "organization", "request_type_category",
            "new_tickets", "closed_tickets", "hours_logged",
        ):
            letter = get_column_letter(cust_df.columns.get_loc(col) + 1)
            cust_ranges[col] = f"'{cust_sheet}'!${letter}$2:${letter}${len(cust_df) + 1}"
        in_period = (
            f"(CHOOSE($B$5,{cust_ranges['week']},{cust_ranges['month']},"
            f"{cust_ranges['quarter']})=$B$9)"
        )
        orgs = sorted({str(o) for o in cust_df["organization"]})
        end = len(orgs) + 1
        calc["E1"], calc["F1"] = "customers_ranked", "organization"
        for row, org in enumerate(orgs, start=2):
            calc[f"F{row}"] = org
        # per measure: value and tie-broken score per customer, then the ranked top list
        values: list[str] = []
        ranked: list[tuple[int, int]] = []
        rank_base = 7 + 2 * len(cust_measures)
        for j, (_, header, col, cat) in enumerate(cust_measures):
            v, sc = get_column_letter(7 + 2 * j), get_column_letter(8 + 2 * j)
            calc[f"{v}1"], calc[f"{sc}1"] = header, f"{header} rank"
            cat_filter = ""
            if cat:
                quoted = cat.replace('"', '""')
                cat_filter = f'*({cust_ranges["request_type_category"]}="{quoted}")'
            for row in range(2, end + 1):
                calc[f"{v}{row}"] = (
                    f"=SUMPRODUCT(({cust_ranges['organization']}=$F{row})*{in_period}"
                    f"{cat_filter}*{cust_ranges[col]})"
                )
                calc[f"{sc}{row}"] = f"={v}{row}+ROW()/1000000000"
            calc[f"E{j + 2}"] = (
                f'=MIN({DASHBOARD_TOP_CUSTOMERS},COUNTIF(${v}$2:${v}${end},">0"))'
            )
            values.append(f"dashboard_calc!${v}$2:${v}${end}")
            org_col, top_col = rank_base + 2 * j, rank_base + 2 * j + 1
            o, t = get_column_letter(org_col), get_column_letter(top_col)
            calc[f"{o}1"], calc[f"{t}1"] = f"{header} customer", f"{header} top"
            scores = f"${sc}$2:${sc}${end}"
            for i in range(1, DASHBOARD_TOP_CUSTOMERS + 1):
                match = f"MATCH(LARGE({scores},{i}),{scores},0)"
                guard = f'IF({i}>$E${j + 2},"",'
                calc[f"{o}{i + 1}"] = f"={guard}INDEX($F$2:$F${end},{match}))"
                calc[f"{t}{i + 1}"] = f"={guard}INDEX(${v}$2:${v}${end},{match}))"
            ranked.append((org_col, top_col))

        dash[f"A{cust_row}"] = "Customers in the last complete period:"
        dash[f"C{cust_row}"] = '=IF(dashboard_calc!$B$9="","none",dashboard_calc!$B$9)'
        for ref in (f"A{cust_row}", f"C{cust_row}"):
            dash[ref].font = Font(bold=True, size=13)
        chart_idx = 0
        for j, (title, *_) in enumerate(cust_measures):
            if not title:
                continue
            org_col, top_col = ranked[j]
            count = f"dashboard_calc!$E${j + 2}"
            chart = new_chart("bar", title)
            chart.x_axis.scaling.orientation = "maxMin"
            chart.x_axis.tickLblSkip = 1
            chart.y_axis.crosses = "max"
            chart.legend = None
            chart.series.append(
                series(
                    add_name(f"Dash_cust{j}_top", top_col, 2, count, "dashboard_calc"),
                    add_name(f"Dash_cust{j}_org", org_col, 2, count, "dashboard_calc"),
                    title,
                )
            )
            anchor_col = "A" if chart_idx % 2 == 0 else "J"
            dash.add_chart(chart, f"{anchor_col}{cust_row + 1 + 15 * (chart_idx // 2)}")
            chart_idx += 1

        # customer table, ranked by new tickets
        headers = ["Customer"] + [header for _, header, _, _ in cust_measures]
        for c, header in enumerate(headers, start=1):
            cell = dash.cell(row=cust_table_row, column=c, value=header)
            cell.font, cell.fill, cell.border = Font(bold=True), header_fill, box
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        org_letter = get_column_letter(ranked[0][0])
        for i in range(1, DASHBOARD_TOP_CUSTOMERS + 1):
            row = cust_table_row + i
            dash.cell(row=row, column=1, value=f"=dashboard_calc!${org_letter}${i + 1}")
            for j, (_, _, col, _) in enumerate(cust_measures):
                cell = dash.cell(
                    row=row,
                    column=j + 2,
                    value=(
                        f'=IF($A{row}="","",INDEX({values[j]},'
                        f"MATCH($A{row},dashboard_calc!$F$2:$F${end},0)))"
                    ),
                )
                cell.number_format = "0.0" if col == "hours_logged" else "0"

    @log_call
    def recalculate_all(self) -> None:
        """Recalculate KPI and mapped fields for all stored tickets."""
        with self.store.engine.connect() as conn:
            tickets = [tid for (tid,) in conn.execute(select(self.store.jira_tickets.c.id)).fetchall()]
        self.recalculate_tickets(tickets)

    @log_call
    def recalculate_tickets(self, ticket_ids: Iterable[str]) -> None:
        """Recalculate KPI and mapped fields for the given ticket ids."""
        for ticket_id in ticket_ids:
            issue_row, comments, worklogs, changelog = self.store.get_ticket_with_related(
                ticket_id
            )
            if not issue_row:
                continue

            # Recalculate internal user flag for all comments
            updated_comments: list[dict[str, Any]] = []
            for c in comments:
                author = {
                    "displayName": c.get("author"),
                    "emailAddress": c.get("author_email"),
                }
                updated = dict(c)
                # Preserve stored flag if no email is present to avoid
                # misclassifying internal comments
                internal_stored = c.get("internal_user")
                if internal_stored is not None and not c.get("author_email"):
                    updated["internal_user"] = internal_stored
                else:
                    updated["internal_user"] = is_internal_user(author)
                updated_comments.append(updated)
            self.store.insert_many(self.store.jira_comments, updated_comments)

            fields = {
                "summary": issue_row.get("summary"),
                "status": {"name": issue_row.get("status")},
            }
            if issue_row.get("assignee"):
                fields["assignee"] = {
                    "displayName": issue_row.get("assignee"),
                    "emailAddress": issue_row.get("assignee_email"),
                }
            if issue_row.get("creator"):
                fields["creator"] = {
                    "displayName": issue_row.get("creator"),
                    "emailAddress": issue_row.get("creator_email"),
                }
            if issue_row.get("request_participants"):
                # stored as a comma-separated list of emails (or names)
                fields["requestParticipants"] = [
                    {"emailAddress": p}
                    for p in issue_row["request_participants"].split(",")
                    if p
                ]
            if issue_row.get("labels"):
                fields["labels"] = [l for l in issue_row.get("labels", "").split(",") if l]
            if issue_row.get("priority"):
                fields["priority"] = {"name": issue_row.get("priority")}
            if issue_row.get("request_type"):
                fields["requestType"] = issue_row.get("request_type")
            if issue_row.get("deployment_type"):
                fields["Deployment type"] = issue_row.get("deployment_type")
            if issue_row.get("organizations"):
                fields["Organizations"] = [
                    o for o in issue_row.get("organizations", "").split(",") if o
                ]
            if issue_row.get("resolution"):
                fields["resolution"] = {"name": issue_row.get("resolution")}
            if issue_row.get("satisfaction"):
                fields["satisfaction"] = issue_row.get("satisfaction")
            if issue_row.get("satisfaction_ts"):
                fields["satisfaction_ts"] = issue_row.get("satisfaction_ts")
            if issue_row.get("work_category"):
                fields["work category"] = issue_row.get("work_category")
            if issue_row.get("sentiment"):
                fields["sentiment"] = issue_row.get("sentiment")
            fields["issuetype"] = {"name": issue_row.get("issuetype") or "Support"}

            issue = {
                "id": issue_row["id"],
                "key": issue_row.get("key"),
                "created": issue_row.get("created"),
                "resolved": issue_row.get("resolved"),
                "updated": issue_row.get("updated"),
                "fields": fields,
            }
            processed_issue = calculate_kpi_fields(issue, updated_comments, changelog)
            self.store_jira_tickets([processed_issue])
            calendar_rows = make_jira_ticket_calendar(
                processed_issue,
                updated_comments,
                worklogs,
                changelog,
                self.config.custom_fields.get("request_type"),
            )
            with self.store.engine.begin() as conn:
                conn.execute(
                    delete(self.store.jira_calendar).where(
                        self.store.jira_calendar.c.ticket_id == ticket_id
                    )
                )
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
            get_since = updated_since - datetime.timedelta(minutes=JIRA_PULL_OVERLAP_MIN)
            ts = get_since.astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M")
            parts.append(f"updated >= \"{ts}\"")
        final_jql = " AND ".join(parts)

        url = f"{self.config.jira_base_url}/rest/api/3/search/jql"
        next_page_token: str | None = None
        max_results = 50
        all_issues: list[dict[str, Any]] = []

        while True:
            payload: dict[str, Any] = {
                "jql": final_jql,
                "maxResults": max_results,
                "fields": ["*all"],
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            logging.getLogger(__name__).info(f"POST {url} json={payload}")
            r = requests.post(
                url,
                json=payload,
                auth=self._jira_auth(),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()

            # The new search API nests issues under "results".
            issues = data.get("issues")
            next_page_token = data.get("nextPageToken")
            if issues is None:
                results = data.get("results", [])
                if results:
                    first = results[0]
                    issues = first.get("issues", [])
                    next_page_token = first.get("nextPageToken")
                else:
                    issues = []
                    next_page_token = None
            for issue in issues:
                fields = issue.get("fields", {})
                issue["created"] = self._parse_ts(fields.get("created"))
                issue["resolved"] = self._parse_ts(fields.get("resolutiondate"))
                issue["updated"] = self._parse_ts(fields.get("updated"))
            all_issues.extend(issues)

            if not next_page_token or not issues:
                break

        return all_issues

    @log_call
    def store_jira_tickets(self, issues: Iterable[dict[str, Any]]) -> None:
        records = []
        for issue in issues:
            fields = issue.get("fields", {})
            prio_txt = (fields.get("priority") or {}).get("name")
            prio_pos = PRIORITY_POS_MAP.get(prio_txt, 0)
            sat_field = self._field_value(fields, "satisfaction", ["satisfaction"])
            sat_ts_field = self._field_value(fields, "satisfaction_ts", ["satisfaction_ts"])
            req_type = self._simple_value(
                self._field_value(fields, "request_type", ["requestType"])
            )
            issuetype = self._simple_value(
                (fields.get("issuetype") or {}).get("name")
            ) or "Support"
            participants = self._field_value(
                fields, "request_participants", ["requestParticipants"]
            )
            participants_csv = ",".join(
                [
                    p.get("emailAddress")
                    or p.get("displayName")
                    or p.get("name")
                    or str(p)
                    for p in (participants or [])
                ]
            )
            records.append(
                {
                    "id": issue["id"],
                    "key": issue.get("key"),
                    "summary": fields.get("summary"),
                    "status": (fields.get("status") or {}).get("name"),
                    "assignee": (fields.get("assignee") or {}).get("displayName"),
                    "assignee_email": (fields.get("assignee") or {}).get("emailAddress"),
                    "creator": (fields.get("creator") or {}).get("displayName"),
                    "creator_email": (fields.get("creator") or {}).get("emailAddress"),
                    "request_participants": participants_csv,
                    "labels": ",".join(fields.get("labels") or []),
                    "priority": prio_txt,
                    "priority_pos": prio_pos,
                    "request_type": req_type,
                    "issuetype": issuetype,
                    "deployment_type": self._simple_value(
                        self._field_value(fields, "deployment_type", ["Deployment type"])
                    ),
                    "organizations": ",".join(
                        [
                            o.get("name") if isinstance(o, dict) else str(o)
                            for o in (
                                self._field_value(fields, "organizations", ["Organizations"]) or []
                            )
                        ]
                    ),
                    "primary_organization": primary_org_from_names([
                        o.get("name") if isinstance(o, dict) else str(o)
                        for o in (
                            self._field_value(fields, "organizations", ["Organizations"]) or []
                        )
                    ]) or None,
                    "resolution": self._simple_value(
                        self._field_value(fields, "resolution", ["resolution"])
                    ),
                    "satisfaction": self._simple_value(sat_field),
                    "satisfaction_ts": self._parse_ts(sat_ts_field),
                    "work_category": self._simple_value(
                        self._field_value(fields, "work_category", ["work category"])
                    ),
                    "sentiment": self._simple_value(
                        self._field_value(fields, "sentiment", ["sentiment"])
                    ),

                    "created": issue.get("created"),
                    "resolved": issue.get("resolved"),
                    "updated": issue.get("updated"),
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
                    "author_email": comment.get("author", {}).get("emailAddress"),
                    "type": comment_type,
                    "body": body,
                    "public": comment.get("public"),
                    "internal_user": is_internal_user(comment.get("author")),
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
                    "author_email": log.get("author", {}).get("emailAddress"),
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
            author_email = hist.get("author", {}).get("emailAddress")
            created = hist.get("created")
            items = hist.get("items", [])
            for idx, item in enumerate(items):
                records.append(
                    {
                        "id": f"{history_id}-{idx}",
                        "history_id": history_id,
                        "ticket_id": issue_id,
                        "author": author,
                        "author_email": author_email,
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
    def _simple_value(val: Any) -> Any:
        """Return a basic value from a Jira field entry."""
        if isinstance(val, dict):
            for key in ("value", "name", "text", "requestType", "rating"):
                if key in val:
                    if key == "requestType":
                        return val.get(key, {}).get("name")
                    return val.get(key)
            return None
        if isinstance(val, list):
            return ",".join(str(SupportStatsGetter._simple_value(v) or v) for v in val)
        return val

    @staticmethod
    def _parse_ts(value: str | datetime.datetime | None) -> datetime.datetime | None:
        """Return a timezone-aware datetime for *value*.

        The input may already be a datetime instance or a string. In either
        case the result is converted to an aware UTC datetime. ``None`` values
        are returned unchanged.
        """
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            ts = value
        else:
            ts = dateutil.parser.isoparse(str(value))
        return ts if ts.tzinfo else pytz.utc.localize(ts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Jira ticket statistics")
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to the YAML (or JSON) config file. Default: config.yaml, else config.json",
    )
    parser.add_argument(
        "--log-level",
        help="Logging level (overrides config)",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument(
        "--recalculate-all",
        action="store_true",
        help="Recalculate KPI fields for all stored tickets",
    )
    parser.add_argument(
        "--output-excel",
        nargs="?",
        const="SupportStats.xlsx",
        default=None,
        help="Export all views to an Excel workbook. If filename is omitted, uses 'SupportStats.xlsx'.",
    )

    def parse_start_date(value: str) -> datetime.datetime:
        try:
            return datetime.datetime.combine(
                datetime.date.fromisoformat(value), datetime.time()
            )
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid date '{value}', expected YYYY-MM-DD"
            )

    parser.add_argument(
        "--start-date",
        type=parse_start_date,
        metavar="YYYY-MM-DD",
        help=(
            "For a new database: only fetch tickets updated on or after this date. "
            "Ignored once the database contains tickets."
        ),
    )
    args = parser.parse_args()

    if args.config:
        cfg_path = Path(args.config)
    else:
        # older installations still use config.json
        cfg_path = next(
            (p for p in (Path("config.yaml"), Path("config.json")) if p.exists()),
            Path("config.yaml"),
        )
    if not cfg_path.exists():
        logging.basicConfig(level=logging.ERROR)
        logging.error(f"No configuration file found at {cfg_path}.")
        raise SystemExit(1)

    cfg = Config.load(cfg_path)
    log_level = args.log_level or cfg.log_level
    setup_logging(log_level, cfg.log_file)

    output_excel = Path(args.output_excel) if args.output_excel else cfg.output_excel

    getter = SupportStatsGetter(cfg)
    logging.info("Running SupportStatsGetter")
    if cfg.jira_base_url:
        try:
            since = getter.latest_update_timestamp()
            if args.start_date and since is None:
                since = args.start_date
                logging.info(
                    "New database: fetching tickets updated since %s",
                    since.date().isoformat(),
                )
            elif args.start_date:
                logging.warning(
                    "--start-date ignored: database already contains tickets, "
                    "fetching updates since %s",
                    since,
                )
            logging.info("Fetching Jira tickets")
            raw_issues = getter.fetch_jira_tickets(updated_since=since)
            logging.info("Retrieved %d tickets from Jira", len(raw_issues))
            comments_total = 0
            worklogs_total = 0
            changelog_total = 0
            calendar_total = 0
            processed: list[dict[str, Any]] = []
            total_issues = len(raw_issues)
            for idx, issue in enumerate(raw_issues, 1):
                logging.info(
                    "Processing ticket %s (%d/%d)", issue.get("key"), idx, total_issues
                )
                comments = getter.fetch_jira_comments(issue["id"])
                worklogs = getter.fetch_jira_worklogs(issue["id"])
                changelog = getter.fetch_jira_changelog(issue["id"])

                processed_issue = calculate_kpi_fields(issue, comments, changelog)
                processed.append(processed_issue)

                getter.store_jira_comments(issue["id"], issue.get("key"), comments)
                getter.store_jira_worklogs(issue["id"], worklogs)
                getter.store_jira_changelog(issue["id"], changelog)

                calendar_rows = make_jira_ticket_calendar(
                    issue,
                    comments,
                    worklogs,
                    changelog,
                    getter.config.custom_fields.get("request_type"),
                    getter.config.priority_deescalation_ignore_minutes,
                )
                getter.store.insert_many(getter.store.jira_calendar, calendar_rows)
                calendar_total += len(calendar_rows)

                comments_total += len(comments)
                worklogs_total += len(worklogs)
                for hist in changelog:
                    changelog_total += len(hist.get("items", []))

                if idx % 10 == 0 or idx == total_issues:
                    logging.info("Processed %d of %d tickets", idx, total_issues)

            logging.info("Saving tickets to database")
            getter.store_jira_tickets(processed)
            if args.recalculate_all:
                logging.info("Recalculating statistics for all tickets")
                getter.recalculate_all()
            else:
                logging.info("Recalculating statistics for new tickets")
                getter.recalculate_tickets([issue["id"] for issue in processed])
            logging.info(
                f"Stored {len(processed)} Jira tickets, {comments_total} comments, {worklogs_total} worklogs, {changelog_total} changelog items and {calendar_total} calendar rows in {cfg.db_path}"
            )
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch or store Jira data")
    else:
        logging.error("Jira configuration missing in config file.")

    if output_excel:
        logging.info("Writing Excel workbook to %s", output_excel)
        getter.export_views_to_excel(output_excel, cfg.excel_write_mode)
        logging.info("Finished writing Excel workbook to %s", output_excel)
