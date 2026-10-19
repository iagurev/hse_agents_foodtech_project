import pytest
from pydantic_ai.usage import RunUsage

from app.assistant.tools import ProductQuery, SkuId
from app.integrations import parse_product
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
from app.shopping import Shopper, aggregate


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


async def test_public_product_searches_have_fixed_top_ten(store):
    from types import SimpleNamespace

    from app.assistant.tools import get_product_analogs, search_products

    class Oversized:
        async def search(self, query, limit):
            assert limit == 10
            return [Product(sku_id=str(i), title="Товар", price_rub=100.0) for i in range(15)]

        async def analogs(self, sku_id, limit):
            return await self.search(sku_id, limit)

    ctx = SimpleNamespace(deps=SimpleNamespace(products=Oversized(), store=store))
    assert len((await search_products(ctx, ProductQuery(query="товар"))).data) == 10
    assert len((await get_product_analogs(ctx, SkuId(sku_id="sku"))).data) == 10
