import datetime
from SupportStatsGetter import make_jira_ticket_calendar


def test_request_type_category_in_calendar():
    issue = {
        "id": "1",
        "key": "TST-1",
        "created": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        "resolved": datetime.datetime(2025, 1, 3, tzinfo=datetime.UTC),
        "fields": {"request_type": "Support"},
    }
    changelog = [
        {
            "created": datetime.datetime(2025, 1, 2, tzinfo=datetime.UTC),
            "items": [
                {"field": "Request Type", "fromString": "Support", "toString": "Service"}
            ],
        }
    ]
    rows = make_jira_ticket_calendar(
        issue,
        [],
        [],
        changelog,
        {"Support": "support", "Service": "service"},
        "Request Type",
    )
    assert rows[0]["start_issue_category"] == "support"
    assert rows[1]["start_issue_category"] == "support"
    assert rows[1]["end_issue_category"] == "service"
    assert rows[2]["start_issue_category"] == "service"
