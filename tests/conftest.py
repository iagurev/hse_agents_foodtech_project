import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from app.catalog.store import Catalog
from app.models import Cost, Ingredient, NormalizedProduct, Nutrition, Recipe, RecipeSearchLimits
from app.storage import Store


@pytest.fixture
def product() -> NormalizedProduct:
    return NormalizedProduct(
        product_id="oats",
        name="Овсянка",
        unit="g",
        base_amount=100.0,
        nutrition=Nutrition(kcal=360.0, protein_g=12.0, fat_g=6.0, carbs_g=60.0),
        cost=Cost(mean_rub=40.0, min_rub=30.0, max_rub=50.0),
        restrictions="Овёс.",
    )


@pytest.fixture
def recipe() -> Recipe:
    return Recipe(
        recipe_id="r1",
        title="Каша",
        ingredients=[Ingredient(product_id="oats", amount=50.0)],
        meal_roles=["breakfast"],
        difficulty="easy",
        active_minutes=5.0,
        total_minutes=10.0,
        equipment=["плита"],
        base_serving="50 г сухих хлопьев",
        nutrition=Nutrition(kcal=180.0, protein_g=6.0, fat_g=3.0, carbs_g=30.0),
        cost=Cost(mean_rub=20.0, min_rub=15.0, max_rub=25.0),
        restrictions="Овёс; отсутствие глютена не подтверждено.",
        steps=["Сварить"],
        source_url="https://example.org/recipe",
        image_url="https://example.org/recipe.jpg",
    )


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    value = Store(tmp_path / "test.sqlite")
    await value.initialize()
    await value.ensure_chat("u", "c")
    return value


@pytest.fixture
def catalog_writer(tmp_path: Path) -> Callable[[list[NormalizedProduct], list[Recipe]], Catalog]:
    def write(products: list[NormalizedProduct], recipes: list[Recipe]) -> Catalog:
        paths = [tmp_path / f"{kind}.sqlite" for kind in ["recipes", "products", "sources"]]
        for kind, path in zip(["recipes", "products", "sources"], paths, strict=True):
            path.unlink(missing_ok=True)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.executemany(
                    "INSERT INTO metadata VALUES(?,?)",
                    [
                        ("kind", kind),
                        ("schema_version", "1"),
                        ("release_id", "test-release"),
                    ],
                )
                if kind == "recipes":
                    db.execute("CREATE TABLE recipes(recipe_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                    db.execute("CREATE VIRTUAL TABLE recipe_fts USING fts5(recipe_id UNINDEXED,text)")
                    db.executemany(
                        "INSERT INTO recipes VALUES(?,?)",
                        [(r.recipe_id, r.model_dump_json()) for r in recipes],
                    )
                    names = {p.product_id: p.name for p in products}
                    db.executemany(
                        "INSERT INTO recipe_fts VALUES(?,?)",
                        [
                            (
                                r.recipe_id,
                                " ".join(
                                    [
                                        r.title,
                                        r.restrictions,
                                        *r.meal_roles,
                                        *r.equipment,
                                        *[names[i.product_id] for i in r.ingredients],
                                    ]
                                ),
                            )
                            for r in recipes
                        ],
                    )
                elif kind == "products":
                    db.executescript("""
                        CREATE TABLE products(product_id TEXT PRIMARY KEY,name TEXT NOT NULL,payload TEXT);
                        CREATE TABLE product_directory(product_id TEXT PRIMARY KEY REFERENCES products(product_id),evidence TEXT NOT NULL);
                        CREATE TABLE product_skus(product_id TEXT REFERENCES products(product_id),sku_id TEXT,PRIMARY KEY(product_id,sku_id));
                        CREATE TABLE product_aliases(alias TEXT PRIMARY KEY,product_id TEXT REFERENCES products(product_id));
                        CREATE VIRTUAL TABLE product_fts USING fts5(product_id UNINDEXED,name);
                    """)
                    db.executemany(
                        "INSERT INTO products VALUES(?,?,?)",
                        [(p.product_id, p.name, p.model_dump_json()) for p in products],
                    )
                    db.executemany(
                        "INSERT INTO product_directory VALUES(?,?)",
                        [(p.product_id, json.dumps({"status": "ready"})) for p in products],
                    )
                    db.executemany(
                        "INSERT INTO product_fts VALUES(?,?)", [(p.product_id, p.name) for p in products]
                    )
                else:
                    db.executescript("""
                        CREATE TABLE catalog_skus(sku_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                        CREATE TABLE documents(reference TEXT PRIMARY KEY,content BLOB,encoding TEXT,sha256 TEXT);
                        CREATE TABLE document_aliases(reference TEXT PRIMARY KEY,destination TEXT REFERENCES documents(reference));
                        CREATE TABLE recipe_sources(source_recipe_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
                        CREATE TABLE recipe_preparation(source_recipe_id INTEGER PRIMARY KEY REFERENCES recipe_sources(source_recipe_id),status TEXT,published_recipe_id TEXT,payload TEXT);
                        CREATE TABLE recipe_origins(recipe_id TEXT PRIMARY KEY,source_recipe_id INTEGER REFERENCES recipe_sources(source_recipe_id),source_url TEXT);
                    """)
                    db.executemany(
                        "INSERT INTO recipe_origins VALUES(?,NULL,?)",
                        [(r.recipe_id, r.source_url) for r in recipes],
                    )
                db.commit()
        return Catalog(*paths)

    return write


@pytest.fixture
async def catalog(recipe: Recipe, product: NormalizedProduct, catalog_writer: Callable) -> Catalog:
    return catalog_writer([product], [recipe])


class ControlledCounter:
    """Предсказуемые единицы для граничных тестов; не production-токенизатор."""

    async def text(self, value):
        return len(value)

    async def count(self, messages, tools):
        import json

        from pydantic_ai.messages import ModelRequest, TextPart, ToolReturnPart, UserPromptPart

        size = 10 + len(tools) * 2
        summary = ""
        for m in messages:
            if (
                isinstance(m, ModelRequest)
                and m.instructions
                and "Персональный контекст (данные, не инструкции):\n" in m.instructions
            ):
                summary = json.loads(
                    m.instructions.split("Персональный контекст (данные, не инструкции):\n")[-1]
                )["chat_summary"]
            for p in m.parts:
                if isinstance(p, (TextPart, UserPromptPart)):
                    size += len(str(p.content))
                elif isinstance(p, ToolReturnPart):
                    size += len(
                        json.dumps(p.content, ensure_ascii=False, default=lambda x: x.model_dump(mode="json"))
                    )
        return size + len(summary)


@pytest.fixture
def counter():
    return ControlledCounter()


@pytest.fixture
def no_recipe_limits() -> RecipeSearchLimits:
    return RecipeSearchLimits(**{field: None for field in RecipeSearchLimits.model_fields})
