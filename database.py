from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from typing import Iterable

from config import SERVICE_NAME

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = str(DATA_DIR / f"{SERVICE_NAME.lower().replace(' ', '_')}_vault.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE RESTRICT
        )
        """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_unique_values
        ON accounts(username, password, category_id)
        """
    )

    existing_columns = [row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "used" not in existing_columns:
        cur.execute("ALTER TABLE accounts ADD COLUMN used INTEGER NOT NULL DEFAULT 0")
    if "used_at" not in existing_columns:
        cur.execute("ALTER TABLE accounts ADD COLUMN used_at TEXT")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            requested_amount INTEGER NOT NULL,
            retrieved_amount INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE RESTRICT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(session_id) REFERENCES retrieval_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_accounts_category_id
        ON accounts(category_id)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_items_session_id
        ON retrieval_items(session_id)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_items_used
        ON retrieval_items(used)
        """
    )

    cur.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", ("uncategorized",))

    conn.commit()
    conn.close()


def normalize_name(name: str) -> str:
    return " ".join(name.split()).strip()


def get_category_id(name: str) -> int | None:
    name = normalize_name(name)
    conn = connect()
    row = conn.execute(
        "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
        (name,),
    ).fetchone()
    conn.close()
    return int(row["id"]) if row else None


def get_category_name(category_id: int) -> str | None:
    conn = connect()
    row = conn.execute(
        "SELECT name FROM categories WHERE id = ?",
        (category_id,),
    ).fetchone()
    conn.close()
    return row["name"] if row else None


def add_category(name: str) -> tuple[bool, str]:
    name = normalize_name(name)
    if not name:
        return False, "Category name cannot be empty."

    conn = connect()
    try:
        conn.execute("INSERT INTO categories(name) VALUES(?)", (name,))
        conn.commit()
        return True, name
    except sqlite3.IntegrityError:
        return False, "Category already exists."
    finally:
        conn.close()


def delete_category(name: str) -> tuple[bool, str]:
    name = normalize_name(name)
    if not name:
        return False, "Category name cannot be empty."

    if name.lower() == "uncategorized":
        return False, "uncategorized cannot be deleted."

    conn = connect()
    row = conn.execute(
        "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
        (name,),
    ).fetchone()
    if not row:
        conn.close()
        return False, "Category not found."

    category_id = int(row["id"])

    conn.execute(
        "UPDATE accounts SET category_id = (SELECT id FROM categories WHERE LOWER(name) = 'uncategorized') WHERE category_id = ?",
        (category_id,),
    )
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    return True, name


def list_categories() -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT c.id, c.name, COUNT(a.id) AS account_count
        FROM categories c
        LEFT JOIN accounts a ON a.category_id = c.id
        GROUP BY c.id, c.name
        ORDER BY CASE WHEN LOWER(c.name) = 'uncategorized' THEN 0 ELSE 1 END, LOWER(c.name)
        """
    ).fetchall()
    conn.close()
    return rows


def get_category_id_by_name(name: str) -> int | None:
    conn = connect()
    row = conn.execute(
        "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
        (name.strip(),),
    ).fetchone()
    conn.close()
    return int(row["id"]) if row else None


def add_account(username: str, password: str, category_id: int) -> tuple[bool, str, int | None]:
    username = username.strip()
    password = password.strip()
    if not username or not password:
        return False, "Username and password cannot be empty.", None

    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO accounts(username, password, category_id) VALUES(?,?,?)",
            (username, password, category_id),
        )
        conn.commit()
        return True, "Account added.", int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return False, "Duplicate account skipped.", None
    finally:
        conn.close()


def add_accounts_bulk(items: Iterable[tuple[str, str]], category_id: int) -> dict:
    added = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    conn = connect()
    cur = conn.cursor()

    for username, password in items:
        username = username.strip()
        password = password.strip()
        if not username or not password:
            failed += 1
            errors.append("Empty username or password skipped.")
            continue

        try:
            cur.execute(
                "INSERT INTO accounts(username, password, category_id) VALUES(?,?,?)",
                (username, password, category_id),
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1

    conn.commit()
    conn.close()

    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }


def search_accounts(term: str) -> list[sqlite3.Row]:
    term = term.strip()
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        WHERE a.username LIKE ? OR a.password LIKE ? OR c.name LIKE ?
        ORDER BY a.id DESC
        """,
        (f"%{term}%", f"%{term}%", f"%{term}%"),
    ).fetchall()
    conn.close()
    return rows


def delete_account(account_id: int) -> bool:
    conn = connect()
    cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_accounts_by_ids(account_ids: Iterable[int]) -> int:
    ids = list(dict.fromkeys(int(x) for x in account_ids))
    if not ids:
        return 0

    conn = connect()
    cur = conn.execute(f"DELETE FROM accounts WHERE id IN ({','.join('?' for _ in ids)})", ids)
    conn.commit()
    conn.close()
    return int(cur.rowcount)


def delete_accounts_in_category(category_id: int) -> int:
    conn = connect()
    cur = conn.execute("DELETE FROM accounts WHERE category_id = ?", (category_id,))
    conn.commit()
    conn.close()
    return int(cur.rowcount)


def get_accounts_for_category(category_id: int, limit: int) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        WHERE a.category_id = ?
        ORDER BY a.id ASC
        LIMIT ?
        """,
        (category_id, limit),
    ).fetchall()
    conn.close()
    return rows


