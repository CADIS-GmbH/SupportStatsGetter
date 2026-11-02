# Setup and run

Step-by-step instructions for setting up SupportStatsGetter on Windows. For a
description of all settings, tables and views see [README.md](README.md).

## 1. Prerequisites

- **Python 3.11 or newer** – download from <https://www.python.org/downloads/>.
  During installation tick *"Add python.exe to PATH"*.
- **A Jira Service Management Cloud site** and a Jira user that can see all
  tickets of the service desk project, including internal comments (an agent
  account).
- **A Jira API token** for that user – create one at
  <https://id.atlassian.com/manage-profile/security/api-tokens>.
- Optional: [DB Browser for SQLite](https://sqlitebrowser.org/) to edit the
  lookup tables (step 5).

## 2. Unpack and install

Unpack the zip, e.g. to `C:\SupportStatsGetter`, then open PowerShell:

```powershell
cd C:\SupportStatsGetter
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This creates a private Python environment in `.venv` and installs the
required packages into it.

## 3. Create the configuration

Copy the example and open it in an editor:

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

The example contains the settings of another company. Replace **every** value
with your own:

| Setting | What to enter |
| --- | --- |
| `jira_base_url` | Your Jira URL, e.g. `https://yourcompany.atlassian.net` |
| `jira_user` / `jira_token` | The Jira user's email address and the API token |
| `base_jql` | Which tickets to collect, e.g. `project = "SUP"` (your project key) |
| `db_path` | Where the database is stored, e.g. `C:\\SupportStats\\support_stats.sqlite` |
| `output_excel` | Where the Excel report is written, e.g. `C:\\SupportStats\\SupportStats.xlsx` |
| `log_file` | Log file location |
| `custom_fields` | The custom field IDs of **your** Jira (see below). Remove entries you don't have. |
| `non_support_request_types` | Request types that should not count as support, or `[]` |
| `support_issue_types` | Your support issue type name(s), e.g. `["Support"]` or `["Service Request", "Incident"]` |
| `request_type_categories` / `request_type_category_default` | How request types are grouped in the reports, or `{}` / `null` for no grouping |
| `priority_positions` | Your Jira priority names; positions above 0 count as high priority |
| `closed_statuses`, `cancelled_statuses`, `resolution_time_excluded_statuses`, `no_contact_ignore_statuses` | Your workflow's status names (case doesn't matter); see the README for what each list does. Leave them out to use the defaults. |

Notes:

- In JSON, backslashes in paths must be doubled: `C:\\SupportStats\\...`.
- The folders for the database, Excel file and log must exist; create them
  first, e.g. `New-Item -ItemType Directory -Force C:\SupportStats`.
- `config.json` contains your API token. Don't share it.

**Finding custom field IDs:** open this URL in a browser while logged in to
Jira: `https://yourcompany.atlassian.net/rest/api/3/field`. Search for the
field name (e.g. "Request Type", "Satisfaction", "Organizations") and use its
`id` value, e.g. `customfield_10010`.

## 4. First run

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.json
```

The first run creates the database and downloads **all** tickets that match
`base_jql`, including comments, worklogs and history. Depending on the number
of tickets this can take from minutes to hours. Progress is written to the
console and to the log file. When it finishes, the Excel report is written to
`output_excel`.

## 5. Fill the lookup tables

Some settings are stored in the database, which exists after the first run.
Open the database file with DB Browser for SQLite (tab *Browse Data*) and add
rows to these tables:

- **`internal_users`** – one regular expression per row that matches the email
  addresses of your support agents, e.g. `@yourcompany\.com$`. **This is
  required** for correct first and last response times: only public comments
  by matching authors count as responses.
- **`org_lookup`** (optional) – maps Jira organization names to a primary
  organization: `regex` is matched against the organization name,
  `primary_org` is the result, and the row with the highest `priority` wins.
- **`week_reporting`** – assigns calendar weeks to reporting months and
  quarters. Weeks are filled in automatically from the oldest ticket to the
  current year; you only need to edit rows to change the month assignment.

Click *Write Changes*, close DB Browser, then recalculate all tickets so the
new settings are applied:

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.json --recalculate-all
```

Run with `--recalculate-all` again whenever you change these tables or the
configuration.

## 6. Regular runs

Later runs only download tickets that changed since the last run, so they are
quick:

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.json
```

To run it automatically, adjust the folder in `GetSupport_KPI_example.bat`
and add it to the Windows Task Scheduler, e.g. daily in the early morning.

Close the Excel report before a run; Excel locks the file and the export fails
otherwise.

## 7. Result

The Excel workbook has one sheet per database view. The most important ones:

- `weekly_support_summary`, `monthly_support_summary`,
  `quarterly_support_summary` – the KPIs per period: new, closed, open and
  aged tickets, escalations, first reply time, resolution time, CSAT.
- `jira_tickets_reporting` – one row per ticket with all KPI fields.
- `jira_calendar_reporting` – one row per ticket and day.

## Troubleshooting

| Problem | Cause / solution |
| --- | --- |
| `401 Unauthorized` in the log | Wrong `jira_user` or `jira_token`. |
| `400 Bad Request` on the search | `base_jql` is invalid; test it in Jira's issue search first. |
| `FileNotFoundError` or `unable to open database file` | The folder in `log_file` or `db_path` does not exist. |
| `Permission denied` on the Excel file | The workbook is open in Excel; close it. |
| Summary views are all 0 | `support_issue_types` does not match your issue type names, or `cancelled_statuses` / `closed_statuses` don't match your workflow. |
| No first response times | `internal_users` is empty or does not match your agents' email addresses (step 5). |
| Request type, satisfaction, ... empty | Wrong or missing `custom_fields` IDs (step 3). |
