import asyncio
import hashlib
import threading

import numpy as np
import pytest
from pydantic_ai.usage import RunUsage

from app.assistant.tools import RecipeSearchResult
from app.config import Config
from app.models import PersonContext, PlanLimits, RecipeSearchLimits
from app.planning import Planner
from app.retrieval.features import equipment_features
from app.retrieval.service import RecipeRetriever


def limits(**changes):
    return RecipeSearchLimits.model_validate(
        {field: None for field in RecipeSearchLimits.model_fields} | changes
    )


class Encoder:
    """Контролируемая семантическая близость без сети или весов."""

    identity = "controlled-semantic-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.wait: threading.Event | None = None

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        if self.wait is not None:
            self.wait.wait(3)
        values = []
        for text in texts:
            vector = np.array(
                [float("сырник" in text.lower()), float("омлет" in text.lower()), 0.1], dtype=np.float32
            )
            values.append(vector / np.linalg.norm(vector))
        return np.asarray(values)


@pytest.fixture
async def retrieval(catalog_writer, recipe, product, tmp_path):
    quark = product.model_copy(update={"product_id": "quark", "name": "Творог"})
    egg = product.model_copy(update={"product_id": "egg", "name": "Яйцо куриное"})
    apple = product.model_copy(update={"product_id": "apple", "name": "Яблоко"})
    from app.models import Ingredient

    definitions = [
        ("quark-cakes", "Сырники из творога", "quark", ["Пожарьте на сковороде."], "breakfast"),
        ("apple-cakes", "Сырники из яблок", "apple", ["Пожарьте на сковороде."], "breakfast"),
        ("omelet", "Омлет с яблоком", "egg", ["Запекайте в духовке."], "dinner"),
        (
            "alternative",
            "Омлет с яблоком в аэрогриле",
            "egg",
            ["Запекайте в духовке или аэрогриле."],
            "dinner",
        ),
        ("uncertain", "Яблочный десерт", "apple", ["Выпекайте при 200 °C."], "dessert"),
    ]
    definitions.append(("garnish", "Яблоки к сырникам", "apple", ["Нарежьте яблоки."], "dessert"))
    recipes = [
        recipe.model_copy(
            update={
                "recipe_id": rid,
                "title": title,
                "ingredients": [Ingredient(product_id=pid, amount=100.0)],
                "steps": steps,
                "meal_roles": [role],
                "equipment": [],
                "difficulty": "advanced" if rid == "alternative" else "easy",
                "cost": recipe.cost.model_copy(
                    update={"mean_rub": 21.0 if rid == "apple-cakes" else 21.01 if rid == "garnish" else 20.0}
                ),
            }
        )
        for rid, title, pid, steps, role in definitions
    ]
    catalog = catalog_writer([quark, egg, apple], recipes)
    config = Config(_env_file=None, retrieval_cache_path=tmp_path / "index", retrieval_timeout_seconds=0.05)
    encoder = Encoder()
    retriever = RecipeRetriever(catalog, config, encoder)
    await retriever.prepare(load_model=False)
    try:
        yield retriever, encoder
    finally:
        if encoder.wait:
            encoder.wait.set()
        retriever.close()


@pytest.mark.parametrize("engine", ["rag", "fts"])
async def test_explicit_filters_and_search_contract(retrieval, engine):
    retriever, _ = retrieval
    search = retriever.search if engine == "rag" else retriever.catalog.search
    result = await search("сырники", limits(exclude_products=["творога"]))
    ids = [r["recipe_id"] for r in result.data["recipes"]]
    assert "apple-cakes" in ids and "quark-cakes" not in ids
    assert set(result.data) == {"recipes"}
    assert RecipeSearchResult.from_result(result).data.recipes
    result = await search("сырники", limits(exclude_products=["quark", "apple"]))
    assert not result.data["recipes"]
    result = await search("сырники", limits(exclude_products=["неопределённый ингредиент"]))
    assert not result.data["recipes"]
    assert not (await search("сырники", limits(forbidden_equipment=["неизвестная техника"]))).data["recipes"]


async def test_query_is_not_parsed_or_rewritten(retrieval, monkeypatch):
    retriever, _ = retrieval
    seen = []

    async def rank(query, allowed, diagnostics, deadline):
        seen.append((query, allowed))
        return sorted(allowed)

    monkeypatch.setattr(retriever, "_rank", rank)
    queries = [
        "салат без помидоров с курицей",
        "омлет без духовки с грибами",
        "веганский ужин до двадцати минут",
        "сырники, аллергия на орехи",
    ]
    for query in queries:
        await retriever.select(query, limits())
    assert [query for query, _ in seen] == queries[1:]
    assert seen[0][1] == {"omelet", "alternative"}
    assert seen[1][1] == set(retriever.recipes)
    assert seen[2][1] == {"quark-cakes", "apple-cakes"}


