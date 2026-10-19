import asyncio
import hashlib
import threading

import numpy as np
import pytest
from pydantic_ai.usage import RunUsage

from app.assistant.tools import RecipeSearchResult
from app.config import Config
from app.models import PersonContext, PlanLimits
from app.planning import Planner
from app.retrieval.features import equipment_features
from app.retrieval.models import SearchConstraints
from app.retrieval.service import RecipeRetriever


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


async def test_negation_is_not_a_positive_search_term(retrieval):
    retriever, _ = retrieval
    result = await retriever.search("сырники без творога")
    ids = [r["recipe_id"] for r in result.data["recipes"]]
    assert ids[0] == "apple-cakes"
    assert "quark-cakes" not in ids
    assert "garnish" not in ids
    only_exclusion = await retriever.select("без творога")
    assert only_exclusion.recipe_ids and "quark-cakes" not in only_exclusion.recipe_ids
    assert not any(d.code == "RETRIEVAL_DEGRADED" for d in only_exclusion.diagnostics)
    assert RecipeSearchResult.from_result(result).data.recipes[0].title == "Сырники из яблок"


async def test_profile_current_memory_and_explicit_override(retrieval):
    retriever, _ = retrieval
    person = PersonContext(memory="Не ем творог", dialogue="Раньше обсуждалась аллергия на яйца")
    selected = await retriever.select("сырники", person)
    assert "quark-cakes" not in selected.recipe_ids
    forgotten = await retriever.select("сырники", PersonContext(dialogue=person.dialogue))
    assert "quark-cakes" in forgotten.recipe_ids
    override = await retriever.select("сырники", person, SearchConstraints(exclude_products=[]))
    assert "quark-cakes" in override.recipe_ids
    partial = await retriever.select("сырники", person, SearchConstraints(max_total_minutes=100))
    assert "quark-cakes" not in partial.recipe_ids
    preferred = await retriever.select("сырники", PersonContext(memory="Предпочитаю яблоки."))
    assert preferred.recipe_ids[0] == "apple-cakes"
    assert not retriever.conditions.preferences(PersonContext(memory="Не люблю яблоки."))
    _, conditions, _ = retriever.conditions.resolve(
        "сырники",
        PersonContext(questionnaire={"cooking_difficulty": "easy"}),
        SearchConstraints(max_difficulty=None),
    )
    assert conditions.max_difficulty is None


async def test_equipment_alternatives_and_unknown_are_conservative(retrieval):
    retriever, _ = retrieval
    selected = await retriever.select("омлет без духовки")
    assert "omelet" not in selected.recipe_ids
    assert "alternative" in selected.recipe_ids
    assert "uncertain" not in selected.recipe_ids
    both = await retriever.select(
        "омлет", constraints=SearchConstraints(forbidden_equipment=["духовка", "аэрогриль"])
    )
    assert "alternative" not in both.recipe_ids
    assert any(d.code == "INSUFFICIENT_EVIDENCE" for d in selected.diagnostics)


async def test_unresolved_allergen_and_unknown_product_do_not_claim_safety(retrieval):
    retriever, _ = retrieval
    for constraints in [
        SearchConstraints(allergens=["орехи"]),
        SearchConstraints(exclude_products=["несуществующий порошок"]),
    ]:
        result = await retriever.select("сырники", constraints=constraints)
        assert result.recipe_ids == []
        assert result.diagnostics
    result = await retriever.select("сырники без неопределенного ингредиента")
    assert result.recipe_ids == []
    gluten = await retriever.select("сырники без глютена")
    assert not gluten.recipe_ids and any(d.code == "INSUFFICIENT_EVIDENCE" for d in gluten.diagnostics)
    no_allergy = await retriever.select("сырники", PersonContext(memory="Аллергий нет."))
    assert no_allergy.recipe_ids
    mixed = await retriever.select(
        "сырники", PersonContext(memory="Непереносимости нет, но аллергия на орехи.")
    )
    assert not mixed.recipe_ids


