# SupportStatsGetter

This project collects ticket statistics from **Jira Service Management** and stores them in a normalized SQLite database. It calculates support KPIs and can export all reporting views to an Excel workbook.

For step-by-step setup instructions see [SETUP.md](SETUP.md).

## Usage

1. Copy `config.example.yaml` to `config.yaml` and fill in your Jira site, credentials and custom field IDs. The example lists every option with a comment explaining it and its default; options you leave out use the default. Existing `config.json` files keep working.
2. Install the dependencies:
```bash
pip install -r requirements.txt
```
3. Run the script. Without `--config` it reads `config.yaml`, or `config.json` if there is no `config.yaml`; `--log-level` overrides the configured log level:
```bash
python SupportStatsGetter.py --config config.yaml --log-level DEBUG
```
Fetched tickets will be stored in the SQLite database specified in the config file.
Subsequent runs only request tickets updated after the newest record already stored.
For a new database, `--start-date YYYY-MM-DD` limits the first download to
tickets updated on or after that date; it is ignored once tickets are stored.
Use `--recalculate-all` to recalculate the KPI fields for every stored ticket,
for example after changing the configuration.

## Configuration

The config file is YAML (`.yaml`/`.yml`) or JSON. `config.example.yaml` documents every option; unknown options are reported as a warning at startup.

| Key | Description |
| --- | --- |
| `jira_base_url`, `jira_user`, `jira_token` | Jira Cloud URL and API credentials. |
| `db_path` | Path of the SQLite database. |
| `base_jql` | JQL that limits which tickets are fetched, e.g. `project = DEMO`. |
| `log_level`, `log_file` | Logging settings. |
| `output_excel`, `excel_write_mode` | Excel export target and overwrite behaviour (see below). |
| `dashboard_start_quarter` | Start quarter preselected on the Excel dashboard, e.g. `"2025.Q1"`. Default: the same quarter one year back. |
| `custom_fields` | Maps logical field names to the Jira custom field IDs of your instance. Supported names: `request_type`, `deployment_type`, `organizations`, `satisfaction`, `satisfaction_ts`, `work_category`, `sentiment`, `request_participants`, `resolution`. |
| `non_support_request_types` | Request types that are excluded from the support KPIs in the summary views. Default: none. |
| `support_issue_types` | Issue types counted in the summary views (case-insensitive). Default: `["Support"]`. An empty list counts all issue types. |
| `request_type_categories` | Groups tickets into categories for the summary views. Each category has a list of patterns; the first category with a matching pattern wins. A pattern is either a SQL `LIKE` pattern (`%` = any text, case-insensitive) matched against the request type, or a mapping of `issue_type` and/or `request_type` `LIKE` patterns that must all match, e.g. `{issue_type: Incident, request_type: "%Hardware%"}`. The summary views get an `open_<category>` and a `closed_<category>` column per category. Default (`null`): one category per issue type in `support_issue_types`, or per stored issue type when that list is empty. `{}` gives no categories. |
| `request_type_category_default` | Category for tickets that match no pattern. Default: `null` (no category). Set to `null` together with an empty `request_type_categories` to drop the category columns. |
| `priority_positions` | Maps Jira priority names to a numeric position. Positions above `0` count as high priority, and a raise to a position above `0` counts as an escalation. Unknown priorities get `0`. Defaults to the standard Jira priorities (see `config.example.yaml`). |
| `closed_statuses` | Statuses in which a ticket counts as closed: sets the resolved date and stops the open-day count, and closed tickets are left out of the open ticket counts. The resolved date is the last change from another status into one of these; moving between them (e.g. an automation closing resolved tickets) doesn't change it, and reopened tickets have none until they are closed again. Default: `["Resolved", "Closed", "Cancelled"]`. |
| `cancelled_statuses` | Statuses that mark a ticket as cancelled (`is_junk`); these tickets are left out of the summary views. Default: `["Cancelled"]`. |
| `resolution_time_excluded_statuses` | Days that start in one of these statuses do not count towards the adjusted resolution time. Default: `["Closed", "Cancelled", "Resolved", "Waiting for customer", "Pending"]`. |
| `no_contact_ignore_statuses` | Tickets in these statuses are not counted in the "no contact" KPI (see `no_contact_days`). Default: `["Closed", "Resolved", "Cancelled", "Pending"]`. |
| `internal_email_domains` | Email domains of your agents, e.g. `["example.com"]`. Comment authors with an address in one of these domains count as internal (see `internal_users` below). Default: none. |
| `internal_groups` | Jira groups whose members are agents, e.g. `["service-desk-agents"]`. When set, authors count as internal only with an address in `internal_email_domains` / `internal_users` or as a member of one of these groups; otherwise the Jira account type decides for authors without a visible email. Members are read from Jira on every run and stored in `internal_group_members`. Run once with `--recalculate-all` after changing it. Default: none. |
| `aged_days_short`, `aged_days_long` | Open tickets on Fridays count as aged when open longer than this many days (columns `aged_5_plus` / `aged_30_plus`; the names stay the same). Defaults: `5` and `30`. |
| `no_contact_days` | Open tickets on Fridays count as "no contact" after more than this many weekdays without contact (column `over5_days_no_contact`). Default: `5`. |
| `priority_deescalation_ignore_minutes` | A priority raise that is reverted within this many minutes is not counted as an escalation. |

### Lookup tables

Some settings live in database tables so they can be maintained without changing the config. Fill them with any SQLite tool:

- `internal_users` (`pattern`) – regular expressions matched against author email addresses. Matching authors count as agents, which is used to calculate first and last response times. For whole domains, `internal_email_domains` in the config is simpler; both are used.
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

### Dashboard

The first sheet, `Dashboard`, shows the support KPIs from a start quarter up to
the export date as charts and a table. Three input cells control it:

- **Start quarter** – first quarter shown (preset from `dashboard_start_quarter`).
- **Granularity** – one row per `Week`, `Month` or `Quarter`.
- **Include current period** – `Yes` adds the current, incomplete week, month or quarter (default `No`).

Below the period charts, the customer section shows the top 15 customers by new
tickets (in total and per request type category) and by hours logged, plus a
table, for the **last complete** week, month or quarter of the selected
granularity.

The dashboard is built from formulas on the `weekly_support_summary`,
`monthly_support_summary`, `quarterly_support_summary` and
`customer_weekly_summary` sheets, so it updates immediately without pivot
tables or a data refresh. Its helper cells are on the hidden `dashboard_calc`
sheet. Empty values (e.g. no first reply in a week) show as `#N/A` so the line
charts leave a gap instead of dropping to 0.

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
- `internal_user` - whether the author is an agent (same rules as for comments; automations count as external)

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

`agent_weekly_activity` shows per ISO week and internal user on how many tickets
they worked: `tickets_total`, split into `tickets_commented`,
`tickets_status_changed` (status or resolution), `tickets_assignment_changed` and
`tickets_other_changes` (any other field). A ticket counts once per column, so
the split columns can add up to more than the total. `comments`,
`public_comments`, `status_changes`, `assignment_changes` and `other_changes`
count the actions themselves; one changelog entry that changes several fields of
the same kind counts once. Changelog entries stored by older versions have no
internal flag; for those, authors with internal comments count as internal.

## License

Copyright (c) 2024-2026 CADIS GmbH. All rights reserved. See [LICENSE](LICENSE).
