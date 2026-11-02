# SupportStatsGetter

This project collects ticket statistics from **Jira Service Management** and stores them in a normalized SQLite database. It calculates support KPIs and can export all reporting views to an Excel workbook.

For step-by-step setup instructions see [SETUP.md](SETUP.md).

## Usage

1. Create a `config.json` file with your Jira credentials. You can optionally specify a base JQL query to limit the scope of fetched tickets. A sample configuration is provided as `config.example.json`:
```json
{
  "jira_base_url": "https://example.atlassian.net",
  "jira_user": "your-email@example.com",
  "jira_token": "api-token",
  "db_path": "support_stats.sqlite",
  "base_jql": "project = DEMO",
  "log_level": "INFO",
  "log_file": "support_stats.log",
  "output_excel": "SupportStats.xlsx",
  "excel_write_mode": "overwrite_with_backup",
  "custom_fields": {
    "request_type": "customfield_10010",
    "deployment_type": "customfield_10011",
    "organizations": "customfield_10002",
    "satisfaction": "customfield_10012",
    "satisfaction_ts": "customfield_10013"
  },
  "non_support_request_types": [
    "Licensing and billing questions"
  ],
  "support_issue_types": [
    "Support"
  ],
  "request_type_categories": {
    "HW": ["%Hardware%"]
  },
  "request_type_category_default": "SW",
  "closed_statuses": ["Resolved", "Closed", "Cancelled"],
  "cancelled_statuses": ["Cancelled"],
  "resolution_time_excluded_statuses": [
    "Closed",
    "Cancelled",
    "Resolved",
    "Waiting for customer",
    "Pending"
  ],
  "no_contact_ignore_statuses": ["Closed", "Resolved", "Cancelled", "Pending"],
  "priority_deescalation_ignore_minutes": 5,
  "priority_positions": {
    "Lowest": -200,
    "Low": -100,
    "Medium": 0,
    "High": 100,
    "Highest": 200
  }
}
```
2. Install the dependencies:
```bash
pip install -r requirements.txt
```
3. Run the script (optionally pass `--config` to specify the file and `--log-level` to override the configured log level):
```bash
python SupportStatsGetter.py --config config.json --log-level DEBUG
```
Fetched tickets will be stored in the SQLite database specified in the config file.
Subsequent runs only request tickets updated after the newest record already stored.
Use `--recalculate-all` to recalculate the KPI fields for every stored ticket,
for example after changing the configuration.

## Configuration

| Key | Description |
| --- | --- |
| `jira_base_url`, `jira_user`, `jira_token` | Jira Cloud URL and API credentials. |
| `db_path` | Path of the SQLite database. |
| `base_jql` | JQL that limits which tickets are fetched, e.g. `project = DEMO`. |
| `log_level`, `log_file` | Logging settings. |
| `output_excel`, `excel_write_mode` | Excel export target and overwrite behaviour (see below). |
| `custom_fields` | Maps logical field names to the Jira custom field IDs of your instance. Supported names: `request_type`, `deployment_type`, `organizations`, `satisfaction`, `satisfaction_ts`, `work_category`, `sentiment`, `request_participants`. |
| `non_support_request_types` | Request types that are excluded from the support KPIs in the summary views. Default: none. |
| `support_issue_types` | Issue types counted in the summary views (case-insensitive). Default: `["Support"]`. An empty list counts all issue types. |
| `request_type_categories` | Groups request types into categories for the summary views. Each category has a list of SQL `LIKE` patterns (`%` = any text, case-insensitive) matched against the request type; the first matching category wins. The summary views get an `open_<category>` and a `closed_<category>` column per category. Default: `{"HW": ["%Hardware%"]}`. |
| `request_type_category_default` | Category for request types that match no pattern. Default: `SW`. Set to `null` together with an empty `request_type_categories` to drop the category columns. |
| `priority_positions` | Maps Jira priority names to a numeric position. Positions above `0` count as high priority, and a raise to a position above `0` counts as an escalation. Unknown priorities get `0`. Defaults to the standard Jira priorities shown above. |
| `closed_statuses` | Statuses in which a ticket counts as closed: sets the resolved date and stops the open-day count, and closed tickets are left out of the open ticket counts. Default: `["Resolved", "Closed", "Cancelled"]`. |
| `cancelled_statuses` | Statuses that mark a ticket as cancelled (`is_junk`); these tickets are left out of the summary views. Default: `["Cancelled"]`. |
| `resolution_time_excluded_statuses` | Days that start in one of these statuses do not count towards the adjusted resolution time. Default: `["Closed", "Cancelled", "Resolved", "Waiting for customer", "Pending"]`. |
| `no_contact_ignore_statuses` | Tickets in these statuses are not counted in the "over 5 days no contact" KPI. Default: `["Closed", "Resolved", "Cancelled", "Pending"]`. |
| `priority_deescalation_ignore_minutes` | A priority raise that is reverted within this many minutes is not counted as an escalation. |