async def test_coordinated_product_exclusions_are_all_applied(retrieval):
    retriever, _ = retrieval
    for query in ["сырники без творога и без яблок", "сырники без творога, яблок"]:
        result = await retriever.select(query)
        assert not result.recipe_ids


async def test_supported_time_and_roles_filter_before_top_k(retrieval):
    retriever, _ = retrieval
    result = await retriever.select("ужин", constraints=SearchConstraints(max_total_minutes=0.1))
    assert not result.recipe_ids
    result = await retriever.select("ужин")
    assert result.recipe_ids
    assert all("dinner" in retriever.recipes[rid].meal_roles for rid in result.recipe_ids)


async def test_timeout_keeps_cpu_slot_until_actual_completion(retrieval):
    retriever, encoder = retrieval
    encoder.wait = threading.Event()
    calls = encoder.calls
    first = await retriever.select("сырники")
    second = await retriever.select("сырники")
    assert encoder.calls == calls + 1
    assert first.recipe_ids and second.recipe_ids
    assert all(any(d.code == "RETRIEVAL_DEGRADED" for d in r.diagnostics) for r in [first, second])
    encoder.wait.set()
    await asyncio.shield(retriever.busy)
    await retriever.select("сырники")
    assert encoder.calls == calls + 2


async def test_cancelled_request_does_not_release_running_worker(retrieval):
    retriever, encoder = retrieval
    encoder.wait = threading.Event()
    task = asyncio.create_task(retriever.select("сырники"))
    for _ in range(100):
        if retriever.busy:
            break
        await asyncio.sleep(0.001)
    assert retriever.busy is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    before = encoder.calls
    await retriever.select("сырники")
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
    assert not (await retriever.select("омлет")).recipe_ids
    await retriever.prepare(load_model=False)
    assert old_id != retriever.index_id


async def test_no_unrelated_substitution_when_named_dish_has_no_matches(retrieval):
    retriever, _ = retrieval
    result = await retriever.select(
        "сырники", constraints=SearchConstraints(exclude_products=["творог", "яблоко"])
    )
    assert result.recipe_ids == []


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
            assert (await client.post("/api/recipes/search", json={"query": "сырники"})).json()["data"][
                "recipes"
            ]


async def test_plan_candidates_integrated_without_catalog_leaking(retrieval, store):
    from app.models import Meal, PlanDraft

    retriever, _ = retrieval

    class Generator:
        async def generate(self, prompt, schema, payload, usage, effort):
            self.payload = payload
            assert all("breakfast" in r["meal_roles"] for r in payload["catalog"])
            return PlanDraft(
                meals=[
                    Meal(day=1, meal="breakfast", recipe_id=payload["catalog"][0]["recipe_id"], servings=1.0)
                ],
                explanation="",
            )

    generator = Generator()
    planner = Planner(store, retriever.catalog, generator, retriever)
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
    assert result.data["meals"][0]["recipe_id"] == "apple-cakes"
    assert "catalog" not in result.data
    assert "retrieval_diagnostics" in generator.payload
    empty = await planner.run(
        "u",
        "allergy",
        "завтрак",
        1,
        PlanLimits(max_difficulty=None, budget_rub=None),
        PersonContext(),
        RunUsage(),
        constraints=SearchConstraints(allergens=["молоко"]),
    )
    assert empty.error.code == "NO_SUITABLE_RECIPES"


def test_grill_ready_ingredient_is_not_a_required_grill(recipe):
    r = recipe.model_copy(update={"steps": ["Курицу гриль нарежьте. Смешайте с овощами."]})
    assert equipment_features(r)["groups"] == []


async def test_web_native_search_uses_real_context_without_llm(retrieval, store):
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
            assert (await client.post("/api/recipes/search", json={"query": "сырники"})).status_code == 401
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
            result = await client.post("/api/recipes/search", json={"query": "сырники"})
            assert result.status_code == 200
            assert "quark-cakes" not in [r["recipe_id"] for r in result.json()["data"]["recipes"]]
            assert (await client.get("/api/health")).json()["retrieval"]["enabled"] is True
