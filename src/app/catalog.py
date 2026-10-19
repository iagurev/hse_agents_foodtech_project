import gzip
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.models import NormalizedProduct, Recipe, Result


class Catalog:
    """Подготовленные рецепты и справочник продуктов с заменяемым поиском."""

    def __init__(self, recipes_path: Path, products_path: Path, sources_path: Path) -> None:
        self.recipes_path = recipes_path
        self.products_path = products_path
        self.sources_path = sources_path

    async def validate(self) -> None:
        """Проверяет наличие и принадлежность трёх файлов одному выпуску.

        :return: Ничего; отсутствие или несовместимость вызывают явную ошибку
        """
        releases = set()
        for kind, path in [
            ("recipes", self.recipes_path),
            ("products", self.products_path),
            ("sources", self.sources_path),
        ]:
            if not path.is_file():
                raise ValueError(f"Финальная БД {kind} не найдена: {path}")
            with path.open("rb") as source:
                if source.read(16) != b"SQLite format 3\x00":
                    raise ValueError(f"Файл {path} не является SQLite. Для Git LFS выполните git lfs pull.")
            async with aiosqlite.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                metadata = dict(await (await db.execute("SELECT key,value FROM metadata")).fetchall())
                if metadata.get("kind") != kind or metadata.get("schema_version") != "1":
                    raise ValueError(f"Неподдерживаемая схема БД {kind}")
                if not metadata.get("release_id"):
                    raise ValueError(f"БД {kind} не содержит ID выпуска")
                releases.add(metadata["release_id"])
        if len(releases) != 1:
            raise ValueError("Базы рецептов, продуктов и исходников относятся к разным выпускам")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Открывает три финальных файла исключительно для чтения.

        :return: Соединение с рецептами и подключёнными продуктами/исходниками
        """
        async with aiosqlite.connect(self.recipes_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = aiosqlite.Row
            for schema, path in [("product_data", self.products_path), ("source_data", self.sources_path)]:
                await db.execute(f"ATTACH DATABASE ? AS {schema}", (path.resolve().as_uri() + "?mode=ro",))
            await db.execute("PRAGMA query_only=ON")
            yield db

    async def source_document(self, reference: str) -> bytes | None:
        """Читает исходный документ по сохранённому в evidence ключу.

        :param reference: Прежний относительный путь как устойчивый ключ, без доступа к файловой системе
        :return: Исходные байты после распаковки или None
        """
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT content,encoding,sha256 FROM documents WHERE reference=COALESCE("
                    "(SELECT destination FROM document_aliases WHERE reference=?),?)",
                    (reference, reference),
                )
            ).fetchone()
        if row is None:
            return None
        content = gzip.decompress(row["content"]) if row["encoding"] == "gzip" else row["content"]
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("Контрольная сумма исходного документа не совпадает")
        return content

    async def products(self) -> dict[str, NormalizedProduct]:
        """Читает существующие нормализованные продукты.

        :return: Справочник по product_id; диалог его не пополняет
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT product_id,payload FROM products WHERE payload IS NOT NULL ORDER BY product_id"
                )
            ).fetchall()
        return {r["product_id"]: NormalizedProduct.model_validate_json(r["payload"]) for r in rows}

    async def product_sources(self, product_id: str) -> dict | None:
        """Читает происхождение цены, КБЖУ и полный список исходных SKU категории.

        :param product_id: Идентификатор общего продукта
        :return: Наблюдения на совместимой базе либо отсутствие сведений
        """
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT evidence FROM product_directory WHERE product_id=?", (product_id,))
            ).fetchone()
        return json.loads(row["evidence"]) if row else None

    async def source_skus(self, product_id: str) -> list[dict]:
        """Возвращает связанные карточки в исходном формате MCP ВкусВилла.

        :param product_id: Общий продукт, объединяющий товарные варианты
        :return: Исходные JSON-карточки без адаптации цен и единиц
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT c.payload FROM catalog_skus c JOIN product_skus p ON p.sku_id=c.sku_id "
                    "WHERE p.product_id=? ORDER BY c.sku_id",
                    (product_id,),
                )
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    async def all(self) -> list[Recipe]:
        """Возвращает весь каталог без токенового усечения.

        :return: Полные рецепты
        """
        async with self.connection() as db:
            rows = await (await db.execute("SELECT payload FROM recipes ORDER BY recipe_id")).fetchall()
        return [Recipe.model_validate_json(r["payload"]) for r in rows]

    async def recipes_by_ids(self, recipe_ids: list[str]) -> dict[str, Recipe]:
        """Читает только выбранные рецепты для пересчёта версии.

        :param recipe_ids: Ссылки из меню
        :return: Существующие рецепты по ID
        """
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT payload FROM recipes WHERE recipe_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(recipe_ids),),
                )
            ).fetchall()
        recipes = [Recipe.model_validate_json(row["payload"]) for row in rows]
        return {r.recipe_id: r for r in recipes}

    async def edit_products(self, request: str, current_ids: list[str]) -> dict[str, NormalizedProduct]:
        """Собирает текущие продукты, явные ID и до десяти кандидатов FTS.

        :param request: Текст правки
        :param current_ids: Продукты исходной корзины
        :return: Ограниченный справочник для редактора
        """
        words = re.findall(r"[\w-]+", request.lower())
        # Префиксы длинных слов позволяют найти простые русские словоформы.
        terms = [w[:-2] if len(w) > 5 and w.isalpha() else w for w in words]
        expression = " OR ".join('"' + term.replace('"', '""') + '"*' for term in terms)
        async with self.connection() as db:
            matches = []
            if expression:
                matches = await (
                    await db.execute(
                        "SELECT product_id FROM product_fts WHERE product_fts MATCH ? ORDER BY rank LIMIT 10",
                        (expression,),
                    )
                ).fetchall()
            ids = current_ids + re.findall(r"[\w-]+", request) + [row["product_id"] for row in matches]
            rows = await (
                await db.execute(
                    "SELECT payload FROM products WHERE payload IS NOT NULL "
                    "AND product_id IN (SELECT value FROM json_each(?)) ORDER BY product_id",
                    (json.dumps(ids),),
                )
            ).fetchall()
        products = [NormalizedProduct.model_validate_json(row["payload"]) for row in rows]
        return {p.product_id: p for p in products}

    async def get(self, recipe_ids: list[str]) -> Result:
        """Пакетно получает существующие рецепты с раскрытыми названиями продуктов.

        :param recipe_ids: Идентификаторы из каталога
        :return: Полные записи; неизвестные ID пропускаются
        """
        index = await self.recipes_by_ids(recipe_ids)
        products = await self.products()
        recipes = []
        for recipe_id in dict.fromkeys(recipe_ids):
            if recipe_id not in index:
                continue
            recipe = index[recipe_id]
            data = recipe.model_dump(mode="json")
            data["ingredients"] = [
                i.model_dump(mode="json")
                | {"name": products[i.product_id].name, "unit": products[i.product_id].unit}
                for i in recipe.ingredients
            ]
            recipes.append(data)
        return Result(data={"recipes": recipes})

    async def search(self, query: str) -> Result:
        """Возвращает не более десяти карточек по одному запросу.

        :param query: Конкретное блюдо или сочетание ингредиентов
        :return: Карточки без технических сведений поискового движка
        """
        words = re.findall(r"[\w]+", query.lower(), flags=re.UNICODE)
        if not words:
            return Result(data={"recipes": []})
        expression = " OR ".join(f'"{w}"*' for w in words)
        async with self.connection() as db:
            rows = await (
                await db.execute(
                    "SELECT r.payload FROM recipe_fts f JOIN recipes r ON r.recipe_id=f.recipe_id "
                    "WHERE recipe_fts MATCH ? ORDER BY rank LIMIT 10",
                    (expression,),
                )
            ).fetchall()
        products = await self.products()
        return Result(
            data={"recipes": [Recipe.model_validate(json.loads(r["payload"])).card(products) for r in rows]}
        )