async def test_equipment_alternatives_and_unknown_are_conservative(retrieval):
    retriever, _ = retrieval
    selected = await retriever.select("омлет", limits(forbidden_equipment=["духовка"]))
    assert "omelet" not in selected.recipe_ids
    assert "alternative" in selected.recipe_ids
    assert "uncertain" not in selected.recipe_ids
    both = await retriever.select("омлет", limits(forbidden_equipment=["духовка", "аэрогриль"]))
    assert "alternative" not in both.recipe_ids
    assert "INSUFFICIENT_EVIDENCE" in selected.diagnostics


@pytest.mark.parametrize("engine", ["rag", "fts"])
async def test_time_roles_difficulty_and_diet(retrieval, engine):
    retriever, _ = retrieval
    search = retriever.search if engine == "rag" else retriever.catalog.search
    assert not (await search("омлет", limits(max_total_minutes=1))).data["recipes"]
    result = await search("омлет", limits(meal_roles=["dinner"], max_difficulty="easy"))
    assert result.data["recipes"]
    assert all("dinner" in r["meal_roles"] and r["difficulty"] == "easy" for r in result.data["recipes"])
    assert not (await search("омлет", limits(meal_roles=["breakfast"]))).data["recipes"]
    result = await search("сырники", limits(diet="vegetarian"))
    assert "quark-cakes" in {r["recipe_id"] for r in result.data["recipes"]}
    result = await search("сырники", limits(diet="vegan"))
    assert "quark-cakes" not in {r["recipe_id"] for r in result.data["recipes"]}


