"""
database.py — ReflectQL

Creates and seeds a local SQLite database (support_tickets.db) for a fictional
small SaaS company's support ticket system.

The schema is DELIBERATELY messy, on purpose. This is the whole point of the
project: a naive text-to-SQL agent will very often generate a query that is
subtly wrong (wrong join column, wrong "status" field, etc.), and the
self-correcting LangGraph loop is what recovers from that.

Specific "traps" baked into this schema (documented again in README.md):

1. TWO status-like columns on `tickets`:
   - `status`         -> coarse lifecycle state: open / in_progress / closed
   - `ticket_status`  -> finer resolution/queue state: new / pending_customer /
                          escalated / resolved / duplicate
   An LLM asked "how many tickets are resolved?" will often guess the wrong
   column (or the wrong value spelling) on the first try.

2. Non-obvious foreign key names (no "_id" pattern matching the table name):
   - tickets.requester_id  -> customers.id   (NOT "customer_id")
   - tickets.cat_id        -> categories.id  (NOT "category_id")
   - tickets.handler_id    -> agents.id      (NOT "agent_id", nullable)
   - agent_logs.actor_id   -> agents.id      (NOT "agent_id")
   - agent_logs.ticket_ref -> tickets.id     (NOT "ticket_id")

3. Two similarly-named tables that are easy to confuse:
   - `agents`      -> one row per support agent (roster / HR-ish info)
   - `agent_logs`  -> one row per action an agent took on a ticket (activity)
   A naive query for "how many agents worked on ticket X" might mistakenly
   COUNT(*) FROM agents instead of joining through agent_logs.

Run this file directly to (re)create and seed the database:
    python database.py
"""

import os
import random
import sqlite3
from datetime import datetime, timedelta

from faker import Faker

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support_tickets.db")

fake = Faker()
Faker.seed(42)
random.seed(42)

# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

SCHEMA_SQL = """
DROP TABLE IF EXISTS agent_logs;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS agents;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL,
    company_name  TEXT,
    signup_date   TEXT NOT NULL
);

CREATE TABLE categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    description  TEXT
);

CREATE TABLE agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    department  TEXT NOT NULL,
    start_date  TEXT NOT NULL
);

-- Note: tickets has TWO status-like columns on purpose. See module docstring.
-- Foreign keys are intentionally NOT named "<table>_id" to force the agent
-- to actually read the schema instead of guessing conventional names.
CREATE TABLE tickets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id   INTEGER NOT NULL REFERENCES customers(id),
    cat_id         INTEGER NOT NULL REFERENCES categories(id),
    handler_id     INTEGER REFERENCES agents(id),  -- nullable: unassigned tickets
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL,  -- open | in_progress | closed
    ticket_status  TEXT NOT NULL,  -- new | pending_customer | escalated | resolved | duplicate
    priority       TEXT NOT NULL,  -- low | medium | high | urgent
    opened_at      TEXT NOT NULL,
    closed_at      TEXT
);

-- Activity log for agents; distinct from the `agents` roster table.
-- Foreign keys again deliberately not named "<table>_id".
CREATE TABLE agent_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id     INTEGER NOT NULL REFERENCES agents(id),
    ticket_ref   INTEGER NOT NULL REFERENCES tickets(id),
    action_type  TEXT NOT NULL,  -- comment | status_change | assignment | escalation
    notes        TEXT,
    created_at   TEXT NOT NULL
);
"""

DEPARTMENTS = ["Billing", "Technical", "Onboarding", "General Support"]
CATEGORY_SEED = [
    ("Billing Issue", "Questions or problems related to invoices and payments"),
    ("Bug Report", "Something in the product is broken or not working as expected"),
    ("Feature Request", "Customer asking for new functionality"),
    ("Account Access", "Login, password, or permissions problems"),
    ("Onboarding Help", "New customer setup and configuration questions"),
]
TICKET_LIFECYCLE_STATUS = ["open", "in_progress", "closed"]
TICKET_RESOLUTION_STATUS = ["new", "pending_customer", "escalated", "resolved", "duplicate"]
PRIORITIES = ["low", "medium", "high", "urgent"]
ACTION_TYPES = ["comment", "status_change", "assignment", "escalation"]


def _rand_dt(start_days_ago: int, end_days_ago: int) -> datetime:
    days_ago = random.randint(end_days_ago, start_days_ago)
    return datetime.now() - timedelta(days=days_ago, hours=random.randint(0, 23))


