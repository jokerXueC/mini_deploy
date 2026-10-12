"""Private, paginated chat history for the panel's single administrator."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time


class HistoryError(ValueError):
    pass


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "history.sqlite3"

    @contextmanager
    def _db(self):
        connection = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.is_symlink() or self.path.is_symlink():
                raise HistoryError("对话历史路径不可为符号链接，原文件已保留。")
            if not self.path.exists():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                except FileExistsError:
                    pass
            if os.name != "nt":
                for path in (self.directory, self.path):
                    info = path.stat()
                    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise HistoryError("对话历史权限不安全，请设置目录 700、文件 600；原文件已保留。")
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, target TEXT NOT NULL,
                    model TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY, job TEXT UNIQUE NOT NULL,
                    conversation TEXT NOT NULL REFERENCES conversations(id),
                    question TEXT NOT NULL, answer TEXT NOT NULL, context TEXT NOT NULL,
                    events TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS history_turns ON turns(conversation, id);
                CREATE INDEX IF NOT EXISTS history_recent ON conversations(updated DESC, id DESC);
            """)
            with connection:
                yield connection
        except (OSError, sqlite3.Error, ValueError) as exc:
            if isinstance(exc, HistoryError):
                raise
            raise HistoryError("对话历史无法读写，请检查数据目录和磁盘空间；原记录已保留。") from None
        finally:
            if connection is not None:
                connection.close()

    def begin(self, key, job, question, target, model, context):
        now = time.time()
        with self._db() as db:
            current = db.execute("SELECT target FROM conversations WHERE id=?", (key,)).fetchone()
            if current is not None and current["target"] and current["target"] != target:
                raise HistoryError("请先连接历史对话对应的服务器，或新建对话。")
            db.execute("INSERT OR IGNORE INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
                       (key, question[:80], target, model, now, now))
            db.execute("INSERT INTO turns(job, conversation, question, answer, context, events, status, created) "
                       "VALUES (?, ?, ?, '', ?, '[]', 'running', ?)", (job, key, question, context, now))
            db.execute("UPDATE conversations SET updated=?, model=?, target=CASE WHEN target='' THEN ? ELSE target END "
                       "WHERE id=?", (now, model, target, key))

    def finish(self, job, answer, status, events, context=None):
        with self._db() as db:
            db.execute("UPDATE turns SET answer=?, status=?, events=?, context=COALESCE(?, context) WHERE job=?",
                       (answer, status, json.dumps(events, ensure_ascii=False), context, job))
            db.execute("UPDATE conversations SET updated=? WHERE id=(SELECT conversation FROM turns WHERE job=?)",
                       (time.time(), job))

    def checkpoint(self, job, events):
        with self._db() as db:
            db.execute('UPDATE turns SET events=? WHERE job=?',
                       (json.dumps(events, ensure_ascii=False), job))

    def recovery(self, job, event_id, target):
        with self._db() as db:
            row = db.execute('SELECT turns.events, turns.conversation, conversations.target FROM turns '
                             'JOIN conversations ON conversations.id=turns.conversation WHERE job=?', (job,)).fetchone()
            if row is None or not target or row['target'] != target:
                raise HistoryError('备份记录不属于当前服务器，请连接原服务器后恢复。')
            event = next((e for e in json.loads(row['events']) if e['id'] == event_id), None)
            if not event or not event.get('recovery'):
                raise HistoryError('没有找到可恢复的备份记录；原文件未修改。')
            return row['conversation'], event['recovery']

    def list(self, offset=0):
        if not self.path.exists() and not self.path.is_symlink():
            return {"conversations": [], "next_offset": None}
        with self._db() as db:
            rows = db.execute("SELECT * FROM conversations ORDER BY updated DESC, id DESC LIMIT 41 OFFSET ?",
                              (offset,)).fetchall()
            return {"conversations": [dict(row) for row in rows[:40]],
                    "next_offset": offset + 40 if len(rows) > 40 else None}

    def detail(self, key, before=None):
        if not self.path.exists() and not self.path.is_symlink():
            raise HistoryError("对话历史不存在。")
        with self._db() as db:
            conversation = db.execute("SELECT * FROM conversations WHERE id=?", (key,)).fetchone()
            if conversation is None:
                raise HistoryError("对话历史不存在。")
            rows = db.execute("SELECT * FROM turns WHERE conversation=? AND (? IS NULL OR id<?) "
                              "ORDER BY id DESC LIMIT 51", (key, before, before)).fetchall()
            turns = [{k: row[k] for k in ("id", "question", "answer", "status", "created")} |
                     {"events": json.loads(row["events"])} for row in reversed(rows[:50])]
            return {"conversation": dict(conversation), "turns": turns,
                    "next_before": rows[49]["id"] if len(rows) > 50 else None}

    def context(self, key, target):
        with self._db() as db:
            conversation = db.execute("SELECT target FROM conversations WHERE id=?", (key,)).fetchone()
            if conversation is None:
                raise HistoryError("对话历史不存在。")
            if conversation["target"] and conversation["target"] != target:
                raise HistoryError("请先连接历史对话对应的服务器，或新建对话。")
            rows = db.execute("SELECT context, answer FROM turns WHERE conversation=? AND status='done' "
                              "ORDER BY id DESC LIMIT 6", (key,)).fetchall()
            messages = []
            for row in reversed(rows):
                messages.extend([{"role": "user", "content": row["context"][:18000]},
                                 {"role": "assistant", "content": row["answer"][:12000]}])
            while sum(len(m["content"]) for m in messages) > 32000:
                del messages[:2]
            return messages
