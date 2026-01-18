"""Database module for chat message persistence."""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "monios.db"


def init_db():
    """Initialize database tables if they don't exist."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
            
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_uses JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
        """)


@contextmanager
def get_connection():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_conversation(user_id: str, session_id: Optional[str] = None) -> int:
    """Get existing conversation or create new one for user."""
    with get_connection() as conn:
        # Try to find existing active conversation
        row = conn.execute(
            "SELECT id FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        
        if row:
            conv_id = row["id"]
            # Update timestamp and session_id if provided
            if session_id:
                conn.execute(
                    "UPDATE conversations SET updated_at = ?, session_id = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), session_id, conv_id)
                )
            return conv_id
        
        # Create new conversation
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, session_id) VALUES (?, ?)",
            (user_id, session_id)
        )
        return cursor.lastrowid


def save_message(
    user_id: str,
    role: str,
    content: str,
    tool_events: Optional[list] = None,
    session_id: Optional[str] = None
) -> int:
    """Save a message to the database."""
    conv_id = get_or_create_conversation(user_id, session_id)
    
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO messages (conversation_id, role, content, tool_uses)
               VALUES (?, ?, ?, ?)""",
            (conv_id, role, content, json.dumps(tool_events) if tool_events else None)
        )
        return cursor.lastrowid


def get_messages(user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Get messages for a user."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT m.id, m.role, m.content, m.tool_uses, m.created_at
               FROM messages m
               JOIN conversations c ON m.conversation_id = c.id
               WHERE c.user_id = ?
               ORDER BY m.created_at ASC
               LIMIT ? OFFSET ?""",
            (user_id, limit, offset)
        ).fetchall()
        
        messages = []
        for row in rows:
            msg = {
                "id": f"msg_{row['id']}",
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["created_at"],
            }
            if row["tool_uses"]:
                try:
                    msg["tool_events"] = json.loads(row["tool_uses"])
                except json.JSONDecodeError:
                    pass
            messages.append(msg)
        
        return messages


def get_message_count(user_id: str) -> int:
    """Get total message count for a user."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as count
               FROM messages m
               JOIN conversations c ON m.conversation_id = c.id
               WHERE c.user_id = ?""",
            (user_id,)
        ).fetchone()
        return row["count"] if row else 0


def clear_messages(user_id: str) -> bool:
    """Clear all messages for a user."""
    with get_connection() as conn:
        # Get conversation IDs for user
        conv_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM conversations WHERE user_id = ?",
            (user_id,)
        ).fetchall()]
        
        if not conv_ids:
            return False
        
        # Delete messages
        placeholders = ",".join("?" * len(conv_ids))
        conn.execute(
            f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
            conv_ids
        )
        
        # Delete conversations
        conn.execute(
            "DELETE FROM conversations WHERE user_id = ?",
            (user_id,)
        )
        
        return True


# Initialize database on module load
init_db()
