import pytest
from pydantic_ai.usage import RunUsage

from app.assistant.tools import ProductQuery, SkuId
from app.integrations.vkusvill import parse_product
from app.models import (
    CartLimits,
    CartProduct,
    Choice,
    PersonContext,
    PlanSource,
    Product,
    RecipesSource,
    Result,
    Selection,
)
from app.shopping.calculations import aggregate, assemble
from app.shopping.service import Shopper


class Products:
    def __init__(self) -> None:
        self.queries = []
        self.links = []

    async def search(self, query: str, limit: int) -> list[Product]:
        assert limit == 10
        self.queries.append(query)
        return [
            Product(
                sku_id="sku1", title="Овсянка 500 г", price_rub=100.0, package_amount=500.0, package_unit="g"
            )
        ]

    async def cart_link(self, quantities: dict[str, int]) -> str:
        self.links.append(quantities)
        return "https://example.org/cart"


class Selector:
    async def generate(self, prompt, schema, payload, usage, effort="low"):
        assert prompt == "product_selection"
        assert "needs" not in payload
        return Selection(
            choices=[
                Choice(product_id=p["product_id"], sku_id="sku1", reason="") for p in payload["products"]
            ]
        )


async def test_selection_excludes_unquotable_skus_but_keeps_composition_and_saved_provenance(store, catalog):
    class Search(Products):
        async def search(self, query, limit):
            return [
                Product(sku_id="unknown", title="Без фасовки", price_rub=10),
                Product(
                    sku_id="sku1",
                    title="Овсянка 500 г",
                    price_rub=100,
                    package_amount=500,
                    package_unit="g",
                    composition="Овёс",
                    source_url="https://example.org/sku1",
                    package_basis="Маркировка",
                ),
            ]

    class Check(Selector):
        async def generate(self, prompt, schema, payload, usage, effort="low"):
            options = payload["candidates"]["oats"]
            assert len(options) == 1
            assert options[0]["composition"] == "Овёс"
            assert options[0]["purchase"]["quantity"] == 1
            assert "source_url" not in options[0]
            return await super().generate(prompt, schema, payload, usage, effort)

    result = await Shopper(store, catalog, Search(), Check()).run(
        "u",
        "quoted-only",
        RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}]}),
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == "ok"
    assert result.data["items"][0]["product"]["source_url"] == "https://example.org/sku1"
    assert result.data["products"][0]["requested_sku_id"] == "sku1"


def test_aggregation_by_product_id_preserves_purpose() -> None:
    result = aggregate(
        [
            CartProduct(product_id="milk", amount=1000.0, recipe_ids=["r1"]),
            CartProduct(product_id="milk", amount=250.0, recipe_ids=["r2"]),
            CartProduct(product_id="lactose_free_milk", amount=200.0, recipe_ids=["r3"]),
        ]
    )
    assert len(result) == 2
    assert result[0].amount == 1250
    assert result[0].recipe_ids == ["r1", "r2"]
    assert "need_id" not in result[0].model_dump()
    uncertain = aggregate([result[0], CartProduct(product_id="milk", amount=None, recipe_ids=[])])
    assert uncertain[0].amount is None


async def test_cart_aggregates_before_search_uses_cache_and_replays(store, catalog) -> None:
    products = Products()
    shopper = Shopper(store, catalog, products, Selector())
    source = RecipesSource.model_validate(
        {"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}, {"recipe_id": "r1", "servings": 2}]}
    )
    limits = CartLimits(budget_rub=200.0)
    result = await shopper.run("u", "cart1", source, limits, PersonContext(), RunUsage())
    assert result.status == "ok"
    from app.assistant.tools import CartResult

    assert CartResult.from_result(result).data.items[0].packages == 1
    assert result.data["products"][0]["amount"] == 150
    assert result.data["items"][0]["product_id"] == "oats"
    assert not {"needs", "notices"} & result.data.keys()
    assert len(products.queries) == 1
    assert products.links == [{"sku1": 1}]
    again = await shopper.run("u", "cart1", source, limits, PersonContext(), RunUsage())
    assert result == again and len(products.links) == 1
    await shopper.run("u", "cart2", source, limits, PersonContext(), RunUsage())
    assert len(products.queries) == 1


async def test_partial_cart_exposes_missing_purpose(store, catalog) -> None:
    class Empty(Products):
        async def search(self, query: str, limit: int) -> list[Product]:
            return []

    source = RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}]})
    result = await Shopper(store, catalog, Empty(), Selector()).run(
        "u", "partial", source, CartLimits(budget_rub=None), PersonContext(), RunUsage()
    )
    assert result.status == "partial"
    assert result.data["unresolved"][0]["recipe_ids"] == ["r1"]
    assert result.data["unresolved"][0]["product_id"] == "oats"
    assert not result.data["total_is_complete"]
    assert result.data["cart_url"] is None


