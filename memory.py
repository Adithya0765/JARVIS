import sqlite3
import json
import time
import uuid
import asyncio
import os
from collections import deque
from pathlib import Path
from typing import Optional

from google import genai

DB_PATH = os.path.join(os.path.dirname(__file__), "jarvis_memory.db")
MEMORY_INJECTION_LIMIT = 15


class MemoryService:
    def __init__(self, db_path: str = DB_PATH, api_key: Optional[str] = None) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_queue: deque[tuple[str, str, str, float]] = deque()
        self._worker_task: Optional[asyncio.Task] = None
        self._extraction_task: Optional[asyncio.Task] = None
        self._pending_extractions: deque[str] = deque()
        self._running = False

        if api_key:
            self._genai_client = genai.Client(api_key=api_key)
        else:
            api_key_from_env = os.environ.get("GOOGLE_API_KEY", "")
            if api_key_from_env:
                self._genai_client = genai.Client(api_key=api_key_from_env)
            else:
                self._genai_client = None
                import warnings
                warnings.warn("No GOOGLE_API_KEY set. Memory extraction is disabled.")

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                started_at  REAL NOT NULL,
                ended_at    REAL,
                summary     TEXT,
                turn_count  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversation_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_items_session ON conversation_items(session_id, created_at);

            CREATE TABLE IF NOT EXISTS memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                confidence  REAL DEFAULT 1.0,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                UNIQUE(key, category)
            );

            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
        """)
        conn.commit()

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._write_worker())
        self._extraction_task = asyncio.create_task(self._extraction_worker())

    async def stop(self) -> None:
        self._running = False
        self._flush_write_queue()
        if self._worker_task:
            self._worker_task.cancel()
        if self._extraction_task:
            self._extraction_task.cancel()
        if self._conn:
            self._conn.close()
            self._conn = None

    def _flush_write_queue(self) -> None:
        if not self._write_queue:
            return
        conn = self._get_conn()
        cursor = conn.cursor()
        while self._write_queue:
            session_id, role, content, timestamp = self._write_queue.popleft()
            cursor.execute(
                "INSERT INTO conversation_items (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, timestamp),
            )
        conn.commit()

    async def _write_worker(self) -> None:
        while self._running:
            await asyncio.sleep(2.0)
            try:
                self._flush_write_queue()
            except Exception:
                pass

    async def _extraction_worker(self) -> None:
        while self._running:
            await asyncio.sleep(30.0)
            if not self._pending_extractions or not self._genai_client:
                continue
            while self._pending_extractions:
                session_id = self._pending_extractions.popleft()
                try:
                    await self._extract_memories_from_session(session_id)
                except Exception:
                    pass

    def log_turn(self, session_id: str, role: str, content: str) -> None:
        self._write_queue.append((session_id, role, content, time.time()))

    def mark_session_start(self) -> str:
        session_id = str(uuid.uuid4())
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, started_at) VALUES (?, ?)",
            (session_id, time.time()),
        )
        conn.commit()
        return session_id

    def mark_session_end(self, session_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        conn.commit()
        self._flush_write_queue()
        self._pending_extractions.append(session_id)

    def load_memories(self, limit: int = MEMORY_INJECTION_LIMIT) -> list[dict[str, str]]:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT key, value, category FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        results = cursor.fetchall()
        return [{"key": row["key"], "value": row["value"], "category": row["category"]} for row in results]

    def load_recent_history(self, session_limit: int = 3, turn_limit: int = 10) -> list[dict[str, str]]:
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT ci.role, ci.content, ci.created_at
            FROM conversation_items ci
            JOIN (
                SELECT DISTINCT session_id FROM conversation_items
                ORDER BY created_at DESC
                LIMIT ?
            ) recent ON ci.session_id = recent.session_id
            ORDER BY ci.created_at DESC
            LIMIT ?
        """, (session_limit, turn_limit))
        results = cursor.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in results][::-1]

    def build_memory_context(self) -> str:
        memories = self.load_memories()
        history = self.load_recent_history()

        parts = []

        if memories:
            parts.append("[Memorised Information]")
            for m in memories:
                parts.append(f"- {m['key']}: {m['value']}")
            parts.append("")

        if history:
            parts.append("[Recent Conversation History]")
            for h in history:
                label = "User" if h["role"] == "user" else "You"
                parts.append(f"{label}: {h['content']}")
            parts.append("")

        return "\n".join(parts) if parts else ""

    async def _extract_memories_from_session(self, session_id: str) -> None:
        if not self._genai_client:
            return

        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT role, content FROM conversation_items WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        items = cursor.fetchall()
        if len(items) < 2:
            return

        transcript = "\n".join(
            f"{'User' if r['role'] == 'user' else 'Assistant'}: {r['content']}"
            for r in items
        )

        try:
            response = await asyncio.to_thread(
                self._genai_client.models.generate_content,
                model="gemini-2.0-flash",
                contents=(
                    "Extract key facts about the user from this conversation transcript. "
                    "Return ONLY a JSON array of objects with keys 'key', 'value', and 'category'. "
                    "Categories should be one of: personal_info, preference, project, fact, behaviour. "
                    "Do not include assistant identity facts. Only facts about the user. "
                    "Example: [{\"key\": \"User's name\", \"value\": \"John\", \"category\": \"personal_info\"}]\n\n"
                    f"{transcript}"
                ),
            )

            text = response.text.strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            extracted = json.loads(text)

            now = time.time()
            conn = self._get_conn()
            for mem in extracted:
                conn.execute(
                    """INSERT INTO memories (key, value, category, confidence, created_at, updated_at)
                       VALUES (?, ?, ?, 1.0, ?, ?)
                       ON CONFLICT(key, category) DO UPDATE SET
                           value = excluded.value,
                           updated_at = excluded.updated_at,
                           confidence = MIN(1.0, memories.confidence + 0.1)""",
                    (mem["key"], mem["value"], mem["category"], now, now),
                )
            conn.commit()

            session_summary = await asyncio.to_thread(
                self._genai_client.models.generate_content,
                model="gemini-2.0-flash",
                contents=f"Summarise this conversation in 1-2 sentences:\n{transcript}",
            )
            summary_text = session_summary.text.strip()
            conn.execute(
                "UPDATE sessions SET summary = ? WHERE id = ?",
                (summary_text, session_id),
            )
            conn.commit()

        except Exception:
            pass
