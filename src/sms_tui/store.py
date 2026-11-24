"""SQLite persistence for SMS threads and message archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re
import sqlite3

from sms_tui.parser import Sms

_PHONE_CHARS_RE = re.compile(r"[\s\-().]+")


@dataclass(frozen=True)
class Thread:
    """Conversation thread summary for the left pane."""

    peer: str
    display_name: str | None
    last_ts: datetime
    unread: int
    preview: str


@dataclass(frozen=True)
class Message:
    """Archived incoming or outgoing message."""

    id: int
    peer: str
    direction: str
    ts: datetime
    text: str
    parts: int
    status: str


def normalize_peer(peer: str) -> str:
    """Normalize Vietnamese local/E.164 numbers while keeping shortcodes/names intact."""
    cleaned = peer.strip()
    if not cleaned:
        return ""
    compact = _PHONE_CHARS_RE.sub("", cleaned)
    if compact.startswith("+") and compact[1:].isdigit():
        return compact
    if compact.isdigit():
        if len(compact) in (10, 11) and compact.startswith("0"):
            return "+84" + compact[1:]
        if len(compact) in (11, 12) and compact.startswith("84"):
            return "+" + compact
    return cleaned


def _fingerprint(direction: str, peer: str, ts: datetime, text: str) -> str:
    payload = f"{direction}|{peer}|{ts.isoformat()}|{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class Store:
    """SQLite-backed archive of SMS threads and messages (UI-thread owned)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                peer TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                ts TEXT NOT NULL,
                text TEXT NOT NULL,
                parts INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS threads (
                peer TEXT PRIMARY KEY,
                display_name TEXT,
                last_ts TEXT NOT NULL,
                unread INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_peer_ts
                ON messages (peer, ts, id);
            """
        )
        self._conn.commit()

    def _upsert_thread(self, peer: str, ts_iso: str, unread_delta: int) -> None:
        self._conn.execute(
            """
            INSERT INTO threads (peer, display_name, last_ts, unread)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(peer) DO UPDATE SET
                last_ts = MAX(threads.last_ts, excluded.last_ts),
                unread = threads.unread + excluded.unread
            """,
            (peer, ts_iso, unread_delta),
        )

    def add_messages(self, msgs: list[Sms]) -> int:
        """Insert parsed SMS messages into the archive, returning newly inserted count."""
        inserted = 0
        with self._conn:
            for msg in msgs:
                if msg.pdu == "Status_Report":
                    continue
                direction = (
                    "out"
                    if msg.pdu == "Submit" or msg.state in ("Sent", "UnSent")
                    else "in"
                )
                peer = normalize_peer(msg.number)
                ts_iso = msg.sent.isoformat()
                fp = _fingerprint(direction, peer, msg.sent, msg.text)
                status = msg.state.lower() if direction == "out" else msg.state
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (peer, direction, ts, text, parts, status, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (peer, direction, ts_iso, msg.text, msg.parts, status, fp),
                )
                if cur.rowcount == 1:
                    inserted += 1
                    unread_delta = 1 if (direction == "in" and msg.state == "UnRead") else 0
                    self._upsert_thread(peer, ts_iso, unread_delta)
        return inserted

    def add_outgoing(self, peer: str, text: str, status: str = "sending") -> int:
        """Record an outgoing SMS immediately and return its row id."""
        norm_peer = normalize_peer(peer)
        ts = datetime.now().replace(microsecond=0)
        with self._conn:
            while True:
                fp = _fingerprint("out", norm_peer, ts, text)
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (peer, direction, ts, text, parts, status, fingerprint)
                    VALUES (?, 'out', ?, ?, 1, ?, ?)
                    """,
                    (norm_peer, ts.isoformat(), text, status, fp),
                )
                if cur.rowcount == 1 and cur.lastrowid is not None:
                    self._upsert_thread(norm_peer, ts.isoformat(), 0)
                    return int(cur.lastrowid)
                ts += timedelta(seconds=1)

    def set_status(self, message_id: int, status: str) -> None:
        """Update delivery/send status of an archived message."""
        with self._conn:
            self._conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?",
                (status, message_id),
            )

    def mark_read(self, peer: str) -> None:
        """Clear unread counter for a conversation thread."""
        norm_peer = normalize_peer(peer)
        with self._conn:
            self._conn.execute(
                "UPDATE threads SET unread = 0 WHERE peer = ?",
                (norm_peer,),
            )

    def list_threads(self, query: str = "") -> list[Thread]:
        """List threads newest-first, optionally filtered by peer, name, or body."""
        q = query.strip()
        params: list[str] = []
        where = ""
        if q:
            like = f"%{q}%"
            where = """
                WHERE t.peer LIKE ? COLLATE NOCASE
                   OR COALESCE(t.display_name, '') LIKE ? COLLATE NOCASE
                   OR EXISTS (
                       SELECT 1 FROM messages m2
                       WHERE m2.peer = t.peer AND m2.text LIKE ? COLLATE NOCASE
                   )
            """
            params.extend([like, like, like])

        rows = self._conn.execute(
            f"""
            SELECT
                t.peer,
                t.display_name,
                t.last_ts,
                t.unread,
                COALESCE(
                    (
                        SELECT m.text
                        FROM messages m
                        WHERE m.peer = t.peer
                        ORDER BY m.ts DESC, m.id DESC
                        LIMIT 1
                    ),
                    ''
                ) AS preview
            FROM threads t
            {where}
            ORDER BY t.last_ts DESC, t.peer ASC
            """,
            params,
        ).fetchall()

        threads: list[Thread] = []
        for row in rows:
            preview = " ".join(row["preview"].splitlines())
            if len(preview) > 60:
                preview = preview[:57] + "..."
            threads.append(
                Thread(
                    peer=row["peer"],
                    display_name=row["display_name"],
                    last_ts=datetime.fromisoformat(row["last_ts"]),
                    unread=int(row["unread"]),
                    preview=preview,
                )
            )
        return threads

    def list_messages(self, peer: str) -> list[Message]:
        """List messages for a peer in chronological order."""
        norm_peer = normalize_peer(peer)
        rows = self._conn.execute(
            """
            SELECT id, peer, direction, ts, text, parts, status
            FROM messages
            WHERE peer = ?
            ORDER BY ts ASC, id ASC
            """,
            (norm_peer,),
        ).fetchall()
        return [
            Message(
                id=int(row["id"]),
                peer=row["peer"],
                direction=row["direction"],
                ts=datetime.fromisoformat(row["ts"]),
                text=row["text"],
                parts=int(row["parts"]),
                status=row["status"],
            )
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()