def test_product_units_not_guessed() -> None:
    parsed = parse_product(
        {"id": 1, "name": "Молоко 1 л", "unit": "шт", "price": {"current": 100, "currency": "RUB"}}
    )
    assert parsed.package_amount == 1000 and parsed.package_unit == "ml"
    assert (
        parse_product(
            {"id": 1, "name": "Молоко", "unit": "шт", "price": {"current": 100, "currency": "RUB"}}
        ).package_amount
        is None
    )


def test_product_html_entities_are_decoded() -> None:
    row = parse_product(
        {"id": 1, "name": "Молоко 900&nbsp;мл", "unit": "шт", "price": {"current": 100, "currency": "RUB"}}
    )
    assert row.package_amount == 900 and row.package_unit == "ml"


async def test_plan_source_uses_saved_portions_without_text_extraction(store, catalog) -> None:
    plan = await store.save_object(
        "u",
        "plan",
        {"meals": [{"day": 1, "meal": "breakfast", "recipe_id": "r1", "servings": 2.0}]},
        Result(),
        "source_plan",
    )
    result = await Shopper(store, catalog, Products(), Selector()).run(
        "u",
        "plan_cart",
        PlanSource(type="plan", plan_id=plan.data["plan_id"]),
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.data["products"][0]["amount"] == 100
    assert result.data["source"]["plan_id"] == plan.data["plan_id"]
    assert await store.get_object("u", "plan", plan.data["plan_id"]) == plan


@pytest.mark.parametrize("price,status", [(94.0, "ok"), (105.0, "ok"), (105.01, "needs_revision")])
async def test_cart_budget_five_percent_tolerance(store, catalog, price, status):
    class Priced(Products):
        async def search(self, query, limit):
            return [p.model_copy(update={"price_rub": price}) for p in await super().search(query, limit)]

    result = await Shopper(store, catalog, Priced(), Selector()).run(
        "u",
        "budget",
        RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}]}),
        CartLimits(budget_rub=100.0),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == status
    assert result.data["limits"]["budget_rub"] == 100
    if result.issues:
        assert result.issues[0].expected == 105


@pytest.mark.parametrize("title", ["Набор 2 шт по 100 г", "Молоко 2×900 мл", "Десерт 100 г и соус 20 г"])
def test_ambiguous_multipack_needs_structured_weight(title: str) -> None:
    row = parse_product({"id": 1, "name": title, "unit": "шт", "price": {"current": 100, "currency": "RUB"}})
    assert row.package_amount is None


async def test_batch_selection_uses_ten_candidates_per_product(
    store, catalog, recipe, product, catalog_writer
):
    from app.models import Ingredient

    second = product.model_copy(update={"product_id": "rice", "name": "Рис"})
    recipe = recipe.model_copy(
        update={"ingredients": [*recipe.ingredients, Ingredient(product_id="rice", amount=70.0)]}
    )
    catalog = catalog_writer([product, second], [recipe])

    class Many(Products):
        async def search(self, query, limit):
            base = (await super().search(query, limit))[0]
            return [base.model_copy(update={"sku_id": f"{query}_{i}"}) for i in range(12)]

    class Batch:
        calls = 0

        async def generate(self, prompt, schema, payload, usage, effort="low"):
            self.calls += 1
            assert prompt == "product_selection"
            assert set(payload["candidates"]) == {"oats", "rice"}
            assert all(len(v) == 10 for v in payload["candidates"].values())
            return Selection(
                choices=[
                    Choice(product_id=k, sku_id=v[0]["sku_id"], reason="")
                    for k, v in payload["candidates"].items()
                ]
            )

    batch = Batch()
    result = await Shopper(store, catalog, Many(), batch).run(
        "u",
        "batch",
        RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}]}),
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == "ok"
    assert batch.calls == 1
    assert len(result.data["items"]) == 2


async def test_public_product_searches_have_fixed_top_ten(store, catalog):
    from types import SimpleNamespace

    from app.assistant.tools import get_product_analogs, search_products

    class Oversized:
        async def search(self, query, limit):
            assert limit == 10
            return [Product(sku_id=str(i), title="Товар", price_rub=100.0) for i in range(15)]

        async def analogs(self, sku_id, limit):
            return await self.search(sku_id, limit)

    ctx = SimpleNamespace(deps=SimpleNamespace(products=Oversized(), store=store, catalog=catalog))
    assert len((await search_products(ctx, ProductQuery(query="товар"))).data) == 10
    assert len((await get_product_analogs(ctx, SkuId(sku_id="sku"))).data) == 10


