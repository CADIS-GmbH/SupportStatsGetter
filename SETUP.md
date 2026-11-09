# Setup and run

Step-by-step instructions for setting up SupportStatsGetter on Windows. For a
description of all settings, tables and views see [README.md](README.md).

## 1. Prerequisites

- **Python 3.13** (newer versions lack prebuilt packages for some dependencies) – download from <https://www.python.org/downloads/>.
  Not needed on computers that run the .exe version (see step 2).
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
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This creates a private Python environment in `.venv` and installs the
required packages into it.

### Without Python: the .exe version

On a computer without Python, use the standalone programs instead. On a
computer *with* the Python setup above, run `build_exe.bat`; it creates the
folder `dist\SupportStatsGetter` with

- `SupportStatsGetter.exe` – the program itself, instead of
  `.\.venv\Scripts\python.exe SupportStatsGetter.py`
- `find_custom_fields.exe` – instead of `.\.venv\Scripts\python.exe find_custom_fields.py`
- `run.bat` – starts `SupportStatsGetter.exe` in its own folder
- `config.example.yaml` – set up for this folder: the database
  (`support_stats.sqlite`) and the log stay next to the exe, the Excel reports
  go to the `reports` subfolder (created by `run.bat`)
- this guide.

Copy that folder to the other computer, e.g. `C:\SupportStatsGetter`, and
start `run.bat`. The first time it creates `config.yaml` and opens it in
Notepad: fill in the settings as described in step 3 (`find_custom_fields.exe
config.yaml` shows your custom field IDs), save, and start `run.bat` again.

The Jira email and API token are not in this `config.yaml` (`jira_user` and
`jira_token` are `null`), so the folder can be shared. When `run.bat` finds
neither in the config nor in the environment variables
`SUPPORTSTATS_JIRA_USER` / `SUPPORTSTATS_JIRA_TOKEN`, it asks for them (the
token is typed hidden) and saves them as environment variables of your Windows
user, so every person running it uses their own login and is asked only once.
To change them later run `setx SUPPORTSTATS_JIRA_TOKEN "<new token>"` in a
terminal, or delete them under *Edit environment variables for your account*
in the Windows settings and start `run.bat` again. A value in `config.yaml`
wins over the environment variables.

Atlassian API tokens expire (at the latest after a year). When Jira rejects the
login, the program stops with exit code 32 and writes no Excel report from old
data, so a scheduled run shows up as failed. `run.bat` then forgets the saved
email and token, asks for them again and repeats the run; in the Task Scheduler
nobody can answer, so the next run you start by hand asks.

Options are passed on, e.g. `run.bat --start-date 2025-01-01` for the first
download (step 4) or `run.bat --recalculate-all`. For regular runs (step 6),
schedule `run.bat` itself; it needs no "Start in" folder and only waits for a
key when something fails.

You can also call `SupportStatsGetter.exe` directly with the same options as
the script. It finds `config.yaml` in the current folder or next to itself;
relative paths in the config are relative to the current folder.
Rebuild the exe after updating the scripts.

## 3. Create the configuration

Copy the example and open it in an editor:

```powershell
Copy-Item config.example.yaml config.yaml
notepad config.yaml
```

Every option has a comment explaining what it does and its default. Replace the
placeholder values with your own, at least:

| Setting | What to enter |
| --- | --- |
| `jira_base_url` | Your Jira URL, e.g. `https://yourcompany.atlassian.net` |
| `jira_user` / `jira_token` | The Jira user's email address and the API token, or leave them empty and use environment variables (see below) |
| `base_jql` | Which tickets to collect, e.g. `project = "SUP"` (your project key) |
| `db_path` | Where the database is stored, e.g. `C:\SupportStats\support_stats.sqlite` |
| `output_excel` | Where the Excel report is written, e.g. `C:\SupportStats\SupportStats.xlsx` |
| `log_file` | Log file location |
| `custom_fields` | The custom field IDs of **your** Jira (see below). Remove entries you don't have. |
| `non_support_request_types` | Request types that should not count as support, or `[]` |
| `support_issue_types` | Your support issue type name(s), e.g. `["Support"]` or `["Service Request", "Incident"]` |
| `request_type_categories` / `request_type_category_default` | How tickets are grouped in the reports by issue type, request type and/or label; default one group per issue type, `{}` / `null` for no grouping |
| `priority_positions` | Your Jira priority names; positions above 0 count as high priority |
| `closed_statuses`, `cancelled_statuses`, `resolution_time_excluded_statuses`, `no_contact_ignore_statuses` | Your workflow's status names (case doesn't matter); see the README for what each list does. Leave them out to use the defaults. |

