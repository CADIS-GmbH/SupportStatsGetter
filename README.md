# SupportStatsGetter

This project collects ticket statistics from **Jira Service Management** and stores them in a normalized SQLite database. The code originated from the old `TeamSupportStatsGetter.py` script but no longer supports TeamSupport.

## Usage

1. Create a `config.json` file with your Jira credentials. You can optionally specify a base JQL query to limit the scope of fetched tickets. A sample configuration is provided as `config.example.json`:
```json
{
  "jira_base_url": "https://example.atlassian.net",
  "jira_user": "your-email@example.com",
  "jira_token": "api-token",
  "db_path": "support_stats.sqlite",
  "base_jql": "project = DEMO"
}
```
2. Install the dependencies:
```bash
pip install -r requirements.txt
```
3. Run the script (optionally pass `--config` to specify the file):
```bash
python SupportStatsGetter.py --config config.json
```
Fetched tickets will be stored in the SQLite database specified in the config file.
Subsequent runs only request tickets updated after the newest record already stored.

The script currently requires the `requests`, `pytz`, `dateutil` and `sqlalchemy` packages.
