import asyncio
import gzip
import hashlib
import json
import math
from collections import Counter

from pydantic import ValidationError

from app.catalog.store import Catalog
from app.config import get_config
from app.models import Recipe


async def check_catalog(catalog: Catalog, documents: bool = True) -> dict:
    """Проверяет три финальных файла без импорта, записи и сетевых вызовов.

    :param catalog: Подключение к финальному выпуску
    :param documents: Проверять содержимое всех сохранённых исходных документов
    :return: Счётчики, отпечатки и результаты проверки целостности
    """
    await catalog.validate()
    products = await catalog.products()
    recipes = await catalog.all()
    ingredient_rows = 0
    for recipe in recipes:
        for ingredient in recipe.ingredients:
            if ingredient.product_id not in products:
                raise ValueError(f"Неизвестный продукт рецепта {recipe.recipe_id}")
            if ingredient.amount <= 0:
                raise ValueError(f"Неположительное количество в {recipe.recipe_id}")
            product = products[ingredient.product_id]
            if product.nutrition is None or product.cost is None:
                raise ValueError(f"Неполные показатели продукта в {recipe.recipe_id}")
            ingredient_rows += 1
        for field, keys in [
            ("nutrition", ["kcal", "protein_g", "fat_g", "carbs_g"]),
            ("cost", ["mean_rub", "min_rub", "max_rub"]),
        ]:
            for key in keys:
                target = sum(
                    i.amount
                    * getattr(getattr(products[i.product_id], field), key)
                    / products[i.product_id].base_amount
                    for i in recipe.ingredients
                )
                tolerance = 0.5 if field == "nutrition" else 0.005
                if not math.isclose(
                    getattr(getattr(recipe, field), key), target, rel_tol=0, abs_tol=tolerance + 1e-8
                ):
                    raise ValueError(f"Ошибка расчёта {field}.{key}: {recipe.recipe_id}")
    async with catalog.connection() as db:
        for schema in ["main", "product_data", "source_data"]:
            if (await (await db.execute(f"PRAGMA {schema}.integrity_check")).fetchone())[0] != "ok":
                raise ValueError(f"Повреждена БД {schema}")
            if await (await db.execute(f"PRAGMA {schema}.foreign_key_check")).fetchall():
                raise ValueError(f"Повреждены внутренние ссылки БД {schema}")
        active = {
            r[0] for r in await (await db.execute("SELECT product_id FROM product_directory")).fetchall()
        }
        for pid in active:
            if pid not in products or products[pid].nutrition is None or products[pid].cost is None:
                raise ValueError("Активная категория неполная")
        observations_checked = 0
        for row in await (await db.execute("SELECT product_id,evidence FROM product_directory")).fetchall():
            evidence = json.loads(row["evidence"])
            product = products[row["product_id"]]
            for field in ["nutrition_observations", "price_observations"]:
                for observation in evidence.get(field, []):
                    if observation.get("unit") != product.unit:
                        raise ValueError(
                            f"Единица наблюдения {field} не соответствует продукту {product.product_id}"
                        )
                    if observation.get("base_amount") != product.base_amount:
                        raise ValueError(
                            f"База наблюдения {field} не соответствует продукту {product.product_id}"
                        )
                    observations_checked += 1
            supplement = evidence.get("nutrition_supplement")
            if supplement and (
                supplement.get("nutrition_unit") != product.unit
                or supplement.get("nutrition_base_amount") != product.base_amount
            ):
                raise ValueError(f"База справочного КБЖУ не соответствует продукту {product.product_id}")
        for query, expected in [
            ("SELECT recipe_id FROM recipe_fts", {r.recipe_id for r in recipes}),
            ("SELECT product_id FROM product_fts", active),
        ]:
            rows = [r[0] for r in await (await db.execute(query)).fetchall()]
            if set(rows) != expected or len(rows) != len(expected):
                raise ValueError("Поисковый индекс не соответствует каталогу")
        for query in [
            "SELECT 1 FROM product_skus p LEFT JOIN products n USING(product_id) LEFT JOIN catalog_skus s USING(sku_id) WHERE n.product_id IS NULL OR s.sku_id IS NULL LIMIT 1",
            "SELECT 1 FROM product_aliases a LEFT JOIN product_directory p USING(product_id) WHERE p.product_id IS NULL LIMIT 1",
        ]:
            if await (await db.execute(query)).fetchone():
                raise ValueError("Некорректная ссылка между финальными базами")
        native = hashlib.sha256()
        for row in await (
            await db.execute("SELECT sku_id,payload FROM catalog_skus ORDER BY sku_id")
        ).fetchall():
            value = json.loads(row["payload"])
            if str(value["id"]) != row["sku_id"]:
                raise ValueError("Native MCP ID не соответствует ключу")
            native.update(
                (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )
        counts = {}
        for table in [
            "product_skus",
            "product_aliases",
            "catalog_skus",
            "recipe_sources",
            "recipe_preparation",
            "documents",
        ]:
            counts[table] = (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0]
        origins = {r[0] for r in await (await db.execute("SELECT recipe_id FROM recipe_origins")).fetchall()}
        if origins != {r.recipe_id for r in recipes}:
            raise ValueError("Не для всех опубликованных рецептов сохранён источник")
        statuses = dict(
            await (
                await db.execute("SELECT status,count(*) FROM recipe_preparation GROUP BY status")
            ).fetchall()
        )
        if counts["recipe_sources"] != counts["recipe_preparation"]:
            raise ValueError("Не все исходные рецепты учтены")
        published = {r.recipe_id: r.model_dump(mode="json") for r in recipes}
        audited = set()
        for row in await (
            await db.execute("SELECT status,published_recipe_id,payload FROM recipe_preparation")
        ).fetchall():
            preparation = json.loads(row["payload"])
            if row["status"] == "ready":
                payload = preparation["payload"]
                try:
                    # Исходная проверка хранится точно; публикация округляет только КБЖУ и цену.
                    expected = Recipe.model_validate(payload).model_dump(mode="json")
                except ValidationError as exc:
                    raise ValueError("Опубликованный рецепт отличается от проверенного состава") from exc
                if (
                    row["published_recipe_id"] != payload["recipe_id"]
                    or published.get(payload["recipe_id"]) != expected
                ):
                    raise ValueError("Опубликованный рецепт отличается от проверенного состава")
                audited.add(payload["recipe_id"])
            elif row["status"] == "needs_revision" and row["published_recipe_id"] is not None:
                raise ValueError("Опубликован рецепт с незавершённой проверкой")
        if audited != {rid for rid in published if rid.startswith("recipe_adapt_vv_")}:
            raise ValueError("Не для всех адаптаций сохранена проверка")
        checked_documents = 0
        if documents:
            async with db.execute("SELECT content,encoding,sha256 FROM documents") as cursor:
                async for row in cursor:
                    content = gzip.decompress(row["content"]) if row["encoding"] == "gzip" else row["content"]
                    if hashlib.sha256(content).hexdigest() != row["sha256"]:
                        raise ValueError("Повреждён исходный документ")
                    checked_documents += 1
    return {
        "recipes": len(recipes),
        "products": len(products),
        "active_products": len(active),
        "product_observations_checked": observations_checked,
        "ingredient_rows_checked": ingredient_rows,
        "source_counts": counts,
        "source_statuses": statuses,
        "source_documents_checked": checked_documents,
        "new_meal_roles": dict(
            Counter(
                role for r in recipes if r.recipe_id.startswith("recipe_adapt_vv_") for role in r.meal_roles
            )
        ),
        "native_mcp_semantic_sha256": native.hexdigest(),
        "integrity": "ok",
        "database_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [catalog.recipes_path, catalog.products_path, catalog.sources_path]
        },
    }


async def main() -> None:
    """Проверяет настроенные финальные БД и печатает компактный отчёт."""
    config = get_config()
    catalog = Catalog(
        config.recipes_database_path, config.products_database_path, config.sources_database_path
    )
    print(json.dumps(await check_catalog(catalog), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
