r"""Look up the Jira custom field IDs this tool needs and test the connection.

Usage:  .venv\Scripts\python.exe find_custom_fields.py [config.yaml]

Reads jira_base_url / jira_user / jira_token from the config (empty user or
token: the environment variables SUPPORTSTATS_JIRA_USER /
SUPPORTSTATS_JIRA_TOKEN), checks the login
and the base_jql, and prints a ready-to-paste custom_fields block.
"""
import os
import sys

import requests
import yaml

# logical name -> (Jira Service Management field type, field names to fall back
# on in lower case). The type wins: a site can have other fields with the same name.
WANTED = {
    "request_type": ("com.atlassian.servicedesk:vp-origin",
                     ["customer request type", "request type"]),
    "organizations": ("com.atlassian.servicedesk:sd-customer-organizations",
                      ["organizations", "organisations"]),
    "request_participants": ("com.atlassian.servicedesk:sd-request-participants",
                             ["request participants"]),
    "satisfaction": ("com.atlassian.servicedesk:sd-request-feedback", ["satisfaction"]),
    "satisfaction_ts": ("com.atlassian.servicedesk:sd-request-feedback-date",
                        ["satisfaction date"]),
    "sentiment": ("com.atlassian.servicedesk.sentiment:sd-sentiment", ["sentiment"]),
}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    base = (cfg.get("jira_base_url") or "").rstrip("/")
    auth = (
        cfg.get("jira_user") or os.environ.get("SUPPORTSTATS_JIRA_USER") or "",
        cfg.get("jira_token") or os.environ.get("SUPPORTSTATS_JIRA_TOKEN") or "",
    )
    if not base or "your-site" in base or "<" in auth[1]:
        print(
            "Fill in jira_base_url in", path, "and jira_user / jira_token there or "
            "in the environment variables SUPPORTSTATS_JIRA_USER / "
            "SUPPORTSTATS_JIRA_TOKEN first."
        )
        return 1

    me = requests.get(f"{base}/rest/api/3/myself", auth=auth, timeout=30)
    if me.status_code != 200:
        print(f"Login failed ({me.status_code}). Check jira_user / jira_token.")
        return 1
    print("Connected as:", me.json().get("displayName"))

    jql = cfg.get("base_jql") or ""
    r = requests.post(f"{base}/rest/api/3/search/approximate-count",
                      json={"jql": jql}, auth=auth, timeout=30)
    if r.status_code == 200:
        print(f"base_jql OK: about {r.json().get('count')} tickets match '{jql}'")
    else:
        print(f"base_jql problem ({r.status_code}): {r.text[:300]}")

    fields = requests.get(f"{base}/rest/api/3/field", auth=auth, timeout=30).json()
    by_name = {}
    by_type = {}
    for fld in fields:
        by_name.setdefault(fld.get("name", "").strip().lower(), []).append(fld["id"])
        custom_type = (fld.get("schema") or {}).get("custom")
        if custom_type:
            by_type.setdefault(custom_type, []).append(fld["id"])

    print("\nPaste this into config.yaml (replace the custom_fields block):\n")
    print("custom_fields:")
    for key, (custom_type, names) in WANTED.items():
        ids = by_type.get(custom_type) or next(
            (by_name[n] for n in names if n in by_name), None
        )
        if ids:
            print(f"  {key}: {ids[0]}")
        else:
            print(f"  # {key}: not found in your Jira - leave out")
    print("  resolution: resolution")
    return 0


if __name__ == "__main__":
    sys.exit(main())
