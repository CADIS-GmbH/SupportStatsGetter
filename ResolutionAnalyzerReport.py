import argparse
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
import requests
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

JIRA_BASE_URL = "https://cadissoftware.atlassian.net"
JQL_TEMPLATE = (
    'project = "Customer Service" AND status in (Closed, Resolved) '
    'AND resolutiondate >= "{start}" AND resolutiondate < "{end}"'
)

MAX_COMMENTS = 3
MAX_RESULTS_PER_PAGE = 100

session: requests.Session | None = None

# Resolver alias lookup in priority order: (alias, real name, level)
DEFAULT_RESOLVER_ALIASES: list[tuple[str, str, int]] = [
    ("Abdelrahman Khamis", "Abdelrahman Khamis", 1),
    ("Abdelrahman.Khamis", "Abdelrahman Khamis", 1),
    ("Ali Ahmed", "Ali Mohamed", 1),
    ("Ali Mohamed", "Ali Mohamed", 1),
    ("Ali.Mohamed", "Ali Mohamed", 1),
    ("Anne Leske", "Anne Leske", 3),
    ("Anne.Leske", "Anne Leske", 3),
    ("Benedikt Wagner", "Benedikt Wagner", 2),
    ("Benedikt.Wagner", "Benedikt Wagner", 2),
    ("Erik von Rüsten", "Erik von Rüsten", 3),
    ("Erik.vonRüsten", "Erik von Rüsten", 3),
    ("Inge Emmerichs", "Inge Emmerichs", 3),
    ("Inge.Emmerichs", "Inge Emmerichs", 3),
    ("Isabelle Lernbass", "Isabelle Lernbass", 3),
    ("Isabelle.Lernbass", "Isabelle Lernbass", 3),
    ("Johannes Arneth", "Johannes Arneth", 3),
    ("Johannes.Arneth", "Johannes Arneth", 3),
    ("Julian Beyer", "Julian Beyer", 2),
    ("Julian.Beyer", "Julian Beyer", 2),
    ("Mohamed Shebl", "Mohamed Shebl", 1),
    ("Mohamed.Shebl", "Mohamed Shebl", 1),
    ("Nermine Rashad", "Nermine Rashad", 1),
    ("Nermine.Rashad", "Nermine Rashad", 1),
    ("Müller", "Ullrich Müller", 3),
    ("Ulli Müller", "Ullrich Müller", 3),
    ("Ulli.Müller", "Ullrich Müller", 3),
    ("Ullrich Müller", "Ullrich Müller", 3),
    ("Ullrich.Müller", "Ullrich Müller", 3),
    ("Winfried Gillich", "Winfried Gillich", 3),
    ("Winfried.Gillich", "Winfried Gillich", 3),
    ("Gillich", "Winfried Gillich", 3),
    ("Franz Renger", "Franz Renger", 3),
    ("Franz.Renger", "Franz Renger", 3),
    ("Khalil Boukhdoud", "Khalil Boukhdoud", 3),
    ("Khalil.Boukhdoud", "Khalil Boukhdoud", 3),
    ("Stephan Schneider", "Stephan Schneider", 2),
    ("Stephan.Schneider", "Stephan Schneider", 2),
    ("Stefan Lieth", "Stefan Lieth", 3),
    ("Stefan.Lieth", "Stefan Lieth", 3),
    ("Hamud.AlHammoud", "Hamud Al Hammoud", 3),
    ("Hamud Al Hammoud", "Hamud Al Hammoud", 3),
    ("Al Hammoud, Hamud", "Hamud Al Hammoud", 3),
    ("André Hinz", "André Hinz", 3),
    ("Andre Hinz", "André Hinz", 3),
    ("André.Hinz", "André Hinz", 3),
    ("Andre.Hinz", "André Hinz", 3),
    ("Cornelia Hagner", "Cornelia Hagner", 3),
    ("Cornelia.Hagner", "Cornelia Hagner", 3),
    ("Gillich, Winfried", "Winfried Gillich", 3),
    ("Müller, Ullrich", "Ullrich Müller", 3),
    ("Thomas.Wagner", "Thomas Wagner", 2),
    ("Thomas Wagner", "Thomas Wagner", 2),
    ("Armin Dobner", "Armin Dobner", 3),
    ("Armin.Dobner", "Armin Dobner", 3),
    ("Martin.Görick", "Martin Görick", 3),
    ("Martin Görick", "Martin Görick", 3),
]