def get_unused_accounts_for_category(category_id: int, limit: int) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        WHERE a.category_id = ? AND a.used = 0
        ORDER BY a.id ASC
        LIMIT ?
        """,
        (category_id, limit),
    ).fetchall()
    conn.close()
    return rows


def set_account_used(account_id: int, used: bool) -> bool:
    conn = connect()
    cur = conn.execute(
        """
        UPDATE accounts
        SET used = ?, used_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = ?
        """,
        (1 if used else 0, 1 if used else 0, account_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def count_accounts_for_category(category_id: int) -> int:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM accounts WHERE category_id = ?",
        (category_id,),
    ).fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def get_account_by_id(account_id: int) -> sqlite3.Row | None:
    conn = connect()
    row = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at, a.used, a.used_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        WHERE a.id = ?
        """,
        (account_id,),
    ).fetchone()
    conn.close()
    return row


def count_pending_items() -> int:
    conn = connect()
    row = conn.execute("SELECT COUNT(*) AS count FROM retrieval_items WHERE used = 0").fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def list_accounts(limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at, a.used, a.used_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        ORDER BY a.id ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return rows


def count_accounts() -> int:
    conn = connect()
    row = conn.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def create_retrieval_session(user_id: int, category_id: int, requested_amount: int, retrieved_amount: int) -> int:
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO retrieval_sessions(user_id, category_id, requested_amount, retrieved_amount)
        VALUES(?,?,?,?)
        """,
        (user_id, category_id, requested_amount, retrieved_amount),
    )
    conn.commit()
    session_id = int(cur.lastrowid)
    conn.close()
    return session_id


def add_retrieval_item(session_id: int, account_id: int, position: int) -> int:
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO retrieval_items(session_id, account_id, position)
        VALUES(?,?,?)
        """,
        (session_id, account_id, position),
    )
    conn.commit()
    item_id = int(cur.lastrowid)
    conn.close()
    return item_id


def list_recent_sessions(limit: int = 10) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT s.id, s.user_id, s.requested_amount, s.retrieved_amount, s.created_at, c.name AS category
        FROM retrieval_sessions s
        JOIN categories c ON c.id = s.category_id
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_session(session_id: int) -> sqlite3.Row | None:
    conn = connect()
    row = conn.execute(
        """
        SELECT s.id, s.user_id, s.requested_amount, s.retrieved_amount, s.created_at, c.name AS category
        FROM retrieval_sessions s
        JOIN categories c ON c.id = s.category_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    conn.close()
    return row


def get_session_items(session_id: int) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT i.id AS item_id, i.position, i.used, i.used_at,
               a.id AS account_id, a.username, a.password, c.name AS category
        FROM retrieval_items i
        JOIN accounts a ON a.id = i.account_id
        JOIN categories c ON c.id = a.category_id
        WHERE i.session_id = ?
        ORDER BY i.position ASC
        """,
        (session_id,),
    ).fetchall()
    conn.close()
    return rows


def get_item(item_id: int) -> sqlite3.Row | None:
    conn = connect()
    row = conn.execute(
        """
        SELECT i.id AS item_id, i.session_id, i.position, i.used, i.used_at,
               a.id AS account_id, a.username, a.password, c.name AS category
        FROM retrieval_items i
        JOIN accounts a ON a.id = i.account_id
        JOIN categories c ON c.id = a.category_id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    conn.close()
    return row


def set_item_used(item_id: int, used: bool) -> bool:
    conn = connect()
    cur = conn.execute(
        """
        UPDATE retrieval_items
        SET used = ?, used_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = ?
        """,
        (1 if used else 0, 1 if used else 0, item_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_pending_items(limit: int = 25, offset: int = 0) -> list[sqlite3.Row]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT i.id AS item_id, i.position, i.used, i.used_at,
               s.id AS session_id, s.created_at AS session_created_at,
               c.name AS category, a.username, a.password
        FROM retrieval_items i
        JOIN retrieval_sessions s ON s.id = i.session_id
        JOIN accounts a ON a.id = i.account_id
        JOIN categories c ON c.id = s.category_id
        WHERE i.used = 0
        ORDER BY i.id DESC
        LIMIT ?
        OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return rows


def stats_summary() -> dict:
    conn = connect()
    total_accounts = conn.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()["count"]
    total_sessions = conn.execute("SELECT COUNT(*) AS count FROM retrieval_sessions").fetchone()["count"]
    total_items = conn.execute("SELECT COUNT(*) AS count FROM retrieval_items").fetchone()["count"]
    used_items = conn.execute("SELECT COUNT(*) AS count FROM retrieval_items WHERE used = 1").fetchone()["count"]
    pending_items = conn.execute("SELECT COUNT(*) AS count FROM retrieval_items WHERE used = 0").fetchone()["count"]

    category_rows = conn.execute(
        """
        SELECT c.name, COUNT(a.id) AS account_count
        FROM categories c
        LEFT JOIN accounts a ON a.category_id = c.id
        GROUP BY c.id, c.name
        ORDER BY CASE WHEN LOWER(c.name) = 'uncategorized' THEN 0 ELSE 1 END, LOWER(c.name)
        """
    ).fetchall()
    conn.close()
    return {
        "total_accounts": int(total_accounts),
        "total_sessions": int(total_sessions),
        "total_items": int(total_items),
        "used_items": int(used_items),
        "pending_items": int(pending_items),
        "categories": category_rows,
    }


def export_accounts_csv() -> bytes:
    conn = connect()
    rows = conn.execute(
        """
        SELECT a.id, a.username, a.password, c.name AS category, a.created_at
        FROM accounts a
        JOIN categories c ON c.id = a.category_id
        ORDER BY a.id ASC
        """
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "username", "password", "category", "created_at"])
    for row in rows:
        writer.writerow([row["id"], row["username"], row["password"], row["category"], row["created_at"]])
    return buf.getvalue().encode("utf-8")
