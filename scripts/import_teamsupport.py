import argparse
import json
from pathlib import Path
import datetime
import pytz
import dateutil.parser
from sqlalchemy import create_engine
import urllib.parse

import re

from SupportStatsGetter import (
    DatabaseStore,
    cw_string_from_datetime,
    primary_org_from_names,
    ORG_PATTERNS,
)
from cadis_bdk_contacts import CADIS_BDK_CONTACTS
from TeamSupportStatsGetter import (
    STATUS_CHANGE_RE,
    GROUP_ASSIGN_RE,
    USER_ASSIGN_RE,
    SEVERITY_CHANGE_RE,
    EMAIL_REPLY_RE,
)

SEVERITY_POS_MAP = {
    "Normal": 0,
    "High": 100,
    "Critical": 200,
    "Medium": 0,
    "Low": -100
}

# Map legacy TeamSupport statuses to the Jira equivalents. This ensures that
# imported records use the same status vocabulary as tickets created in Jira
# directly which is important for KPI calculations.
TS_STATUS_MAP = {
    "New": "Waiting for Support",
    "Waiting Internal": "Escalated",
    "Waiting on Customer": "Waiting for Customer",
    "Closed": "Closed",
    "Closed - Resolved": "Resolved",
    "Spam/Junk Delete": "Cancelled",
    "Customer Replied": "Waiting for Support",
    "In Progress": "In Progress",
    "On Hold": "Pending",
    "Closed - NR": "Closed",
}


def map_status(value):
    """Translate a TeamSupport status to the Jira status vocabulary."""
    if value is None:
        return None
    return TS_STATUS_MAP.get(str(value).strip(), str(value).strip())


def severity_to_priority(txt):
    """Return priority text and numeric position for a severity value."""
    if txt is None:
        return None, 0
    txt = str(txt).strip()
    return txt, int(SEVERITY_POS_MAP.get(txt, 0))


def week_underscore_to_dot(week_str):
    """Convert ``YYYY_WW`` strings from TeamSupport to ``YYYY.WW``."""
    if week_str is None:
        return None
    return str(week_str).replace("_", ".")


