"""Write the config.example.yaml of the exe folder (used by build_exe.bat).

It is the project's config.example.yaml with paths for the exe folder: the
database and the log next to the exe, the Excel reports in reports\\. The paths
are relative, so run.bat starts the exe in its own folder.

Usage:  python exe\\make_exe_config.py config.example.yaml dist\\SupportStatsGetter\\config.example.yaml
"""
import re
import sys
from pathlib import Path

EXE_PATHS = {
    # run.bat asks for the credentials and keeps them in environment variables
    "jira_user": "null",
    "jira_token": "null",
    "db_path": r"'support_stats.sqlite'",
    "log_file": r"'support_stats.log'",
    "output_excel": r"'reports\SupportStats.xlsx'",
}


def make_exe_config(text: str) -> str:
    for key, value in EXE_PATHS.items():
        # a function, so the backslash in the value is not read as an escape
        text, count = re.subn(rf"(?m)^{key}:.*$", lambda _: f"{key}: {value}", text)
        if count != 1:
            raise ValueError(f"{key} not found exactly once in the example config")
    return text


if __name__ == "__main__":
    source, target = map(Path, sys.argv[1:3])
    target.write_text(make_exe_config(source.read_text(encoding="utf-8")), encoding="utf-8")