# ------------------------------
# Regex to extract structured fields
# ------------------------------
BLOCK_REGEX = re.compile(
    r"Resolution Summary by Rovo:.*?"
    r"Category:\s*(?P<category>.*?)(?=Summary:)"
    r"Summary:\s*(?P<summary>.*?)(?=Rationale:)"
    r"Rationale:\s*(?P<rationale>.*?)(?=Suggested new category:|Resolver:|Outcome:)",
    re.DOTALL,
)

OPTIONAL_NEW_CATEGORY = re.compile(
    r"Suggested new category:\s*(?P<newcat>.*?)(?=Resolver:|Outcome:)", re.DOTALL
)

RESOLVER_REGEX = re.compile(
    r"Resolver:\s*(?P<resolver_line>.*?)(?=Outcome:)", re.DOTALL
)

OUTCOME_REGEX = re.compile(r"Outcome:\s*(?P<outcome>.*)", re.DOTALL)

ResolverAliasRow = dict[str, str | int]
ResolverAliasList = list[ResolverAliasRow]


class ResolutionDatabase:
    """Wrapper for SQLite storage of Jira resolution summaries."""

    def __init__(self, db_path: Path):
        self.engine = create_engine(f"sqlite+pysqlite:///{db_path}")
        self.meta = MetaData()
        self._prepare_schema()
        self._seed_resolver_aliases()

    def _prepare_schema(self):
        self.resolution_reports = Table(
            "resolution_reports",
            self.meta,
            Column("issue_key", String, primary_key=True),
            Column("issue_id", String),
            Column("summary", Text),
            Column("status", String),
            Column("issuetype", String),
            Column("priority", String),
            Column("request_type", String),
            Column("organizations", Text),
            Column("resolvers_raw", Text),
            Column("primary_resolver", String),
            Column("resolve_helpers", Text),
            Column("escalated_to_level", Integer),
            Column("solved_level", Integer),
            Column("outcome", String),
            Column("work_category", String),
            Column("category", String),
            Column("new_category", String),
            Column("summary_block", Text),
            Column("rationale", Text),
            Column("updated_at", DateTime),
        )
        self.resolution_resolvers = Table(
            "resolution_resolvers",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("issue_key", String, ForeignKey("resolution_reports.issue_key")),
            Column("resolver", String),
            Column("resolver_index", Integer),
            Column("resolver_level", Integer),
        )
        self.resolver_aliases = Table(
            "resolver_aliases",
            self.meta,
            Column("alias", String, primary_key=True),
            Column("canonical_name", String, nullable=False),
            Column("level", Integer, nullable=False),
            Column("priority", Integer, nullable=False),
        )
        self.meta.create_all(self.engine)

    def _seed_resolver_aliases(self):
        """Populate resolver_aliases table with defaults when empty."""
        with self.engine.begin() as conn:
            count = conn.execute(
                select(func.count()).select_from(self.resolver_aliases)
            ).scalar()
            if count:
                return
            rows = []
            for priority, (alias, real, level) in enumerate(DEFAULT_RESOLVER_ALIASES):
                rows.append(
                    {
                        "alias": alias,
                        "canonical_name": real,
                        "level": level,
                        "priority": priority,
                    }
                )
            conn.execute(self.resolver_aliases.insert(), rows)

    def load_resolver_aliases(self) -> ResolverAliasList:
        """Return resolver aliases ordered by priority."""
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    self.resolver_aliases.c.alias,
                    self.resolver_aliases.c.canonical_name,
                    self.resolver_aliases.c.level,
                    self.resolver_aliases.c.priority,
                ).order_by(self.resolver_aliases.c.priority)
            ).all()
        return [
            {
                "alias": row.alias,
                "alias_cf": row.alias.casefold().strip(),
                "canonical_name": row.canonical_name,
                "level": row.level,
                "priority": row.priority,
            }
            for row in rows
        ]

    def upsert_issue(self, issue_row: dict, resolver_rows: list[dict]) -> None:
        """Insert or update an issue and replace its resolver rows atomically."""
        with self.engine.begin() as conn:
            stmt = sqlite_insert(self.resolution_reports).values(issue_row)
            update_cols = {
                col.name: stmt.excluded[col.name]
                for col in self.resolution_reports.columns
                if col.name != "issue_key"
            }
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["issue_key"], set_=update_cols
                )
            )
            conn.execute(
                delete(self.resolution_resolvers).where(
                    self.resolution_resolvers.c.issue_key == issue_row["issue_key"]
                )
            )
            if resolver_rows:
                conn.execute(self.resolution_resolvers.insert(), resolver_rows)