def seed_database(db_path: str = DB_PATH, n_customers: int = 60, n_agents: int = 8,
                   n_tickets: int = 100, n_logs: int = 130) -> None:
    """(Re)create the SQLite file and populate it with synthetic data."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(SCHEMA_SQL)

    # --- customers ---
    customers = []
    for _ in range(n_customers):
        signup = _rand_dt(730, 1)
        customers.append((
            fake.name(),
            fake.company_email(),
            fake.company(),
            signup.strftime("%Y-%m-%d"),
        ))
    cur.executemany(
        "INSERT INTO customers (full_name, email, company_name, signup_date) VALUES (?, ?, ?, ?)",
        customers,
    )

    # --- categories ---
    cur.executemany(
        "INSERT INTO categories (label, description) VALUES (?, ?)",
        CATEGORY_SEED,
    )

    # --- agents ---
    agents = []
    for _ in range(n_agents):
        start = _rand_dt(900, 30)
        agents.append((
            fake.name(),
            fake.company_email(),
            random.choice(DEPARTMENTS),
            start.strftime("%Y-%m-%d"),
        ))
    cur.executemany(
        "INSERT INTO agents (name, email, department, start_date) VALUES (?, ?, ?, ?)",
        agents,
    )

    n_categories = len(CATEGORY_SEED)

    # --- tickets ---
    tickets = []
    for _ in range(n_tickets):
        requester_id = random.randint(1, n_customers)
        cat_id = random.randint(1, n_categories)
        status = random.choice(TICKET_LIFECYCLE_STATUS)

        # Keep the two status columns loosely consistent with each other,
        # the way real messy production data often is (mostly sensible,
        # occasionally contradictory) rather than perfectly aligned.
        if status == "closed":
            ticket_status = random.choice(["resolved", "duplicate", "resolved", "escalated"])
        elif status == "in_progress":
            ticket_status = random.choice(["pending_customer", "escalated", "new"])
        else:  # open
            ticket_status = random.choice(["new", "pending_customer"])

        handler_id = random.randint(1, n_agents) if status != "open" or random.random() < 0.4 else None

        opened = _rand_dt(365, 1)
        closed_at = None
        if status == "closed":
            closed_at = (opened + timedelta(days=random.randint(0, 14))).strftime("%Y-%m-%d %H:%M:%S")

        tickets.append((
            requester_id,
            cat_id,
            handler_id,
            fake.sentence(nb_words=6).rstrip("."),
            fake.paragraph(nb_sentences=3),
            status,
            ticket_status,
            random.choice(PRIORITIES),
            opened.strftime("%Y-%m-%d %H:%M:%S"),
            closed_at,
        ))
    cur.executemany(
        """INSERT INTO tickets
           (requester_id, cat_id, handler_id, subject, body, status, ticket_status,
            priority, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        tickets,
    )

    # --- agent_logs ---
    logs = []
    for _ in range(n_logs):
        actor_id = random.randint(1, n_agents)
        ticket_ref = random.randint(1, n_tickets)
        created = _rand_dt(365, 1)
        logs.append((
            actor_id,
            ticket_ref,
            random.choice(ACTION_TYPES),
            fake.sentence(nb_words=10),
            created.strftime("%Y-%m-%d %H:%M:%S"),
        ))
    cur.executemany(
        """INSERT INTO agent_logs (actor_id, ticket_ref, action_type, notes, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        logs,
    )

    conn.commit()
    conn.close()
    print(f"Seeded database at {db_path}")
    print(f"  customers:   {n_customers}")
    print(f"  categories:  {n_categories}")
    print(f"  agents:      {n_agents}")
    print(f"  tickets:     {n_tickets}")
    print(f"  agent_logs:  {n_logs}")
    total = n_customers + n_categories + n_agents + n_tickets + n_logs
    print(f"  TOTAL rows:  {total}")


def get_schema_text(db_path: str = DB_PATH) -> str:
    """
    Return a human/LLM-readable description of the current DB schema,
    pulled live from sqlite_master + PRAGMA table_info so it always matches
    reality (used both as LLM context and by the /schema API endpoint).
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in cur.fetchall()]

    lines = []
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        cols = cur.fetchall()  # cid, name, type, notnull, dflt_value, pk
        col_descs = [f"{c[1]} {c[2]}{' PRIMARY KEY' if c[5] else ''}" for c in cols]
        lines.append(f"TABLE {table} ({', '.join(col_descs)})")

        cur.execute(f"PRAGMA foreign_key_list({table})")
        fks = cur.fetchall()
        for fk in fks:
            # fk: id, seq, table, from, to, on_update, on_delete, match
            lines.append(f"  FOREIGN KEY {table}.{fk[3]} -> {fk[2]}.{fk[4]}")

    conn.close()
    return "\n".join(lines)


def get_schema_dict(db_path: str = DB_PATH) -> dict:
    """Structured schema info for the /schema endpoint (used by the frontend sidebar)."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in cur.fetchall()]

    schema = {}
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        cols = cur.fetchall()
        schema[table] = [{"name": c[1], "type": c[2], "primary_key": bool(c[5])} for c in cols]

    conn.close()
    return schema


if __name__ == "__main__":
    seed_database()