@pytest.mark.parametrize("engine", ["rag", "fts"])
@pytest.mark.parametrize("budget,expected", [(20, {"quark-cakes", "apple-cakes"}), (0, set())])
async def test_budget_of_base_serving_with_five_percent_tolerance(retrieval, engine, budget, expected):
    retriever, _ = retrieval
    search = retriever.search if engine == "rag" else retriever.catalog.search
    result = await search("сырники", limits(budget_rub=budget))
    assert {r["recipe_id"] for r in result.data["recipes"]} == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_rub", -1),
        ("budget_rub", "100"),
        ("budget_rub", True),
        ("budget_rub", float("inf")),
        ("max_total_minutes", 0),
        ("max_total_minutes", float("nan")),
        ("max_difficulty", "expert"),
        ("diet", "some diet"),
        ("meal_roles", ["any"]),
    ],
)
def test_search_limits_validate_numbers_and_values(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        limits(**{field: value})


async def test_timeout_keeps_cpu_slot_until_actual_completion(retrieval):
    retriever, encoder = retrieval
    encoder.wait = threading.Event()
    calls = encoder.calls
    first = await retriever.select("сырники", limits())
    second = await retriever.select("сырники", limits())
    assert encoder.calls == calls + 1
    assert first.recipe_ids and second.recipe_ids
    assert all("RETRIEVAL_DEGRADED" in r.diagnostics for r in [first, second])
    encoder.wait.set()
    await asyncio.shield(retriever.busy)
    await retriever.select("сырники", limits())
    assert encoder.calls == calls + 2


async def test_cancelled_request_does_not_release_running_worker(retrieval):
    retriever, encoder = retrieval
    encoder.wait = threading.Event()
    task = asyncio.create_task(retriever.select("сырники", limits()))
    for _ in range(100):
        if retriever.busy:
            break
        await asyncio.sleep(0.001)
    assert retriever.busy is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    before = encoder.calls
    await retriever.select("сырники", limits())
    assert encoder.calls == before
    encoder.wait.set()
    await asyncio.shield(retriever.busy)


async def test_stale_or_corrupted_index_rebuilt_and_catalog_readonly(retrieval):
    retriever, encoder = retrieval
    paths = [retriever.catalog.recipes_path, retriever.catalog.products_path, retriever.catalog.sources_path]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    calls = encoder.calls
    await retriever.prepare(load_model=False)
    assert encoder.calls == calls
    (retriever.path / "vectors.npy").write_bytes(b"corrupt")
    await retriever.prepare(load_model=False)
    assert encoder.calls == calls + 1
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    import sqlite3

    with sqlite3.connect(paths[0]) as db:
        db.execute(
            "UPDATE recipes SET payload=json_set(payload,'$.title','Новое блюдо') WHERE recipe_id='omelet'"
        )
    old_id = retriever.index_id
    assert not (await retriever.select("омлет", limits())).recipe_ids
    await retriever.prepare(load_model=False)
    assert old_id != retriever.index_id


async def test_web_without_api_key_still_serves_local_search(retrieval, store, monkeypatch):
    import httpx

    from app.web.server import create_app
    from app.web.storage import WebStore

    retriever, _ = retrieval

    async def prepare(catalog, config):
        return retriever

    monkeypatch.setattr("app.web.server.create_retriever", prepare)
    config = retriever.config.model_copy(update={"openrouter_api_key": None})
    app = create_app(config, store=WebStore(store.path), catalog=retriever.catalog)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = (await client.get("/api/health")).json()
            assert health["chat_enabled"] is False and health["retrieval"]["enabled"] is True
            assert (
                await client.post(
                    "/api/auth/register",
                    json={
                        "login": "offline",
                        "password": "p",
                        "profile": {
                            "goal": "ideas",
                            "sex": "male",
                            "age": 25,
                            "height_cm": 170.0,
                            "weight_kg": 65.0,
                        },
                    },
                )
            ).status_code == 200
            chat = (await client.post("/api/chats")).json()
            response = await client.post(
                f"/api/chats/{chat['chat_id']}/turns", json={"turn_id": "no-key", "text": "Привет"}
            )
            assert response.status_code == 503 and "OPENROUTER_API_KEY" in response.json()["detail"]
            assert (
                await client.post(
                    "/api/recipes/search", json={"query": "сырники", "limits": limits().model_dump()}
                )
            ).json()["data"]["recipes"]


async def test_planner_receives_full_catalog_with_short_ids_and_no_retrieval(retrieval, store):
    from app.models import Meal, PlanDraft

    retriever, _ = retrieval

    class Generator:
        async def generate(self, prompt, schema, payload, usage, effort):
            self.payload = payload
            assert len(payload["catalog"]) == len(retriever.recipes)
            assert {r["recipe_id"] for r in payload["catalog"]} == {
                f"r{i}" for i in range(1, len(retriever.recipes) + 1)
            }
            return PlanDraft(
                meals=[
                    Meal(day=1, meal="breakfast", recipe_id=payload["catalog"][0]["recipe_id"], servings=1.0)
                ],
                explanation="",
            )

    generator = Generator()
    planner = Planner(store, retriever.catalog, generator)
    result = await planner.run(
        "u",
        "rag-plan",
        "завтрак без творога",
        1,
        PlanLimits(max_difficulty=None, budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == "ok"
    assert result.data["meals"][0]["recipe_id"] in retriever.recipes
    assert "catalog" not in result.data
    assert "retrieval_diagnostics" not in generator.payload


def test_grill_ready_ingredient_is_not_a_required_grill(recipe):
    r = recipe.model_copy(update={"steps": ["Курицу гриль нарежьте. Смешайте с овощами."]})
    assert equipment_features(r)["groups"] == []


async def test_web_search_does_not_inherit_profile_and_needs_no_llm(retrieval, store):
    from types import SimpleNamespace

    import httpx

    from app.shopping import Shopper
    from app.web.server import create_app
    from app.web.storage import WebStore

    retriever, _ = retrieval
    webstore = WebStore(store.path)
    harness = SimpleNamespace(retriever=retriever, shopper=Shopper(webstore, retriever.catalog, None, None))
    app = create_app(
        retriever.config, store=webstore, catalog=retriever.catalog, session=harness, guard=object()
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    "/api/recipes/search", json={"query": "сырники", "limits": limits().model_dump()}
                )
            ).status_code == 401
            response = await client.post(
                "/api/auth/register",
                json={
                    "login": "r",
                    "password": "p",
                    "profile": {
                        "goal": "ideas",
                        "sex": "male",
                        "age": 25,
                        "height_cm": 170.0,
                        "weight_kg": 65.0,
                        "restrictions": "Без творога",
                    },
                },
            )
            assert response.status_code == 200
            for invalid in [
                {"query": "сырники"},
                {"query": "сырники", "limits": {}},
                {"query": "сырники", "limits": None},
            ]:
                assert (await client.post("/api/recipes/search", json=invalid)).status_code == 422
            filtered = await client.post(
                "/api/recipes/search",
                json={"query": "сырники", "limits": limits(exclude_products=["quark"]).model_dump()},
            )
            assert "quark-cakes" not in [r["recipe_id"] for r in filtered.json()["data"]["recipes"]]
            result = await client.post(
                "/api/recipes/search", json={"query": "сырники", "limits": limits().model_dump()}
            )
            assert result.status_code == 200
            assert "quark-cakes" in [r["recipe_id"] for r in result.json()["data"]["recipes"]]
            assert set(result.json()["data"]) == {"recipes"}
            assert (await client.get("/api/health")).json()["retrieval"]["enabled"] is True


async def test_filters_run_before_top_ten_in_both_engines(catalog_writer, recipe, product, tmp_path):
    recipes = [
        recipe.model_copy(
            update={
                "recipe_id": f"r{i:02}",
                "cost": recipe.cost.model_copy(update={"mean_rub": 200 if i < 12 else 20}),
            }
        )
        for i in range(15)
    ]
    catalog = catalog_writer([product], recipes)
    retriever = RecipeRetriever(catalog, Config(_env_file=None, retrieval_cache_path=tmp_path / "index"))
    await retriever.prepare(load_model=False)
    try:
        for search in [retriever.search, catalog.search]:
            result = await search("каша", limits(budget_rub=20))
            assert {r["recipe_id"] for r in result.data["recipes"]} == {"r12", "r13", "r14"}
            assert len((await search("каша", limits())).data["recipes"]) == 10
    finally:
        retriever.close()


async def test_named_dish_does_not_become_a_garnish(retrieval):
    retriever, _ = retrieval
    result = await retriever.select("сырники", limits(exclude_products=["quark", "apple"]))
    assert not result.recipe_ids
    result = await retriever.select("сырники", limits())
    assert set(result.recipe_ids) == {"quark-cakes", "apple-cakes"}