Notes:

- Put Windows paths in **single** quotes:
  `db_path: 'C:\SupportStats\support_stats.sqlite'`. In double quotes a
  backslash starts an escape (`\t` is a tab), so the path would break.
- The folders for the database, Excel file and log must exist; create them
  first, e.g. `New-Item -ItemType Directory -Force C:\SupportStats`.
- `config.yaml` contains your API token. Don't share it. To share a config
  without the token, set `jira_token: null` (and `jira_user: null` if everyone
  uses their own account); each user then stores theirs once in a terminal:

  ```
  setx SUPPORTSTATS_JIRA_TOKEN "<your API token>"
  setx SUPPORTSTATS_JIRA_USER "you@example.com"
  ```

  `setx` saves them for your Windows user; windows opened afterwards see them.
  A value in the config file wins over the environment variable.
- An older `config.json` still works; pass it with `-c config.json`.

**Finding custom field IDs:** open this URL in a browser while logged in to
Jira: `https://yourcompany.atlassian.net/rest/api/3/field`. Search for the
field name (e.g. "Request Type", "Satisfaction", "Organizations") and use its
`id` value, e.g. `customfield_10010`.

## 4. First run

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.yaml
```

The first run creates the database and downloads **all** tickets that match
`base_jql`, including comments, worklogs and history. Depending on the number
of tickets this can take from minutes to hours. Progress is written to the
console and to the log file. When it finishes, the Excel report is written to
`output_excel`.

To skip old history, pass a start date. Only tickets updated on or after that
date are downloaded:

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.yaml --start-date 2025-01-01
```

The start date only applies to a new, empty database. Later runs fetch the
updates since the last run as usual, so the option can stay in a batch file.

The first run can take hours. Tickets are processed oldest change first and
saved every 50 tickets. Dropped connections and Jira server errors are retried
a few times. If the run still stops, start it again with the same command: it
continues after the last saved ticket instead of starting over.

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
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.yaml --recalculate-all
```

Run with `--recalculate-all` again whenever you change these tables or the
configuration.

## 6. Regular runs

Later runs only download tickets that changed since the last run, so they are
quick:

```powershell
.\.venv\Scripts\python.exe SupportStatsGetter.py -c config.yaml
```

To run it automatically, adjust the folder in `GetSupport_KPI_example.bat`
and add it to the Windows Task Scheduler, e.g. daily in the early morning.

Close the Excel report before a run; Excel locks the file and the export fails
otherwise.

## 7. Result

The first sheet, `Dashboard`, shows the KPIs as charts and a table. Pick the
start quarter, the granularity (week, month, quarter) and whether the current
period is included in the yellow cells; everything else follows. The default
start quarter can be set with `dashboard_start_quarter` in the config.

After the dashboard, the workbook has one sheet per database view. The most important ones:

- `weekly_support_summary`, `monthly_support_summary`,
  `quarterly_support_summary` – the KPIs per period: new, closed, open and
  aged tickets, escalations, first reply time, resolution time, CSAT.
- `customer_weekly_summary` – new and closed tickets and logged hours per
  customer, week and request type category.
- `jira_tickets_reporting` – one row per ticket with all KPI fields.
- `jira_calendar_reporting` – one row per ticket and day.

## Troubleshooting

| Problem | Cause / solution |
| --- | --- |
| `401 Unauthorized` / "Jira rejected the login" in the log | Wrong `jira_user` or an expired or revoked `jira_token`: create a new token and enter it (`run.bat` asks), set `SUPPORTSTATS_JIRA_TOKEN` or fix the config. |
| `400 Bad Request` on the search | `base_jql` is invalid; test it in Jira's issue search first. |
| `FileNotFoundError` or `unable to open database file` | The folder in `log_file` or `db_path` does not exist. |
| `Permission denied` on the Excel file | The workbook is open in Excel; close it. |
| Summary views are all 0 | `support_issue_types` does not match your issue type names, or `cancelled_statuses` / `closed_statuses` don't match your workflow. |
| No first response times | `internal_users` is empty or does not match your agents' email addresses (step 5). |
| Request type, satisfaction, ... empty | Wrong or missing `custom_fields` IDs (step 3). |
