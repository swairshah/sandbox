"""
Database module for storing users, conversations, and messages.
Uses SQLite with aiosqlite for async operations.
"""

import json
import aiosqlite
from datetime import datetime
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

DATABASE_PATH = Path(__file__).parent / "monios.db"


@dataclass
class User:
    user_id: str
    sprite_name: str
    sprite_url: Optional[str] = None
    created_at: Optional[datetime] = None
    last_active: Optional[datetime] = None


@dataclass
class Conversation:
    id: Optional[int]
    user_id: str
    session_id: Optional[str] = None  # Claude SDK session ID for resume
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Message:
    id: Optional[int]
    conversation_id: int
    role: str  # 'user' | 'assistant'
    content: str
    tool_uses: Optional[list] = None  # [{name, input, result}, ...]
    created_at: Optional[datetime] = None


class Database:
    def __init__(self, db_path: Path = DATABASE_PATH):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Connect to database and create tables if needed."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _create_tables(self):
        """Create tables if they don't exist."""
        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                sprite_name TEXT UNIQUE NOT NULL,
                sprite_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_uses JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
        """)
        await self._connection.commit()

    # ============== User Operations ==============

    async def create_user(self, user_id: str, sprite_name: str, sprite_url: Optional[str] = None) -> User:
        """Create a new user."""
        now = datetime.utcnow()
        await self._connection.execute(
            "INSERT INTO users (user_id, sprite_name, sprite_url, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
            (user_id, sprite_name, sprite_url, now, now)
        )
        await self._connection.commit()
        return User(user_id=user_id, sprite_name=sprite_name, sprite_url=sprite_url, created_at=now, last_active=now)

    async def get_user(self, user_id: str) -> Optional[User]:
        """Get user by ID."""
        async with self._connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return User(**dict(row))
            return None

    async def update_user_last_active(self, user_id: str):
        """Update user's last active timestamp."""
        await self._connection.execute(
            "UPDATE users SET last_active = ? WHERE user_id = ?",
            (datetime.utcnow(), user_id)
        )
        await self._connection.commit()

    async def update_user_sprite_url(self, user_id: str, sprite_url: str):
        """Update user's sprite URL."""
        await self._connection.execute(
            "UPDATE users SET sprite_url = ? WHERE user_id = ?",
            (sprite_url, user_id)
        )
        await self._connection.commit()

    # ============== Conversation Operations ==============

    async def create_conversation(self, user_id: str, session_id: Optional[str] = None) -> Conversation:
        """Create a new conversation."""
        now = datetime.utcnow()
        cursor = await self._connection.execute(
            "INSERT INTO conversations (user_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, session_id, now, now)
        )
        await self._connection.commit()
        return Conversation(id=cursor.lastrowid, user_id=user_id, session_id=session_id, created_at=now, updated_at=now)

    async def get_conversation(self, conversation_id: int) -> Optional[Conversation]:
        """Get conversation by ID."""
        async with self._connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Conversation(**dict(row))
            return None

    async def get_user_conversations(self, user_id: str, limit: int = 10) -> list[Conversation]:
        """Get user's recent conversations."""
        async with self._connection.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [Conversation(**dict(row)) for row in rows]

    async def get_latest_conversation(self, user_id: str) -> Optional[Conversation]:
        """Get user's most recent conversation."""
        conversations = await self.get_user_conversations(user_id, limit=1)
        return conversations[0] if conversations else None

    async def update_conversation_session(self, conversation_id: int, session_id: str):
        """Update conversation's Claude session ID."""
        await self._connection.execute(
            "UPDATE conversations SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, datetime.utcnow(), conversation_id)
        )
        await self._connection.commit()

    async def update_conversation_timestamp(self, conversation_id: int):
        """Update conversation's updated_at timestamp."""
        await self._connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (datetime.utcnow(), conversation_id)
        )
        await self._connection.commit()

    # ============== Message Operations ==============

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        tool_uses: Optional[list] = None
    ) -> Message:
        """Add a message to a conversation."""
        now = datetime.utcnow()
        tool_uses_json = json.dumps(tool_uses) if tool_uses else None
        cursor = await self._connection.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_uses, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_uses_json, now)
        )
        await self._connection.commit()

        # Also update conversation timestamp
        await self.update_conversation_timestamp(conversation_id)

        return Message(
            id=cursor.lastrowid,
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_uses=tool_uses,
            created_at=now
        )

    async def get_conversation_messages(self, conversation_id: int, limit: int = 100) -> list[Message]:
        """Get messages for a conversation."""
        async with self._connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            messages = []
            for row in rows:
                row_dict = dict(row)
                if row_dict.get('tool_uses'):
                    row_dict['tool_uses'] = json.loads(row_dict['tool_uses'])
                messages.append(Message(**row_dict))
            return messages


# Global database instance
_db: Optional[Database] = None


async def get_database() -> Database:
    """Get or create database instance."""
    global _db
    if _db is None:
        _db = Database()
        await _db.connect()
    return _db


async def close_database():
    """Close database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None
