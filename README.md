# SupportStatsGetter

This project collects ticket statistics from **Jira Service Management** and stores them in a normalized Access database by default. A SQLite database can be used for regression tests. The code originated from the old `TeamSupportStatsGetter.py` script but no longer supports TeamSupport.

## Usage

1. Create a `config.json` file with your Jira credentials. You can optionally specify a base JQL query to limit the scope of fetched tickets. A sample configuration is provided as `config.example.json`:
```json
{
  "jira_base_url": "https://example.atlassian.net",
  "jira_user": "your-email@example.com",
  "jira_token": "api-token",
  "db_path": "support_stats.accdb",
  "base_jql": "project = DEMO",
  "log_level": "INFO",
  "log_file": "support_stats.log",
  "custom_fields": {
    "request_type": "customfield_10010",
    "deployment_type": "customfield_10011",
    "satisfaction": "customfield_10012",
    "satisfaction_ts": "customfield_10013"
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
Fetched tickets will be stored in the Access database specified in the config file. Pass `--sqlite` to use SQLite instead.
When using SQLite, `--output-excel [filename]` writes all database views to an Excel
workbook. If no filename is provided the file `SupportStats.xlsx` is created.
Subsequent runs only request tickets updated after the newest record already stored.
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
- `created` / `updated` - timestamps

Worklog entries are stored in the `jira_worklogs` table with the following columns:

- `id` - the Jira worklog ID
- `ticket_id` - internal issue ID
- `author` - display name of the worklog author
- `author_email` - email of the worklog author
- `time_spent_seconds` - duration in seconds
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

The script currently requires the `requests`, `pytz`, `dateutil` and `sqlalchemy` packages.

Daily statistics for each ticket are stored in the `jira_calendar` table. Columns roughly mirror the fields of the `StatisticsDayRecord` structure used by the legacy script:

- `id` - combination of ticket id and date
- `ticket_id` - internal issue id
- `ticket_nr` - issue key
- `date` - day the row represents
- `actions` / `public_actions` - counts of comments on that day
- `start_assigned_group` / `end_assigned_group` - assignment group at start and end of the day
- `start_assigned_user` / `end_assigned_user` - assigned user at start and end of the day
- `start_assigned_user_email` / `end_assigned_user_email` - email addresses of the assigned users
- `start_status` / `end_status` - status at the start and end of the day
- `time_spent_min` - minutes logged via worklogs for the day
- `cw` / `weekday` - calendar week and ISO weekday

## Importing legacy TeamSupport data

Historic exports from the former TeamSupport system can be loaded into the same
Access database used for Jira data. The repository provides a dedicated helper
script for this purpose (use `--sqlite` to import into a SQLite database):

```bash
python scripts/import_teamsupport.py --db path/to/db.accdb --input resource/reduced_tickets.json
```

The importer reads the provided JSON file and inserts tickets, comments,
worklogs, changelog entries and calendar rows. It can also parse the legacy
history entries to populate the `jira_changelog` table and mark comments or
history rows created by internal users based on the IDs defined in
`cadis_bdk_contacts.py`.

Legacy ticket statuses are translated to the Jira status names using a built-in
mapping. This ensures that metrics derived from imported data are consistent
with records created directly in Jira.

