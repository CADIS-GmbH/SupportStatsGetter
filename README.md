# SupportStatsGetter

This project collects ticket statistics from TeamSupport and stores them in a
normalized SQLite database.  The initial implementation mirrors a subset of the
old `TeamSupportStatsGetter.py` script, laying the groundwork for future
features such as KPI calculation, legacy data import, configuration handling and
a graphical user interface.

## Usage

1. Create a `config.json` file with your credentials.  A sample
   configuration file is provided as `config.example.json`:
   ```json
   {
     "base_url": "https://example.teamsupport.com",
     "username": "user",
     "password": "pass",
     "db_path": "support_stats.sqlite"
   }
   ```
2. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the script. You can specify an alternative config file with
   `--config`:
   ```bash
   python SupportStatsGetter.py --config config.json
   ```
   Fetched tickets will be inserted into the SQLite database specified in the
   config file.