async def test_progress_counts_unique_searches_empty_and_error(store, catalog_writer, recipe, product):
    import asyncio

    from app.integrations.vkusvill import IntegrationError
    from app.models import Ingredient
    from app.progress import Progress

    products = [product.model_copy(update={"product_id": str(i), "name": str(i)}) for i in range(3)]
    recipe = recipe.model_copy(
        update={"ingredients": [Ingredient(product_id=p.product_id, amount=50) for p in products]}
    )
    catalog = catalog_writer(products, [recipe])
    events = []

    async def emit(event):
        events.append(event)

    class Search(Products):
        async def search(self, query, limit):
            await asyncio.sleep(0)
            if query == "1":
                return []
            if query == "2":
                raise IntegrationError("Unavailable")
            return await super().search(query, limit)

    source = RecipesSource.model_validate(
        {"type": "recipes", "items": [{"recipe_id": "r1", "servings": 2}, {"recipe_id": "r1", "servings": 1}]}
    )
    shopper = Shopper(store, catalog, Search(), Selector())
    result = await shopper.run(
        "u",
        "count-progress",
        source,
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
        progress=Progress(emit, "create_cart", "call"),
    )
    searches = [e for e in events if e["stage"] == "cart_search"]
    assert [e["completed"] for e in searches] == [0, 1, 2, 3]
    assert all(e["total"] == 3 for e in searches)
    assert searches[-1]["found"] == 1
    assert result.status == "partial" and len(result.data["unresolved"]) == 2
    assert [e["stage"] for e in events[-4:]] == ["cart_select", "cart_calculate", "cart_link", "cart_save"]
    assert all(e["found"] == 1 for e in events[-4:])
    count = len(events)
    await shopper.run(
        "u",
        "count-progress",
        source,
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
        progress=Progress(emit),
    )
    assert len(events) == count


async def test_direct_sku_progress_skips_search_and_selection(store, catalog):
    from app.models import SkusSource
    from app.progress import Progress

    events = []

    async def emit(event):
        events.append(event)

    source = SkusSource.model_validate({"type": "skus", "items": [{"sku_id": "sku1", "packages": 1}]})
    await Shopper(store, catalog, Products(), Selector()).run(
        "u",
        "sku-progress",
        source,
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
        progress=Progress(emit),
    )
    assert [e["stage"] for e in events] == ["cart_calculate", "cart_link", "cart_save"]


@pytest.mark.parametrize(
    "amount,quantity,cost", [(250, 0.25, 232), (281.25, 0.29, 269.12), (300, 0.3, 278.4), (1, 0.01, 9.28)]
)
def test_weighted_quote_rounds_up_to_hundredth(amount, quantity, cost):
    from app.shopping.calculations import purchase_quote

    sku = parse_product(
        {"id": 1066, "name": "Индейка", "unit": "кг", "price": {"current": 928, "currency": "RUB"}}
    )
    assert purchase_quote(amount, "g", sku) == {
        "quantity": quantity,
        "covered_amount": quantity * 1000,
        "cost_rub": cost,
    }
    assert purchase_quote(amount, "ml", sku) is None
    packaged = sku.model_copy(update={"sale_unit": None, "package_amount": 500})
    assert purchase_quote(501, "g", packaged)["quantity"] == 2


async def test_weighted_selection_quote_matches_saved_cart_and_legacy_cache(store, catalog):
    import hashlib
    import json
    import time

    sku = Product(
        sku_id="sku1",
        title="Овсянка на вес",
        price_rub=200,
        package_amount=1000,
        package_unit="g",
        package_basis="Единица продажи MCP: кг",
    )
    legacy = sku.model_dump(exclude={"sale_unit"})
    async with store.connection() as db:
        await db.execute(
            "INSERT INTO sku_cache VALUES(?,?,?)",
            (hashlib.sha256("mcp-v1:Овсянка".encode()).hexdigest(), json.dumps([legacy]), time.time()),
        )
        await db.commit()

    class Check(Selector):
        async def generate(self, prompt, schema, payload, usage, effort="low"):
            quote = payload["candidates"]["oats"][0]["purchase"]
            assert quote == {"quantity": 0.15, "covered_amount": 150, "cost_rub": 30}
            return await super().generate(prompt, schema, payload, usage, effort)

    gateway = Products()
    shopper = Shopper(store, catalog, gateway, Check())
    source = RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 3}]})
    result = await shopper.run(
        "u", "weighted", source, CartLimits(budget_rub=30), PersonContext(), RunUsage()
    )
    assert result.data["items"][0]["packages"] == 0.15
    assert result.data["items"][0]["product"]["price_source"] == "Кэш поиска MCP ВкусВилла"
    assert result.data["items"][0]["product"]["price_observed_at"] is not None
    assert result.data["subtotal_rub"] == 30
    assert gateway.links == [{"sku1": 0.15}]
    assert not gateway.queries
    assert result == await shopper.run(
        "u", "weighted", source, CartLimits(budget_rub=30), PersonContext(), RunUsage()
    )
    assert len(gateway.links) == 1


