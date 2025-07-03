# SupportStatsGetter

This project collects ticket statistics from TeamSupport and Jira Service
Management and stores them in a normalized SQLite database.  The initial
implementation mirrors a subset of the old `TeamSupportStatsGetter.py` script,
laying the groundwork for future features such as KPI calculation, legacy data
import, configuration handling and a graphical user interface.

## Usage

Run the script and provide credentials on the command line. Only the Jira
arguments are required for the current functionality:

```bash
python SupportStatsGetter.py \
    --jira_base_url https://example.atlassian.net \
    --jira_user you@example.com \
    --jira_token api-token \
    --db_path support_stats.sqlite
```

Any of the legacy TeamSupport arguments (`--username`, `--password`,
`--base_url` etc.) may still be specified but are currently ignored.

Fetched tickets will be stored in the SQLite database provided via `--db_path`.

The script currently requires the `requests`, `pytz`, `dateutil` and
`sqlalchemy` packages.
