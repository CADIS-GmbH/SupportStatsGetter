import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from SupportStatsGetter import JIRA_AUTH_FAILED_EXIT


class RejectLogin(BaseHTTPRequestHandler):
    """A Jira that answers every request with 401, like for an expired token."""

    def _reject(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errorMessages":["Unauthorized"]}')

    do_GET = do_POST = _reject

    def log_message(self, *args):
        pass


@pytest.fixture
def rejecting_jira():
    server = HTTPServer(("127.0.0.1", 0), RejectLogin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_rejected_login_fails_without_writing_a_report(tmp_path, rejecting_jira):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"jira_base_url: {rejecting_jira}\n"
        "jira_user: me@example.com\n"
        "jira_token: expired\n"
        "base_jql: 'project = X'\n"
        "output_excel: out.xlsx\n",
        encoding="utf-8",
    )
    environ = {k: v for k, v in os.environ.items() if not k.startswith("SUPPORTSTATS_")}
    result = subprocess.run(
        [sys.executable, str(ROOT / "SupportStatsGetter.py"), "-c", str(config)],
        env=environ, capture_output=True, text=True, cwd=tmp_path, timeout=120,
    )
    assert result.returncode == JIRA_AUTH_FAILED_EXIT
    assert "Jira rejected the login for 'me@example.com' (401" in result.stderr
    assert "No Excel report was written" in result.stderr
    assert not (tmp_path / "out.xlsx").exists()