### Lookup tables

Some settings live in database tables so they can be maintained without changing the config. Fill them with any SQLite tool:

- `internal_users` (`pattern`) – regular expressions matched against author email addresses. Matching authors count as agents, which is used to calculate first and last response times.
- `org_lookup` (`regex`, `primary_org`, `priority`) – maps Jira organization names to a primary organization. When several match, the highest priority wins.
- `week_reporting` (`calendar_week`, `reporting_month`, `reporting_quarter`) – assigns ISO weeks (`YYYY.WW`) to reporting months and quarters. The summary views only show weeks listed here. Weeks are added automatically for every year from the oldest stored ticket to the current year (weeks 1–5 → month 1, 6–9 → 2, 10–13 → 3, and so on); rows you edit are kept.

## Excel export

Setting `output_excel` in the config file writes all database views to an Excel
workbook. The command line option `--output-excel [filename]` overrides this
setting.

The behaviour when the file already exists is controlled by `excel_write_mode`:
- `overwrite_with_backup` – overwrite the entire file but create a timestamped backup (default).
- `overwrite` – replace the entire file without creating a backup.
- `no_overwrite` – abort if the target exists.

## Database tables

Comments and worklogs for each ticket are downloaded and saved alongside the tickets.
Comments include a `public` flag to indicate visibility in the service desk.

Each comment is stored in the `jira_comments` table with the following columns:

- `id` - the Jira comment ID
- `ticket_id` - internal issue ID
- `issue_key` - the human readable issue key
- `author` - display name of the comment author
- `author_email` - email of the comment author
- `type` - value of the body type returned by Jira
- `body` - the comment text rendered as HTML
- `public` - whether the comment is visible to the customer
- `internal_user` - whether the author is an agent (see `internal_users`)
- `created` / `updated` - timestamps

Worklog entries are stored in the `jira_worklogs` table with the following columns:

- `id` - the Jira worklog ID
- `ticket_id` - internal issue ID
- `author` - display name of the worklog author
- `author_email` - email of the worklog author
- `time_spent_minutes` - duration in minutes
- `started` / `created` - timestamps
- `text` - plain text of the worklog comment
- `started_week` / `created_week` - ISO week strings

Changelog entries are stored in the `jira_changelog` table. Each row represents
a single field change from the issue history:

- `id` - combination of the history id and item index
- `history_id` - identifier of the history entry returned by Jira
- `ticket_id` - internal issue ID
- `author` - display name of the user who made the change
- `author_email` - email of the user who made the change
- `created` - timestamp of the change
- `field` - the field that changed
- `fieldtype` - Jira field type
- `from_value` / `to_value` - raw values as provided by the API
- `from_string` / `to_string` - human readable values

Daily statistics for each ticket are stored in the `jira_calendar` table:

- `record_id` - combination of ticket key and date
- `ticket_id` / `ticket_key` - internal issue id and issue key
- `date` - day the row represents
- `comments` / `public_comments` - counts of comments on that day
- `start_assigned_user` / `end_assigned_user` - assigned user at start and end of the day
- `start_assigned_user_email` / `end_assigned_user_email` - email addresses of the assigned users
- `start_status` / `end_status` - status at the start and end of the day
- `start_priority_txt` / `end_priority_txt` and `start_priority_pos` / `end_priority_pos` - priority at start and end of the day
- `did_escalate_priority` - whether the priority was raised that day
- `time_spent_min` - minutes logged via worklogs for the day
- `total_days_open`, `weekdays_days_since_last_contact`, `adjusted_resolution_days` - running counters
- `cw` / `weekday` - calendar week and ISO weekday

## Reporting views

The database contains views that join the tables with the reporting weeks, including
`jira_tickets_reporting`, `jira_calendar_reporting`, `jira_worklogs_reporting` and the
`weekly_support_summary`, `monthly_support_summary` and `quarterly_support_summary`
KPI views. All views are written to the Excel export.
