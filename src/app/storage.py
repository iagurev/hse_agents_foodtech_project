import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import aiosqlite
from pydantic import JsonValue

from app.models import PersonContext, Product, Result


class Store:
    """SQLite-хранилище с транзакционными версиями и журналом операций."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Открывает соединение без общих незавершённых транзакций.

        :return: Соединение SQLite
        """
        async with aiosqlite.connect(self.path, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def initialize(self) -> None:
        """Создаёт схему при явном запуске."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connection() as db:
            await db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(
                  user_id TEXT PRIMARY KEY, questionnaire TEXT NOT NULL DEFAULT '{}',
                  memory TEXT NOT NULL DEFAULT '', memory_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS chats(
                  chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                  summary TEXT NOT NULL DEFAULT '', through_turn INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS turns(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(chat_id),
                  turn_id TEXT NOT NULL, messages TEXT NOT NULL, complete INTEGER NOT NULL,
                  UNIQUE(chat_id,turn_id));
                CREATE TABLE IF NOT EXISTS objects(
                  object_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                  parent_id TEXT, payload TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS purchased(
                  object_id TEXT NOT NULL, sku_id TEXT NOT NULL, PRIMARY KEY(object_id,sku_id));
                CREATE INDEX IF NOT EXISTS object_parent ON objects(parent_id);
                CREATE TABLE IF NOT EXISTS object_meta(
                  root_id TEXT PRIMARY KEY REFERENCES objects(object_id),
                  title TEXT, hidden INTEGER NOT NULL DEFAULT 0, created_at REAL);
                INSERT OR IGNORE INTO object_meta(root_id)
                  SELECT object_id FROM objects WHERE parent_id IS NULL;
                CREATE TABLE IF NOT EXISTS operations(
                  user_id TEXT NOT NULL, operation_id TEXT NOT NULL, result TEXT NOT NULL,
                  PRIMARY KEY(user_id,operation_id));
                CREATE TABLE IF NOT EXISTS sku_cache(
                  cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sku_records(
                  sku_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
            """)
            turn_columns = await (await db.execute("PRAGMA table_info(turns)")).fetchall()
            if "model_visible" not in {r["name"] for r in turn_columns}:
                await db.execute("ALTER TABLE turns ADD COLUMN model_visible INTEGER NOT NULL DEFAULT 1")
            await db.commit()

    async def cache_skus(self, products: list[Product]) -> None:
        """Сохраняет фактические данные SKU из интеграции, не из ответа модели.

        :param products: Товары поиска, аналогов или внутреннего подбора
        """
        if not products:
            return
        async with self.connection() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO sku_records VALUES(?,?,?)",
                [(p.sku_id, p.model_dump_json(), time.time()) for p in products],
            )
            await db.commit()

    async def get_skus(self, sku_ids: list[str]) -> dict[str, Product]:
        """Получает известные SKU без повторного поиска и догадок о фасовке.

        :param sku_ids: Идентификаторы товаров
        :return: Найденные товары по ID
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT payload FROM sku_records WHERE sku_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(sku_ids),),
                )
            ).fetchall()
        products = [Product.model_validate_json(row["payload"]) for row in rows]
        return {p.sku_id: p for p in products}

    async def mentioned_skus(self, text: str) -> dict[str, Product]:
        """Получает данные только явно названных в поручении SKU.

        :param text: Самодостаточное поручение редактору
        :return: Известные упомянутые SKU
        """
        return await self.get_skus(re.findall(r"[\w-]+", text))

    async def ensure_chat(self, user_id: str, chat_id: str) -> None:
        """Создаёт чат либо проверяет владельца существующего.

        :param user_id: Авторизованный пользователь runtime
        :param chat_id: Идентификатор диалога
        """
        async with self.connection() as db:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
            await db.execute("INSERT OR IGNORE INTO chats(chat_id,user_id) VALUES(?,?)", (chat_id, user_id))
            row = await (await db.execute("SELECT user_id FROM chats WHERE chat_id=?", (chat_id,))).fetchone()
            if row["user_id"] != user_id:
                raise PermissionError("Чат принадлежит другому пользователю")
            await db.commit()

    async def person(self, user_id: str) -> PersonContext:
        """Загружает актуальную анкету и память.

        :param user_id: Пользователь
        :return: Независимый снимок контекста
        """
        async with self.connection() as db:
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
        if row is None:
            raise LookupError("Пользователь не найден")
        return PersonContext(
            questionnaire=json.loads(row["questionnaire"]),
            memory=row["memory"],
            memory_revision=row["memory_revision"],
        )

    async def set_questionnaire(self, user_id: str, value: dict[str, JsonValue]) -> None:
        """Сохраняет анкету через интерфейс приложения.

        :param user_id: Пользователь
        :param value: Явно предоставленные сведения
        """
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO users(user_id,questionnaire) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET questionnaire=excluded.questionnaire",
                (user_id, json.dumps(value)),
            )
            await db.commit()

    async def update_memory(
        self, user_id: str, revision: int, text: str, operation_id: str | None = None
    ) -> Result:
        """Сохраняет редакцию только при совпадении версии.

        :param user_id: Пользователь из runtime
        :param revision: Версия прочитанной памяти
        :param text: Полная новая редакция
        :param operation_id: Ключ повторной доставки при вызове через tool
        :return: Подтверждение либо текущая редакция при конфликте
        """
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if operation_id:
                previous = await (
                    await db.execute(
                        "SELECT result FROM operations WHERE user_id=? AND operation_id=?",
                        (user_id, operation_id),
                    )
                ).fetchone()
                if previous:
                    return Result.model_validate_json(previous["result"])
            cursor = await db.execute(
                "UPDATE users SET memory=?,memory_revision=memory_revision+1 WHERE user_id=? AND memory_revision=?",
                (text, user_id, revision),
            )
            row = await (
                await db.execute("SELECT memory,memory_revision FROM users WHERE user_id=?", (user_id,))
            ).fetchone()
            data = dict(row)
            result = Result(data=data)
            if cursor.rowcount == 0:
                result = Result.failed(
                    "MEMORY_CONFLICT", "Память изменилась в другом чате; объедините с текущей редакцией"
                )
                result.data = data
            elif operation_id:
                await db.execute(
                    "INSERT INTO operations VALUES(?,?,?)", (user_id, operation_id, result.model_dump_json())
                )
            await db.commit()
        return result

    async def replay(self, user_id: str, operation_id: str) -> Result | None:
        """Возвращает результат уже записанной операции.

        :param user_id: Пользователь
        :param operation_id: Стабильный ключ повторной доставки
        :return: Результат либо отсутствие записи
        """
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT result FROM operations WHERE user_id=? AND operation_id=?",
                    (user_id, operation_id),
                )
            ).fetchone()
        return Result.model_validate_json(row["result"]) if row else None

    @staticmethod
    async def current_object_id(db: aiosqlite.Connection, object_id: str) -> str | None:
        """Находит последнюю сохранённую версию всей цепочки, включая ответвления.

        :param db: Соединение текущей транзакции
        :param object_id: Любая версия объекта
        :return: ID последней версии либо None
        """
        row = await (
            await db.execute(
                """WITH RECURSIVE ancestors AS (
                    SELECT object_id,parent_id FROM objects WHERE object_id=?
                    UNION ALL SELECT o.object_id,o.parent_id FROM objects o
                    JOIN ancestors a ON o.object_id=a.parent_id
                ), family AS (
                    SELECT object_id,rowid AS sequence FROM objects
                    WHERE object_id IN (SELECT object_id FROM ancestors WHERE parent_id IS NULL)
                    UNION ALL SELECT o.object_id,o.rowid FROM objects o
                    JOIN family f ON o.parent_id=f.object_id
                ) SELECT object_id FROM family ORDER BY sequence DESC LIMIT 1""",
                (object_id,),
            )
        ).fetchone()
        return row["object_id"] if row else None

    async def save_object(
        self,
        user_id: str,
        kind: Literal["plan", "cart"],
        payload: dict[str, JsonValue],
        result: Result,
        operation_id: str,
        parent_id: str | None = None,
    ) -> Result:
        """Атомарно сохраняет новую версию и результат операции; прежняя версия неизменна.

        :param user_id: Владелец
        :param kind: Тип объекта
        :param payload: Содержимое новой версии
        :param result: Статус и замечания
        :param operation_id: Ключ идемпотентности
        :param parent_id: Исходная версия при редактировании
        :return: Адресуемый результат
        """
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT result FROM operations WHERE user_id=? AND operation_id=?",
                    (user_id, operation_id),
                )
            ).fetchone()
            if row:
                return Result.model_validate_json(row["result"])
            if parent_id:
                parent = await (
                    await db.execute(
                        "SELECT payload FROM objects WHERE object_id=? AND user_id=? AND kind=?",
                        (parent_id, user_id, kind),
                    )
                ).fetchone()
                if not parent:
                    return Result.failed("NOT_FOUND", "Исходная версия не найдена")
                head = await self.current_object_id(db, parent_id)
                if head != parent_id and operation_id.startswith("web:"):
                    return Result.failed(
                        "VERSION_CONFLICT", "Объект уже изменился; получите актуальную версию"
                    )
            object_id = f"{kind}_{uuid4().hex[:16]}"
            payload = payload | {f"{kind}_id": object_id, "parent_id": parent_id, "status": result.status}
            result = result.model_copy(update={"data": payload})
            await db.execute(
                "INSERT INTO objects VALUES(?,?,?,?,?,?)",
                (object_id, user_id, kind, parent_id, result.model_dump_json(), result.status),
            )
            if parent_id is None:
                await db.execute(
                    "INSERT INTO object_meta(root_id,created_at) VALUES(?,?)", (object_id, time.time())
                )
            if kind == "cart" and parent_id:
                old_items = json.loads(parent["payload"])["data"].get("items", [])
                old_quantities = {}
                for item in old_items:
                    sku_id = item.get("sku_id") or item.get("product", {}).get("sku_id")
                    old_quantities[sku_id] = old_quantities.get(sku_id, 0) + item["packages"]
                new_quantities = {}
                for item in payload.get("items", []):
                    new_quantities[item["sku_id"]] = new_quantities.get(item["sku_id"], 0) + item["packages"]
                for sku_id, count in new_quantities.items():
                    if old_quantities.get(sku_id) == count:
                        await db.execute(
                            "INSERT OR IGNORE INTO purchased SELECT ?,sku_id FROM purchased WHERE object_id=? AND sku_id=?",
                            (object_id, parent_id, sku_id),
                        )
            await db.execute(
                "INSERT INTO operations VALUES(?,?,?)", (user_id, operation_id, result.model_dump_json())
            )
            await db.commit()
        return result

    async def get_object(self, user_id: str, kind: str, object_id: str) -> Result:
        """Получает объект с проверкой владельца и типа.

        :param user_id: Владелец
        :param kind: Тип
        :param object_id: ID версии
        :return: Сохранённый результат или ошибка
        """
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM objects WHERE user_id=? AND kind=? AND object_id=?",
                    (user_id, kind, object_id),
                )
            ).fetchone()
        return (
            Result.model_validate_json(row["payload"])
            if row
            else Result.failed("NOT_FOUND", "Объект не найден")
        )

    async def list_objects(self, user_id: str, kind: str) -> Result:
        """Возвращает метаданные нескрытых версий без усечения.

        :param user_id: Владелец
        :param kind: Тип объекта
        :return: Список метаданных
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT object_id,parent_id,status FROM objects WHERE user_id=? AND kind=? ORDER BY rowid DESC",
                    (user_id, kind),
                )
            ).fetchall()
        metadata = await self.object_metadata(user_id)
        return Result(
            data=[
                {f"{kind}_id": r["object_id"], "parent_id": r["parent_id"], "status": r["status"]}
                for r in rows
                if not metadata[r["object_id"]]["hidden"]
            ]
        )

    async def object_metadata(self, user_id: str) -> dict[str, dict]:
        """Разрешает корни версий и их общие метаданные одним запросом.

        :param user_id: Владелец объектов
        :return: Метаданные по ID версии; root_data содержит исходный объект для названия
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    """WITH RECURSIVE versions AS (
                    SELECT object_id,object_id AS root_id FROM objects
                    WHERE user_id=? AND parent_id IS NULL
                    UNION ALL SELECT o.object_id,v.root_id FROM objects o
                    JOIN versions v ON o.parent_id=v.object_id WHERE o.user_id=?
                ) SELECT v.object_id,v.root_id,m.title,m.hidden,m.created_at,o.payload,version.rowid AS sequence
                FROM versions v JOIN object_meta m ON m.root_id=v.root_id
                JOIN objects o ON o.object_id=v.root_id JOIN objects version ON version.object_id=v.object_id""",
                    (user_id, user_id),
                )
            ).fetchall()
        roots = {}
        result = {}
        for row in rows:
            root = row["root_id"]
            if root not in roots:
                roots[root] = {key: row[key] for key in ("root_id", "title", "hidden", "created_at")}
                roots[root]["root_data"] = json.loads(row["payload"])["data"]
            result[row["object_id"]] = roots[root] | {"sequence": row["sequence"]}
        return result

    async def set_object_metadata(
        self, user_id: str, kind: str, object_id: str, *, title: str | None = None, hide: bool = False
    ) -> bool:
        """Меняет только метаданные цепочки с проверкой владельца и типа.

        :param user_id: Владелец
        :param kind: plan или cart
        :param object_id: Любая версия
        :param title: Новое проверенное название либо отсутствие изменения
        :param hide: Скрыть цепочку; повтор не меняет результат
        :return: Найден ли принадлежащий пользователю объект
        """
        async with self.connection() as db:
            cursor = await db.execute(
                """WITH RECURSIVE ancestors AS (
                    SELECT object_id,parent_id FROM objects WHERE object_id=? AND user_id=? AND kind=?
                    UNION ALL SELECT o.object_id,o.parent_id FROM objects o
                    JOIN ancestors a ON o.object_id=a.parent_id
                ) UPDATE object_meta SET title=COALESCE(?,title),hidden=MAX(hidden,?)
                WHERE root_id IN (SELECT object_id FROM ancestors WHERE parent_id IS NULL)
                RETURNING root_id""",
                (object_id, user_id, kind, title, int(hide)),
            )
            found = await cursor.fetchone()
            await cursor.close()
            await db.commit()
        return found is not None

    async def history(self, chat_id: str) -> tuple[str, int, list[dict[str, JsonValue]]]:
        """Читает сводку и несжатый хвост целыми ходами.

        :param chat_id: Проверенный runtime идентификатор
        :return: Сводка, граница и ходы
        """
        async with self.connection() as db:
            chat = await (await db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))).fetchone()
            rows = await (
                await db.execute(
                    "SELECT * FROM turns WHERE chat_id=? AND seq>? AND model_visible=1 ORDER BY seq",
                    (chat_id, chat["through_turn"]),
                )
            ).fetchall()
        return chat["summary"], chat["through_turn"], [dict(r) for r in rows]

    async def save_turn(self, chat_id: str, turn_id: str, messages: str, complete: bool) -> None:
        """Записывает только новые сообщения текущего хода.

        :param chat_id: Диалог
        :param turn_id: Ключ доставки
        :param messages: Сериализованные SDK-сообщения
        :param complete: Закончен ли цикл
        """
        async with self.connection() as db:
            await db.execute(
                "INSERT OR IGNORE INTO turns(chat_id,turn_id,messages,complete) VALUES(?,?,?,?)",
                (chat_id, turn_id, messages, int(complete)),
            )
            await db.commit()

    async def save_summary(self, chat_id: str, through_turn: int, summary: str) -> None:
        """Обновляет производную сводку, не удаляя журнал.

        :param chat_id: Диалог
        :param through_turn: Последний полностью включённый ход
        :param summary: Новый текст
        """
        async with self.connection() as db:
            await db.execute(
                "UPDATE chats SET summary=?,through_turn=? WHERE chat_id=?", (summary, through_turn, chat_id)
            )
            await db.commit()
