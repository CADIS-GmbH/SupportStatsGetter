import argparse
import re
import time
from datetime import datetime, date, timedelta
import requests
import openpyxl
from openpyxl import Workbook

JIRA_BASE_URL = "https://cadissoftware.atlassian.net"
JQL_TEMPLATE = (
    'project = "Customer Service" AND status in (Closed, Resolved) '
    'AND resolutiondate >= "{start}" AND resolutiondate < "{end}"'
)

MAX_COMMENTS = 3
MAX_RESULTS_PER_PAGE = 100
OUTPUT_FILE = f"jira_resolution_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

session: requests.Session | None = None

# Resolver alias lookup in priority order: (alias, real name, level)
RESOLVER_ALIASES: list[tuple[str, str, int]] = [
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


def map_resolver(name: str) -> tuple[str, int] | None:
    """Map resolver alias to canonical name and level using priority list."""
    name_cf = name.casefold().strip()
    for alias, real, level in RESOLVER_ALIASES:
        alias_cf = alias.casefold().strip()
        if alias_cf in name_cf:
            return real, level
    return None


def map_resolvers_from_text(text: str) -> list[tuple[str, int]]:
    """Map aliases found in text in order of appearance, using priority to break ties."""
    lower = text.casefold()
    hits: list[tuple[int, int, str, int]] = []
    for idx, (alias, real, level) in enumerate(RESOLVER_ALIASES):
        pos = lower.find(alias.casefold().strip())
        if pos != -1:
            hits.append((pos, idx, real, level))
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

    parser = argparse.ArgumentParser(description="Export Jira resolution summaries.")
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

    # Excel Setup
    wb = Workbook()
    ws = wb.active
    ws.title = "Jira Rovo Extract"

    ws.append(
        [
            "key",
            "summary",
            "status",
            "issuetype",
            "priority",
            "request_type",
            "organizations",
            "resolvers_raw",
            "primary_resolver",
            "resolve_helpers",
            "escalated_to_level",
            "solved_level",
            "outcome",
            "work_category",
            "category",
            "new_category",
            "summary_block",
            "rationale",
        ]
    )

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
            mapped = map_resolvers_from_text(name)
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

        ws.append(
            [
                key,
                fields.get("summary"),
                fields.get("status", {}).get("name"),
                fields.get("issuetype", {}).get("name"),
                fields.get("priority", {}).get("name"),
                request_type,
                organizations,
                resolvers_raw,
                extracted_primary,
                "; ".join(resolve_helpers_mapped) if resolve_helpers_mapped else None,
                escalated_level,
                extracted_primary_level,
                outcome_val,
                work_category,
                extracted["category"],
                extracted["new_category"],
                extracted["summary"],
                extracted["rationale"],
            ]
        )

    wb.save(OUTPUT_FILE)
    print(f"Done. Exported {len(issues)} issues -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