async def test_fractional_direct_quantity_requires_confirmed_sale_unit(store, catalog):
    from app.models import SkuAmount, SkusSource

    gateway = Products()
    shopper = Shopper(store, catalog, gateway, Selector())
    source = SkusSource(type="skus", items=[SkuAmount(sku_id="sku1", packages=0.25)])
    args = ("u", "fractional", source, CartLimits(budget_rub=None), PersonContext(), RunUsage())
    assert (await shopper.run(*args)).error.code == "INVALID_CART_QUANTITY"
    await store.cache_skus([Product(sku_id="sku1", title="Пачка", price_rub=100)])
    assert (await shopper.run(*args)).error.code == "INVALID_CART_QUANTITY"
    await store.cache_skus([Product(sku_id="sku1", title="На вес", price_rub=100, sale_unit="kg")])
    result = await shopper.run(*args)
    assert result.data["subtotal_rub"] == 25
    assert gateway.links == [{"sku1": 0.25}]


def test_explicit_conversion_quotes_edible_amount_and_rounds_packages(product):
    from app.models import UnitConversion
    from app.shopping.calculations import purchase_quote

    estimate = UnitConversion(factor=50, source_url="https://example.org/eggs", note="Оценка съедобной части")
    sku = Product(sku_id="7", title="Яйца 10 шт", price_rub=120, package_amount=10, package_unit="pcs")
    assert purchase_quote(501, "g", sku) is None
    assert purchase_quote(501, "g", sku, estimate) == {
        "quantity": 2,
        "covered_amount": 1000,
        "cost_rub": 240,
    }
    oil = sku.model_copy(update={"package_amount": 1000, "package_unit": "ml"})
    density = estimate.model_copy(update={"factor": 0.96})
    assert purchase_quote(960, "g", oil, density)["quantity"] == 1
    assert purchase_quote(960.1, "g", oil, density)["quantity"] == 2
    normalized = product.model_copy(update={"conversions": {"pcs": estimate}})
    payload, _, quantities = assemble(
        [CartProduct(product_id=product.product_id, amount=501, recipe_ids=["r1"])],
        {product.product_id: normalized},
        {product.product_id: sku},
        [],
        {},
        CartLimits(budget_rub=None),
    )
    assert quantities == {"7": 2}
    assert payload["items"][0]["quantity_basis"]["source_url"] == estimate.source_url


async def test_candidates_supplement_live_search_and_keep_live_price(store, catalog, product):
    import json
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(catalog.sources_path)) as db:
        db.execute("INSERT INTO metadata VALUES('snapshot_date','2026-10-17')")
        for sku in [7, 8]:
            db.execute(
                "INSERT INTO catalog_skus VALUES(?,?)",
                (
                    str(sku),
                    json.dumps(
                        {
                            "id": sku,
                            "name": "Овсянка 500 г",
                            "unit": "шт",
                            "price": {"current": 100, "currency": "RUB"},
                        }
                    ),
                ),
            )
        db.commit()
    with closing(sqlite3.connect(catalog.products_path)) as db:
        db.executemany("INSERT INTO product_skus VALUES(?,?)", [(product.product_id, str(s)) for s in [7, 8]])
        db.commit()

    class Live(Products):
        async def search(self, query, limit):
            self.queries.append(query)
            return [
                Product(
                    sku_id="7", title="Овсянка 500 г", price_rub=123, package_amount=500, package_unit="g"
                )
            ]

    service = Live()
    shopper = Shopper(store, catalog, service, None)
    for _ in range(2):
        result = await shopper.candidates(product)
        assert [(s.sku_id, s.price_rub) for s in result] == [("7", 123), ("8", 100)]
        assert result[1].price_observed_at == "2026-10-17"
    assert len(service.queries) == 1
