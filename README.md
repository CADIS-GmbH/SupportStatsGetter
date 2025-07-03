# SupportStatsGetter

This project collects ticket statistics from TeamSupport and Jira Service
Management and stores them in a normalized SQLite database.  The initial
implementation mirrors a subset of the old `TeamSupportStatsGetter.py` script,
laying the groundwork for future features such as KPI calculation, legacy data
import, configuration handling and a graphical user interface.

## Usage

1. Create a `config.json` file with your credentials.  For TeamSupport only:
   ```json
   {
     "base_url": "https://example.teamsupport.com",
     "username": "user",
     "password": "pass",
     "db_path": "support_stats.sqlite"
   }
   ```
   To fetch from Jira instead, include these keys:
   ```json
   {
     "jira_base_url": "https://example.atlassian.net",
     "jira_user": "your-email@example.com",
     "jira_token": "api-token",
     "db_path": "support_stats.sqlite"
   }
   ```
2. Run the script:
   ```bash
   python SupportStatsGetter.py
   ```
   Fetched tickets will be inserted into the SQLite database specified in the
   config file.

The script currently requires the `requests`, `pytz`, `dateutil` and
`sqlalchemy` packages.
