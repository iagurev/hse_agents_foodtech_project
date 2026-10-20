import asyncio
import hashlib
import json
import secrets
import time
from uuid import uuid4

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException
from pydantic import JsonValue

from app.storage import Store
from app.web.contracts import Profile, Registration

_hasher = PasswordHasher()


class WebStore(Store):
    """Дополняет доменное хранилище аккаунтами и состоянием веб-интерфейса."""

    async def initialize(self) -> None:
        await super().initialize()
        async with self.connection() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(
                  user_id TEXT PRIMARY KEY REFERENCES users(user_id), login TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES accounts(user_id),
                  expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_meta(
                  chat_id TEXT PRIMARY KEY REFERENCES chats(chat_id), title TEXT NOT NULL,
                  updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_runs(
                  chat_id TEXT NOT NULL REFERENCES chats(chat_id), turn_id TEXT NOT NULL,
                  text TEXT NOT NULL, state TEXT NOT NULL, output TEXT NOT NULL DEFAULT '',
                  report TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                  PRIMARY KEY(chat_id,turn_id));
                CREATE INDEX IF NOT EXISTS object_parent ON objects(parent_id);
                CREATE TABLE IF NOT EXISTS purchased(
                  object_id TEXT NOT NULL, sku_id TEXT NOT NULL, PRIMARY KEY(object_id,sku_id));
            """)
            columns = await (await db.execute("PRAGMA table_info(turns)")).fetchall()
            if "model_visible" not in {r["name"] for r in columns}:
                await db.execute("ALTER TABLE turns ADD COLUMN model_visible INTEGER NOT NULL DEFAULT 1")
            columns = await (await db.execute("PRAGMA table_info(web_runs)")).fetchall()
            if "references_json" not in {r["name"] for r in columns}:
                await db.execute("ALTER TABLE web_runs ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'")
            await db.execute(
                "UPDATE web_runs SET state='interrupted',output=? WHERE state IN ('queued','guarding','running')",
                ("Выполнение прервано перезапуском приложения. Сохранённые результаты доступны.",),
            )
            await db.commit()

    async def register(self, value: Registration) -> str:
        """Создаёт аккаунт и анкету одной транзакцией.

        :param value: Проверенные поля регистрации
        :return: ID пользователя
        """
        user_id = uuid4().hex
        password_hash = await asyncio.to_thread(_hasher.hash, value.password)
        try:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO users(user_id,questionnaire) VALUES(?,?)",
                    (user_id, value.profile.model_dump_json()),
                )
                await db.execute("INSERT INTO accounts VALUES(?,?,?)", (user_id, value.login, password_hash))
                await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise HTTPException(409, "Этот логин уже занят") from exc
        return user_id

    async def authenticate(self, login: str, password: str) -> str:
        """Проверяет пароль, не раскрывая существование аккаунта."""
        async with self.connection() as db:
            row = await (await db.execute("SELECT * FROM accounts WHERE login=?", (login,))).fetchone()
        # Одинаковая дорогостоящая проверка для неизвестного логина.
        encoded = (
            row["password_hash"] if row else await asyncio.to_thread(_hasher.hash, secrets.token_hex(16))
        )
        try:
            await asyncio.to_thread(_hasher.verify, encoded, password)
        except VerificationError as exc:
            raise HTTPException(401, "Неверный логин или пароль") from exc
        if not row:
            raise HTTPException(401, "Неверный логин или пароль")
        return row["user_id"]

    async def issue_session(self, user_id: str, days: int) -> str:
        token = secrets.token_urlsafe(32)
        async with self.connection() as db:
            await db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            await db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + days * 86400),
            )
            await db.commit()
        return token

    async def session_user(self, token: str | None) -> str:
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (digest, time.time())
                )
            ).fetchone()
        if not row:
            raise HTTPException(401, "Войдите в аккаунт")
        return row["user_id"]

    async def logout(self, token: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )
            await db.commit()

    async def me(self, user_id: str) -> dict[str, JsonValue]:
        person = await self.person(user_id)
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT login FROM accounts WHERE user_id=?", (user_id,))
            ).fetchone()
        return {
            "user_id": user_id,
            "login": row["login"],
            "profile": person.questionnaire,
            "memory": person.memory,
            "memory_revision": person.memory_revision,
        }

    async def chat(self, user_id: str, chat_id: str) -> None:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT 1 FROM chats WHERE user_id=? AND chat_id=?", (user_id, chat_id))
            ).fetchone()
        if not row:
            raise HTTPException(404, "Чат не найден")

    async def create_chat(self, user_id: str) -> dict[str, str]:
        chat_id = uuid4().hex
        await self.ensure_chat(user_id, chat_id)
        async with self.connection() as db:
            await db.execute("INSERT INTO chat_meta VALUES(?,?,?)", (chat_id, "Новый чат", time.time()))
            await db.commit()
        return {"chat_id": chat_id, "title": "Новый чат"}

    async def chats(self, user_id: str) -> list[dict[str, JsonValue]]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """SELECT c.chat_id,COALESCE(m.title,'Чат') title,
                EXISTS(SELECT 1 FROM web_runs r WHERE r.chat_id=c.chat_id
                  AND state IN ('queued','guarding','running')) active
                FROM chats c LEFT JOIN chat_meta m USING(chat_id) WHERE c.user_id=?
                ORDER BY m.updated_at DESC,c.rowid DESC""",
                    (user_id,),
                )
            ).fetchall()
        return [dict(r) | {"active": bool(r["active"])} for r in rows]

    async def rename_chat(self, chat_id: str, title: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO chat_meta VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at",
                (chat_id, title, time.time()),
            )
            await db.commit()

    async def delete_chat(self, chat_id: str) -> None:
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for table in ("web_runs", "turns", "chat_meta", "chats"):
                await db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
            await db.commit()

    async def history(self, chat_id: str) -> tuple[str, int, list[dict[str, JsonValue]]]:
        summary, through, rows = await super().history(chat_id)
        return summary, through, [r for r in rows if r.get("model_visible", 1)]

    async def latest(self, user_id: str, kind: str) -> list[dict[str, JsonValue]]:
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """WITH RECURSIVE versions AS (
                        SELECT object_id,object_id AS root_id,rowid AS sequence,payload FROM objects
                        WHERE user_id=? AND kind=? AND parent_id IS NULL
                        UNION ALL SELECT o.object_id,v.root_id,o.rowid,o.payload FROM objects o
                        JOIN versions v ON o.parent_id=v.object_id
                    ) SELECT payload FROM versions JOIN object_meta USING(root_id)
                    WHERE hidden=0 AND sequence IN (SELECT MAX(sequence) FROM versions GROUP BY root_id)
                    ORDER BY sequence DESC""",
                    (user_id, kind),
                )
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    async def require_current(self, user_id: str, kind: str, object_id: str) -> dict[str, JsonValue]:
        result = await self.get_object(user_id, kind, object_id)
        if not isinstance(result.data, dict):
            raise HTTPException(404, "Объект не найден")
        async with self.connection() as db:
            head = await self.current_object_id(db, object_id)
        if head != object_id:
            raise HTTPException(
                409, "Объект уже изменился. Откройте актуальную версию; ваши правки сохранены в форме."
            )
        return result.data

    async def profile(self, user_id: str, value: Profile) -> None:
        await self.set_questionnaire(user_id, value.model_dump(mode="json"))
