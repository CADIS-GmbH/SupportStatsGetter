import json
import sqlite3
from pathlib import Path
from datetime import datetime

def parse_jira_datetime(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S") if s else None

def init_db(conn):
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        key TEXT,
        summary TEXT,
        status TEXT,
        created TEXT,
        resolved TEXT
    );
    CREATE TABLE IF NOT EXISTS worklogs (
        id TEXT PRIMARY KEY,
        ticket_id TEXT,
        author TEXT,
        time_spent INTEGER,
        started TEXT
    );
    CREATE TABLE IF NOT EXISTS changelog (
        id TEXT PRIMARY KEY,
        ticket_id TEXT,
        field TEXT,
        from_val TEXT,
        to_val TEXT,
        timestamp TEXT
    );
    CREATE TABLE IF NOT EXISTS comments (
        id TEXT PRIMARY KEY,
        ticket_id TEXT,
        author TEXT,
        public INTEGER,
        created TEXT,
        body TEXT
    );
    """)
    conn.commit()

def insert_ticket(conn, data):
    flds = data["fields"]
    conn.execute("""
        INSERT OR REPLACE INTO tickets (id, key, summary, status, created, resolved)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["id"], data["key"], flds["summary"], flds["status"]["name"],
        flds["created"], flds.get("resolutiondate")
    ))

def insert_worklogs(conn, data):
    for wl in data["fields"].get("worklog", {}).get("worklogs", []):
        conn.execute("""
            INSERT OR REPLACE INTO worklogs (id, ticket_id, author, time_spent, started)
            VALUES (?, ?, ?, ?, ?)
        """, (
            wl["id"], data["id"], wl["author"]["displayName"],
            wl["timeSpentSeconds"], wl["started"]
        ))

def insert_comments(conn, data):
    for com in data["fields"].get("comment", {}).get("comments", []):
        is_public = com.get("jsdPublic", True)  # default to public
        body = com["body"]
        if isinstance(body, dict) and body.get("content"):
            # Simplify structured content to plain text
            text_parts = []
            for blk in body["content"]:
                for frag in blk.get("content", []):
                    text_parts.append(frag.get("text", ""))
            body = " ".join(text_parts)
        elif isinstance(body, str):
            body = body
        else:
            body = ""
        conn.execute("""
            INSERT OR REPLACE INTO comments (id, ticket_id, author, public, created, body)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            com["id"], data["id"], com["author"]["displayName"],
            int(is_public), com["created"], body
        ))

def insert_changelog(conn, data):
    for hist in data.get("changelog", {}).get("histories", []):
        timestamp = hist["created"]
        for item in hist.get("items", []):
            conn.execute("""
                INSERT OR REPLACE INTO changelog (id, ticket_id, field, from_val, to_val, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                hist["id"] + ":" + item["field"], data["id"], item["field"],
                item.get("fromString"), item.get("toString"), timestamp
            ))

def load_and_store(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        insert_ticket(conn, data)
        insert_worklogs(conn, data)
        insert_comments(conn, data)
        insert_changelog(conn, data)
        conn.commit()

def print_kpis(conn):
    print("\n--- KPI OVERVIEW ---")
    for row in conn.execute("""
        SELECT t.key, t.status, COUNT(c.id) AS public_comments, SUM(w.time_spent) AS total_work_secs
        FROM tickets t
        LEFT JOIN comments c ON c.ticket_id = t.id AND c.public = 1
        LEFT JOIN worklogs w ON w.ticket_id = t.id
        GROUP BY t.id
    """):
        key, status, pubcoms, worksecs = row
        print(f"{key}: {pubcoms or 0} public comments, {round((worksecs or 0)/60)} min work, status: {status}")

if __name__ == "__main__":
    db_path = "jira_kpi.sqlite"
    json_path = "CSTMR-14.json"  # replace with your actual file path

    conn = sqlite3.connect(db_path)
    init_db(conn)
    load_and_store(conn, json_path)
    print_kpis(conn)
    conn.close()