def bool_from_string(val):
    """Return boolean value parsed from ``"True"``/``"False"`` strings."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() == "true"


def parse_dt(val):
    if not val:
        return None
    dt = dateutil.parser.isoparse(val)
    if not dt.tzinfo:
        dt = pytz.utc.localize(dt)
    return dt


def convert_cw(val):
    if not val:
        return None
    if isinstance(val, list):
        return ",".join(filter(None, (convert_cw(v) for v in val)))
    return week_underscore_to_dot(val)


def extract_orgs(ticket):
    names = []
    pc = ticket.get("PrimaryCustomer")
    if pc:
        names.append(pc.strip())
    customers = ticket.get("Customers", {}).get("Customer")
    if isinstance(customers, list):
        for c in customers:
            name = c.get("CustomerName")
            if name:
                names.append(name)
    elif isinstance(customers, dict):
        name = customers.get("CustomerName")
        if name:
            names.append(name)
    return names


def map_ticket(t):
    created = parse_dt(t.get("DateCreated"))
    resolved = parse_dt(t.get("ResolvedDate"))
    updated = parse_dt(t.get("DateModified"))
    first_resp = parse_dt(t.get("FirstResponseDate"))
    last_resp = parse_dt(t.get("LastResponseDate"))
    orgs = extract_orgs(t)
    priority_txt, priority_pos = severity_to_priority(t.get("Severity"))
    assignee = t.get("UserName")
    if assignee and assignee.strip().lower() == "unassigned":
        assignee = None
    return {
        "id": str(t.get("ID")),
        "key": str(t.get("TicketNumber")),
        "summary": (t.get("TicketName") or t.get("Name") or "").strip(),
        "status": map_status(t.get("Status")),
        "assignee": assignee,
        "assignee_email": None,
        "creator": t.get("CreatorName"),
        "creator_email": None,
        "labels": (t.get("GroupName") or "").strip(),
        "priority": priority_txt,
        "priority_pos": priority_pos,
        "created": created,
        "resolved": resolved,
        "updated": updated,
        "created_week": cw_string_from_datetime(created),
        "resolved_week": cw_string_from_datetime(resolved),
        "updated_week": cw_string_from_datetime(updated),
        "active_weeks": convert_cw(t.get("ActiveWeeks")),
        "first_response_date": first_resp,
        "last_response_date": last_resp,
        "first_response_date_week": cw_string_from_datetime(first_resp),
        "last_response_date_week": cw_string_from_datetime(last_resp),
        "first_response_time_minutes": int((first_resp - created).total_seconds() // 60) if created and first_resp else None,
        "resolution_time_days": (resolved - created).total_seconds() / 86400 if created and resolved else None,
        "request_type": t.get("ProductName"),
        "deployment_type": None,
        "organizations": ",".join(orgs),
        "primary_organization": primary_org_from_names(orgs),
        "resolution": t.get("Resolution"),
        "satisfaction": None,
        "satisfaction_ts": None,
        "work_category": None,
        "sentiment": None,
    }


def map_comment(act, ticket_id, ticket_nr):
    return {
        "id": str(act.get("ID")),
        "ticket_id": ticket_id,
        "issue_key": ticket_nr,
        "author": act.get("CreatorName"),
        "author_email": None,
        "type": act.get("ActionType"),
        "body": act.get("Description"),
        "public": bool_from_string(act.get("IsVisibleOnPortal")),
        "internal_user": str(act.get("CreatorID")) in CADIS_BDK_CONTACTS,
        "created": parse_dt(act.get("DateCreated")),
        "updated": parse_dt(act.get("DateModified")),
    }


def map_worklog(act, ticket_id):
    if act.get("TimeSpent") is None:
        return None
    started = parse_dt(act.get("DateStarted"))
    created = parse_dt(act.get("DateCreated"))
    return {
        "id": str(act.get("ID")),
        "ticket_id": ticket_id,
        "author": act.get("CreatorName"),
        "author_email": None,
        "time_spent_minutes": int(float(act.get("TimeSpent"))),
        "started": started,
        "started_week": cw_string_from_datetime(started),
        "text": act.get("Description"),
        "created": created,
        "created_week": cw_string_from_datetime(created),
    }


def map_history(hist, ticket_id):
    desc = hist.get("Description") or ""
    field = "Legacy"
    from_string = None
    to_string = None
    if m := re.match(STATUS_CHANGE_RE, desc):
        field = "status"
        from_string = map_status(m.group(1))
        to_string = map_status(m.group(2))
    elif m := re.match(EMAIL_REPLY_RE, desc):
        field = "status"
        to_string = map_status(m.group(1))
    elif m := re.match(GROUP_ASSIGN_RE, desc):
        field = "group"
        from_string = m.group(1)
        to_string = m.group(2)
    elif m := re.match(USER_ASSIGN_RE, desc):
        field = "assignee"
        from_string = m.group(1)
        to_string = m.group(2)
    elif m := re.match(SEVERITY_CHANGE_RE, desc):
        field = "priority"
        from_string = m.group(1)
        to_string = m.group(2)
    else:
        to_string = desc

    return {
        "id": f"{hist.get('ID')}-0",
        "history_id": hist.get("ID"),
        "ticket_id": ticket_id,
        "author": hist.get("Actor"),
        "author_email": None,
        "internal_user": str(hist.get("CreatorID")) in CADIS_BDK_CONTACTS,
        "created": parse_dt(hist.get("DateCreated")),
        "field": field,
        "fieldtype": None,
        "from_value": None,
        "from_string": from_string,
        "to_value": None,
        "to_string": to_string,
    }


def map_calendar(cal):
    dt = parse_dt(cal.get("date"))
    modified = parse_dt(cal.get("DateModified"))
    start_status = map_status(cal.get("start_status"))
    end_status = map_status(cal.get("end_status"))
    is_junk = bool_from_string(cal.get("is_junk"))

    start_assigned_user = cal.get("start_assigned_user")
    if start_assigned_user and start_assigned_user.strip().lower() == "unassigned":
        start_assigned_user = None

    end_assigned_user = cal.get("end_assigned_user")
    if end_assigned_user and end_assigned_user.strip().lower() == "unassigned":
        end_assigned_user = None

    if is_junk is None:
        status_for_junk = end_status or start_status or ""
        is_junk = status_for_junk.lower() == "cancelled"


    return {
        "record_id": cal.get("ID"),
        "ticket_id": cal.get("ticket_id"),
        "ticket_key": cal.get("ticket_nr"),
        "date": dt,
        "comments": cal.get("actions"),
        "public_comments": cal.get("public_actions"),
        "start_assigned_group": cal.get("start_assigned_group"),
        "end_assigned_group": cal.get("end_assigned_group"),
        "start_assigned_user": start_assigned_user,
        "end_assigned_user": end_assigned_user,
        "start_assigned_user_email": None,
        "end_assigned_user_email": None,
        "did_escalate_priority": bool_from_string(cal.get("did_escalate_severity")),
        "did_escalate_cs": bool_from_string(cal.get("did_escalate_cs")),
        "did_escalate_management": bool_from_string(cal.get("did_escalate_management")),
        "start_priority_txt": cal.get("start_severity_txt"),
        "start_priority_pos": cal.get("start_severity_pos"),
        "end_priority_txt": cal.get("end_severity_txt"),
        "end_priority_pos": cal.get("end_severity_pos"),
        "time_spent_min": cal.get("time_spent_min"),
        "start_status": start_status,
        "end_status": end_status,
        "did_resolve": bool_from_string(cal.get("did_resolve")),
        "total_days_open": cal.get("total_days_open"),
        "weekdays_days_since_last_contact": cal.get("weekdays_days_since_last_contact"),
        "adjusted_resolution_days": cal.get("adjusted_resolution_days"),
        "adjusted_resolution_weekdays": cal.get("adjusted_resolution_weekdays"),
        "is_junk": is_junk,
        "cw": convert_cw(cal.get("cw")),
        "weekday": cal.get("weekday"),
        "date_modified": modified,
    }


def main():
    parser = argparse.ArgumentParser(description="Import TeamSupport JSON data into the database")
    parser.add_argument("--db", default="support_stats", help="Path to database file")
    parser.add_argument("--sqlite", action="store_true", help="Use SQLite database")
    parser.add_argument("--input", default=str(Path("resource/tickets.json")), help="Path to JSON file")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        tickets = json.load(f)

    if args.sqlite:
        engine = create_engine(f"sqlite+pysqlite:///{args.db}.sqlite")
    else:
        db_path = Path(f"{args.db}.accdb").resolve()
        if not db_path.is_file():
            raise FileNotFoundError(
                f"Access database not found at {db_path}. "
                "Please create the .accdb file or provide a correct path."
            )
        conn = urllib.parse.quote_plus(
            f"DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={str(db_path)}"
        )
        engine = create_engine(f"access+pyodbc:///?odbc_connect={conn}")
    store = DatabaseStore(engine)
    global ORG_PATTERNS
    ORG_PATTERNS = store.load_org_patterns()

    ticket_rows = []
    comment_rows = []
    worklog_rows = []
    changelog_rows = []
    calendar_rows = []

    for t in tickets:
        tid = str(t.get("ID"))
        tkey = str(t.get("TicketNumber"))
        ticket_rows.append(map_ticket(t))
        for act in t.get("Actions", []):
            comment_rows.append(map_comment(act, tid, tkey))
            wl = map_worklog(act, tid)
            if wl:
                worklog_rows.append(wl)
        for hist in t.get("History", []):
            changelog_rows.append(map_history(hist, tid))
        for cal in t.get("Calendar", []):
            calendar_rows.append(map_calendar(cal))

    store.insert_many(store.jira_tickets, ticket_rows)
    store.insert_many(store.jira_comments, comment_rows)
    store.insert_many(store.jira_worklogs, worklog_rows)
    store.insert_many(store.jira_changelog, changelog_rows)
    store.insert_many(store.jira_calendar, calendar_rows)


if __name__ == "__main__":
    main()