def extract_resolution_data(comment_body: str):
    """Extracts structured fields from a comment body. Returns None if not present."""
    block = BLOCK_REGEX.search(comment_body)
    if not block:
        return None

    data = block.groupdict()

    newcat_match = OPTIONAL_NEW_CATEGORY.search(comment_body)
    data["new_category"] = newcat_match.group("newcat").strip() if newcat_match else None

    resolver_match = RESOLVER_REGEX.search(comment_body)
    if resolver_match:
        resolvers = resolver_match.group("resolver_line").strip()
        resolver_list = [r.strip() for r in resolvers.split(",") if r.strip()]
        data["resolvers"] = resolver_list
        data["primary_resolver"] = resolver_list[0] if resolver_list else None
        data["resolve_helpers"] = resolver_list[1:] if len(resolver_list) > 1 else []
    else:
        data["resolvers"] = []
        data["primary_resolver"] = None
        data["resolve_helpers"] = []

    outcome_match = OUTCOME_REGEX.search(comment_body)
    data["outcome"] = outcome_match.group("outcome").strip() if outcome_match else None

    for key in ["category", "summary", "rationale"]:
        if data.get(key):
            data[key] = data[key].strip()

    return data


def query_jira_issues(jql: str):
    """Fetch issues matching the JQL using the /search/jql endpoint with pagination."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    if session is None:
        raise RuntimeError("Session not initialized")
    issues: list[dict] = []
    next_page_token: str | None = None

    while True:
        payload = {
            "jql": jql,
            "maxResults": MAX_RESULTS_PER_PAGE,
            "fields": ["*all"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        response = request_with_retry("POST", url, json=payload)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Jira search failed ({response.status_code}): {response.text}"
            ) from exc

        data = response.json()
        page_issues = data.get("issues")
        next_page_token = data.get("nextPageToken")

        if page_issues is None:
            results = data.get("results", [])
            if results:
                page_issues = results[0].get("issues", [])
                next_page_token = results[0].get("nextPageToken")
            else:
                page_issues = []
                next_page_token = None

        issues.extend(page_issues)

        if not next_page_token or not page_issues:
            break

    return issues


def get_comments(issue_key):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    if session is None:
        raise RuntimeError("Session not initialized")
    r = request_with_retry("GET", url, params={"expand": "renderedBody"})
    r.raise_for_status()
    comments = r.json().get("comments", [])
    return comments[-MAX_COMMENTS:]  # latest few comments


def simple_value(val):
    """Return a simplified display value for Jira field entries."""
    if isinstance(val, dict):
        # Handle Jira Service Management request types nested under requestType
        if "requestType" in val:
            return simple_value(val.get("requestType"))
        for key in ("displayName", "name", "value", "text", "summary"):
            if key in val and val.get(key) is not None:
                return val.get(key)
        # Fall back to any stringifiable identifier
        for key in ("id", "key"):
            if key in val and val.get(key) is not None:
                return str(val.get(key))
        return str(val)
    if isinstance(val, list):
        return ", ".join(str(simple_value(v)) for v in val)
    return val


def adf_to_text(node):
    """Return plain text from an Atlassian Document Format structure."""

    def _inner(n):
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


def map_resolver(name: str, aliases: ResolverAliasList) -> tuple[str, int] | None:
    """Map resolver alias to canonical name and level using priority list."""
    name_cf = name.casefold().strip()
    for alias in aliases:
        if alias["alias_cf"] in name_cf:
            return str(alias["canonical_name"]), int(alias["level"])
    return None


def map_resolvers_from_text(text: str, aliases: ResolverAliasList) -> list[tuple[str, int]]:
    """Map aliases found in text in order of appearance, using priority to break ties."""
    lower = text.casefold()
    hits: list[tuple[int, int, str, int]] = []
    for alias in aliases:
        alias_cf = str(alias["alias_cf"])
        pos = lower.find(alias_cf)
        if pos != -1:
            hits.append(
                (
                    pos,
                    int(alias["priority"]),
                    str(alias["canonical_name"]),
                    int(alias["level"]),
                )
            )
    hits.sort(key=lambda x: (x[0], x[1]))  # order of appearance, then alias priority

    ordered: list[tuple[str, int]] = []
    seen = set()
    for _, _, real, level in hits:
        if real not in seen:
            ordered.append((real, level))
            seen.add(real)

    return ordered


def request_with_retry(method: str, url: str, max_attempts: int = 5, **kwargs) -> requests.Response:
    """Perform HTTP request with simple backoff on 429."""
    if session is None:
        raise RuntimeError("Session not initialized")
    attempt = 0
    while True:
        attempt += 1
        resp = session.request(method, url, **kwargs)
        if resp.status_code != 429 or attempt >= max_attempts:
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = int(retry_after)
        except (TypeError, ValueError):
            delay = 5
        delay = max(1, min(delay, 60))
        time.sleep(delay)


# ------------------------------
# Excel Setup and main
# ------------------------------
def main():
    global JIRA_BASE_URL, session

    parser = argparse.ArgumentParser(description="Export Jira resolution summaries to SQLite.")
    parser.add_argument("--email", required=True, help="Jira user email (for basic auth)")
    parser.add_argument("--token", required=True, help="Jira API token")
    parser.add_argument("--url", default=JIRA_BASE_URL, help="Jira base URL")
    parser.add_argument(
        "--start-date",
        help="Start date (YYYY-MM-DD). Defaults to 7 days ago (excluding today).",
    )
    parser.add_argument(
        "--end-date",
        help="End date (YYYY-MM-DD). Defaults to today (exclusive).",
    )
    parser.add_argument(
        "--jql",
        help="Override the generated JQL entirely. If set, start/end date options are ignored.",
    )
    parser.add_argument(
        "--db-path",
        default="resolution_reports.sqlite",
        help="Path to the SQLite database file.",
    )
    args = parser.parse_args()

    JIRA_BASE_URL = args.url.rstrip("/")

    session = requests.Session()
    session.auth = (args.email, args.token)
    session.headers.update({"Accept": "application/json"})

    def parse_date(val: str) -> date:
        return datetime.strptime(val, "%Y-%m-%d").date()

    today = date.today()
    default_start = today - timedelta(days=7)
    default_end = today

    start_date = parse_date(args.start_date) if args.start_date else default_start
    end_date = parse_date(args.end_date) if args.end_date else default_end

    if args.jql:
        jql = args.jql
    else:
        jql = JQL_TEMPLATE.format(
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
        )

    db = ResolutionDatabase(Path(args.db_path))
    resolver_aliases = db.load_resolver_aliases()

    # Process Jira issues
    issues = query_jira_issues(jql)

    for issue in issues:
        key = issue["key"]
        fields = issue["fields"]

        comments = get_comments(key)
        extracted = None

        for comment in comments:
            raw_body = comment.get("body") or ""
            rendered = comment.get("renderedBody")
            body = ""
            if isinstance(raw_body, dict):
                body = adf_to_text(raw_body)
            else:
                body = str(raw_body)
            if not body and rendered:
                body = str(rendered)

            if "Resolution Summary by Rovo:" in body:
                extracted = extract_resolution_data(body)
                if extracted:
                    break

        if not extracted:
            extracted = {
                "category": None,
                "summary": None,
                "rationale": None,
                "new_category": None,
                "primary_resolver": None,
                "resolve_helpers": None,
                "outcome": None,
                "resolvers": [],
            }
        else:
            extracted.setdefault("resolvers", extracted.get("resolvers", []))

        request_type = simple_value(
            fields.get("customfield_10010")
            or fields.get("requestType")
            or fields.get("request_type")
        )
        organizations = simple_value(fields.get("customfield_10002") or fields.get("organizations"))
        work_category = simple_value(fields.get("customfield_10194") or fields.get("work_category"))

        # Resolver mapping (only consider extracted resolvers list, not other text)
        resolvers_list = extracted.get("resolvers") or []
        resolvers_raw = ", ".join(resolvers_list)
        mapped_order: list[tuple[str, int]] = []
        for name in resolvers_list:
            mapped = map_resolvers_from_text(name, resolver_aliases)
            mapped_order.extend(mapped)

        extracted_primary = mapped_order[0][0] if mapped_order else None
        extracted_primary_level = mapped_order[0][1] if mapped_order else None
        resolve_helpers_mapped = [n for n, _ in mapped_order[1:]] if len(mapped_order) > 1 else []
        all_levels = [lvl for _, lvl in mapped_order]
        escalated_level = max(all_levels) if all_levels else None

        # Outcome normalization: only keep leading Abandoned/Solved/Not Solved, else keep original
        outcome_val = extracted.get("outcome")
        if isinstance(outcome_val, str):
            trimmed = outcome_val.strip()
            lowered = trimmed.casefold()
            if lowered.startswith("abandoned"):
                outcome_val = "Abandoned"
            elif lowered.startswith("solved"):
                outcome_val = "Solved"
            elif lowered.startswith("not solved"):
                outcome_val = "Not Solved"
            else:
                outcome_val = trimmed

        issue_row = {
            "issue_key": key,
            "issue_id": issue.get("id"),
            "summary": fields.get("summary"),
            "status": fields.get("status", {}).get("name"),
            "issuetype": fields.get("issuetype", {}).get("name"),
            "priority": fields.get("priority", {}).get("name"),
            "request_type": request_type,
            "organizations": organizations,
            "resolvers_raw": resolvers_raw,
            "primary_resolver": extracted_primary,
            "resolve_helpers": "; ".join(resolve_helpers_mapped) if resolve_helpers_mapped else None,
            "escalated_to_level": escalated_level,
            "solved_level": extracted_primary_level,
            "outcome": outcome_val,
            "work_category": work_category,
            "category": extracted["category"],
            "new_category": extracted["new_category"],
            "summary_block": extracted["summary"],
            "rationale": extracted["rationale"],
            "updated_at": datetime.now(),
        }

        resolver_rows = [
            {
                "issue_key": key,
                "resolver": resolver_name,
                "resolver_index": idx + 1,
                "resolver_level": level,
            }
            for idx, (resolver_name, level) in enumerate(mapped_order)
        ]

        db.upsert_issue(issue_row, resolver_rows)

    print(f"Done. Upserted {len(issues)} issues into {args.db_path}")


if __name__ == "__main__":
    main()
