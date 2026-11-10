import datetime
import socket
import sys
import threading
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import SupportStatsGetter as SupportStatsGetter_module
from SupportStatsGetter import Config, SupportStatsGetter, jira_session, sync_jira_tickets


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def make_getter(tmp_path, base_jql="project = SD"):
    return SupportStatsGetter(
        Config(db_path=tmp_path / "test.db", jira_base_url="https://jira.example", base_jql=base_jql)
    )


def search_jql(getter, monkeypatch):
    sent = []

    def post(self, url, json=None, **kw):
        sent.append(json["jql"])
        return FakeResponse({"issues": []})

    monkeypatch.setattr(SupportStatsGetter_module.requests.Session, "post", post)
    getter.fetch_jira_tickets(updated_since=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    return sent[0]


def test_tickets_are_fetched_oldest_change_first(tmp_path, monkeypatch):
    jql = search_jql(make_getter(tmp_path), monkeypatch)
    assert jql.startswith('(project = SD) AND updated >= "2025-12-31 23:55"')
    assert jql.endswith(" ORDER BY updated ASC")


def test_order_in_base_jql_is_replaced(tmp_path, monkeypatch):
    jql = search_jql(make_getter(tmp_path, "project = SD OR project = CS order by created DESC"), monkeypatch)
    assert jql == '(project = SD OR project = CS) AND updated >= "2025-12-31 23:55" ORDER BY updated ASC'


def ticket(n):
    day = datetime.datetime(2026, 10, n, 12, 0)
    return {
        "id": str(n), "key": f"SD-{n}", "created": day, "updated": day, "resolved": None,
        "fields": {"status": {"name": "Open"}, "summary": f"Ticket {n}"},
    }


def test_aborted_run_keeps_the_tickets_processed_so_far(tmp_path, monkeypatch):
    monkeypatch.setattr(SupportStatsGetter_module, "TICKET_BATCH_SIZE", 2)
    getter = make_getter(tmp_path)
    tickets = [ticket(n) for n in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(getter, "fetch_jira_tickets", lambda updated_since=None: tickets)

    def comments(issue_id):
        if issue_id == "4":
            raise requests.ConnectionError("connection reset")
        return []

    monkeypatch.setattr(getter, "fetch_jira_comments", comments)
    monkeypatch.setattr(getter, "fetch_jira_worklogs", lambda issue_id: [])
    monkeypatch.setattr(getter, "fetch_jira_changelog", lambda issue_id: [])

    with pytest.raises(requests.ConnectionError):
        sync_jira_tickets(getter, None)

    # tickets 1 and 2 were saved as a batch, ticket 3 when the run failed
    assert getter.latest_update_timestamp() == datetime.datetime(2026, 10, 3, 12, 0)


def serve(responses):
    """Local server answering each connection with the next response (None: drop it)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    hits = []

    def run():
        for response in responses:
            conn, _ = server.accept()
            conn.recv(65536)
            hits.append(1)
            if response is not None:
                conn.sendall(response)
            conn.close()
        server.close()

    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}/", hits


OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
UNAVAILABLE = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def test_dropped_connection_is_retried():
    url, hits = serve([None, OK])
    r = jira_session().get(url, timeout=5)
    assert r.json() == {}
    assert len(hits) == 2


def test_server_error_is_retried():
    url, hits = serve([UNAVAILABLE, OK])
    r = jira_session().get(url, timeout=5)
    assert r.status_code == 200
    assert len(hits) == 2
