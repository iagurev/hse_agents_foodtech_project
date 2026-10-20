import copy
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import RunUsage

from app.assistant.tools import ProductQuery, SkuId, build_toolset, get_product_analogs, search_products
from app.integrations import IntegrationError
from app.models import (
    CartLimits,
    CartProduct,
    CartRevision,
    Meal,
    PlanLimits,
    PlanRevision,
    Product,
    Result,
    UnresolvedProduct,
)
from app.planning import Planner
from app.shopping.calculations import assemble
from app.shopping.service import Shopper


class Editor:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def generate(self, prompt, schema, payload, usage, effort="low"):
        assert prompt in {"edit_plan", "edit_cart"}
        assert effort == "low"
        assert not {"catalog", "context", "memory", "dialogue"} & payload.keys()
        self.calls.append(copy.deepcopy(payload))
        usage.requests += 1
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return schema.model_validate(result.model_dump())


class Products:
    def __init__(self, fail_link=False):
        self.links = []
        self.fail_link = fail_link

    async def search(self, *args):
        raise AssertionError("Редактор не должен повторять поиск SKU")

    async def cart_link(self, quantities):
        self.links.append(quantities)
        if self.fail_link:
            raise IntegrationError("HTTP 502")
        return "https://example.org/new-cart"


def plan(servings=1.0, budget=100.0, recipe_id="r1"):
    return PlanRevision(
        days=2,
        limits=PlanLimits(max_difficulty="easy", budget_rub=budget),
        meals=[
            Meal(day=1, meal="breakfast", recipe_id=recipe_id, servings=servings),
            Meal(day=2, meal="dinner", recipe_id="r1", servings=1.0),
        ],
        explanation="",
    )


async def save_plan(store):
    return await store.save_object("u", "plan", plan().model_dump(mode="json"), Result(), "initial-plan")


def sku(sku_id="old", price=100.0, amount=500.0):
    return Product(
        sku_id=sku_id, title="Овсянка 500 г", price_rub=price, package_amount=amount, package_unit="g"
    )


async def save_cart(store):
    return await store.save_object(
        "u",
        "cart",
        {
            "source": {"type": "text", "text": "Овсянка"},
            "limits": {"budget_rub": 200.0},
            "products": [CartProduct(product_id="oats", amount=100.0, recipe_ids=["r1"]).model_dump()],
            "items": [{"product_id": "oats", "product": sku().model_dump(), "packages": 1}],
            "unresolved": [],
            "subtotal_rub": 100.0,
            "total_is_complete": True,
            "cart_url": "https://example.org/old-cart",
            "link_error": None,
        },
        Result(),
        "initial-cart",
    )


def cart(amount=100.0, sku_id="old", budget=200.0):
    return CartRevision(
        products=[CartProduct(product_id="oats", amount=amount, recipe_ids=["r1"], requested_sku_id=sku_id)],
        unresolved=[],
        limits=CartLimits(budget_rub=budget),
    )


def test_edit_schemas_have_only_id_and_request():
    tools = build_toolset().tools
    for name, key in [("edit_plan", "plan_id"), ("edit_cart", "cart_id")]:
        schema = tools[name].function_schema.json_schema
        assert set(schema["properties"]) == {key, "request"}
        assert set(schema["required"]) == {key, "request"}
    assert "limits" in tools["generate_plan"].function_schema.json_schema["required"]
    assert "limits" in tools["create_cart"].function_schema.json_schema["required"]
    assert len(tools) == 13


async def test_plan_full_version_no_catalog_and_replay(store, catalog, monkeypatch):
    initial = await save_plan(store)

    async def forbidden():
        raise AssertionError("Не читать полный каталог для редактора")

    monkeypatch.setattr(catalog, "all", forbidden)
    updated = plan(servings=1.5)
    editor = Editor([updated])
    planner = Planner(store, catalog, editor)
    result = await planner.edit("u", "edit", initial.data["plan_id"], "Первый завтрак 1,5 порции", RunUsage())
    assert result.status == "ok"
    assert result.data["meals"] == updated.model_dump()["meals"]
    assert result.data["totals"]["mean_rub"] == 50
    assert editor.calls[0]["current_plan"] == initial.data
    assert await store.get_object("u", "plan", initial.data["plan_id"]) == initial
    assert result.data["parent_id"] == initial.data["plan_id"]
    assert await planner.edit("u", "edit", initial.data["plan_id"], "Повтор", RunUsage()) == result
    assert len(editor.calls) == 1


@pytest.mark.parametrize("mutation", ["add", "remove", "replace"])
async def test_plan_full_list_operations(store, catalog, recipe, product, catalog_writer, mutation):
    catalog = catalog_writer([product], [recipe, recipe.model_copy(update={"recipe_id": "r2"})])
    initial = await save_plan(store)
    updated = plan()
    if mutation == "add":
        updated.days = 3
        updated.meals.append(Meal(day=3, meal="lunch", recipe_id="r2", servings=1.0))
    elif mutation == "remove":
        updated.meals.pop(0)
    else:
        updated.meals[0].recipe_id = "r2"
    result = await Planner(store, catalog, Editor([updated])).edit(
        "u", "change", initial.data["plan_id"], mutation, RunUsage()
    )
    assert result.data["meals"] == updated.model_dump()["meals"]
    assert result.data["days"] == updated.days


async def test_edit_limits_frozen_during_repair(store, catalog):
    initial = await save_plan(store)
    editor = Editor([plan(budget=10), plan(budget=1000)])
    result = await Planner(store, catalog, editor).edit(
        "u", "edit", initial.data["plan_id"], "Бюджет 10", RunUsage()
    )
    assert result.status == "needs_revision"
    assert result.data["limits"]["budget_rub"] == 10
    assert editor.calls[1]["fixed_limits"]["budget_rub"] == 10
    assert result.issues[0].expected == 10.5
    assert len(editor.calls) == 2


@pytest.mark.parametrize("fails_twice", [True, False])
async def test_format_repair_uses_same_two_call_budget(store, catalog, fails_twice):
    initial = await save_plan(store)
    error = UnexpectedModelBehavior("Некорректный JSON")
    editor = Editor([error, error if fails_twice else plan(recipe_id="missing")])
    result = await Planner(store, catalog, editor).edit(
        "u", "edit", initial.data["plan_id"], "Правка", RunUsage()
    )
    assert result.status == ("error" if fails_twice else "needs_revision")
    assert len(editor.calls) == 2
    assert await store.get_object("u", "plan", initial.data["plan_id"]) == initial


async def test_cart_quantity_keeps_snapshot_no_reselection(store, catalog):
    initial = await save_cart(store)
    await store.cache_skus([sku(price=900)])
    editor, products = Editor([cart(amount=600)]), Products()
    shopper = Shopper(store, catalog, products, editor)
    result = await shopper.edit("u", "edit", initial.data["cart_id"], "Овсянки 600 г", RunUsage())
    assert result.status == "ok"
    assert result.data["items"][0]["product"] == initial.data["items"][0]["product"]
    assert result.data["subtotal_rub"] == 200
    assert products.links == [{"old": 2}]
    assert await shopper.edit("u", "edit", initial.data["cart_id"], "Повтор", RunUsage()) == result
    assert len(editor.calls) == len(products.links) == 1
    assert await store.get_object("u", "cart", initial.data["cart_id"]) == initial


@pytest.mark.parametrize("operation", ["search", "analogs"])
async def test_new_sku_from_public_tools_is_resolved(store, catalog, operation):
    class Search:
        async def search(self, *args):
            return [sku("new", 80.0)]

        async def analogs(self, *args):
            return [sku("new", 80.0)]

    ctx = SimpleNamespace(deps=SimpleNamespace(store=store, products=Search(), catalog=catalog))
    if operation == "search":
        await search_products(ctx, ProductQuery(query="Овсянка"))
    else:
        await get_product_analogs(ctx, SkuId(sku_id="old"))
    initial = await save_cart(store)
    editor = Editor([cart(sku_id="new")])
    result = await Shopper(store, catalog, Products(), editor).edit(
        "u", "edit", initial.data["cart_id"], "Для oats выбери SKU new", RunUsage()
    )
    assert result.status == "ok"
    assert result.data["subtotal_rub"] == 80
    assert editor.calls[0]["selected_skus"][0]["sku_id"] == "new"


@pytest.mark.parametrize("problem", ["missing", "package", "link"])
async def test_cart_partial_and_link_failure_preserve_old(store, catalog, problem):
    initial = await save_cart(store)
    if problem == "package":
        await store.cache_skus([sku("new", amount=None)])
    updated = cart(sku_id="old" if problem == "link" else "new")
    result = await Shopper(store, catalog, Products(fail_link=problem == "link"), Editor([updated])).edit(
        "u", "edit", initial.data["cart_id"], "Измени", RunUsage()
    )
    assert result.status == "partial"
    assert result.data["cart_url"] is None
    if problem == "link":
        assert result.data["link_error"] == "HTTP 502"
        assert result.data["total_is_complete"]
    else:
        assert result.data["unresolved"][0]["recipe_ids"] == ["r1"]
        assert not result.data["total_is_complete"]
    assert await store.get_object("u", "cart", initial.data["cart_id"]) == initial


async def test_cart_add_remove_products_and_unknown_name(store, catalog, product, recipe, catalog_writer):
    rice = product.model_copy(update={"product_id": "rice", "name": "Рис"})
    catalog = catalog_writer([product, rice], [recipe])
    await store.cache_skus([sku("rice-sku", price=50.0)])
    initial = await save_cart(store)
    updated = cart()
    updated.products = [
        CartProduct(product_id="rice", amount=100.0, recipe_ids=[], requested_sku_id="rice-sku")
    ]
    updated.unresolved = [UnresolvedProduct(product_id=None, recipe_ids=[], reason="Нет трюфелей")]
    editor = Editor([updated])
    result = await Shopper(store, catalog, Products(), editor).edit(
        "u",
        "edit",
        initial.data["cart_id"],
        "Убери oats; добавь рис 100 г SKU rice-sku и трюфели",
        RunUsage(),
    )
    assert result.status == "partial"
    assert [p["product_id"] for p in result.data["products"]] == ["rice"]
    assert result.data["subtotal_rub"] == 50
    assert result.data["cart_url"] == "https://example.org/new-cart"
    assert {p["product_id"] for p in editor.calls[0]["products"]} == {"rice", "oats"}
    assert set(await catalog.products()) == {"oats", "rice"}


async def test_cart_budget_repair_and_format_limit(store, catalog):
    initial = await save_cart(store)
    editor = Editor([cart(budget=1), cart(budget=1000)])
    products = Products()
    result = await Shopper(store, catalog, products, editor).edit(
        "u", "edit", initial.data["cart_id"], "Бюджет 1", RunUsage()
    )
    assert result.status == "needs_revision"
    assert result.data["limits"]["budget_rub"] == 1
    assert len(products.links) == 1
    editor = Editor([UnexpectedModelBehavior("bad"), cart(budget=1)])
    result = await Shopper(store, catalog, Products(), editor).edit(
        "u", "format", initial.data["cart_id"], "Бюджет 1", RunUsage()
    )
    assert result.status == "needs_revision" and len(editor.calls) == 2


async def test_foreign_objects_are_not_sent_to_editor(store, catalog):
    initial_plan, initial_cart = await save_plan(store), await save_cart(store)
    editor = Editor([])
    assert (
        await Planner(store, catalog, editor).edit(
            "other", "op", initial_plan.data["plan_id"], "Правка", RunUsage()
        )
    ).error.code == "NOT_FOUND"
    assert (
        await Shopper(store, catalog, Products(), editor).edit(
            "other", "op", initial_cart.data["cart_id"], "Правка", RunUsage()
        )
    ).error.code == "NOT_FOUND"
    assert editor.calls == []


async def test_state_migration_preserves_objects_and_independent_product_index(store, catalog, product):
    initial = await save_cart(store)
    async with store.connection() as db:
        await db.execute("DROP TABLE sku_records")
        await db.commit()
    await store.initialize()
    await store.initialize()
    assert await store.get_object("u", "cart", initial.data["cart_id"]) == initial
    assert list(await catalog.edit_products("Овсянка", [])) == [product.product_id]
    assert (await store.person("u")).memory == ""
    async with store.connection() as db:
        assert not await (await db.execute("SELECT 1 FROM sqlite_master WHERE name='product_fts'")).fetchone()


async def test_product_candidates_limited_but_current_and_explicit_ids_retained(
    store, catalog, product, catalog_writer
):
    variants = [product.model_copy(update={"product_id": f"p{i}", "name": f"Овсянка {i}"}) for i in range(30)]
    catalog = catalog_writer([product, *variants], [])
    result = await catalog.edit_products("Овсянка и p29", ["p28"])
    assert {"p28", "p29"} <= result.keys()
    assert len(result) <= 12


@pytest.mark.parametrize("problem", ["unknown_product", "wrong_unit", "format"])
async def test_cart_edit_integrity_and_format_failures(store, catalog, problem):
    initial = await save_cart(store)
    changed = cart(sku_id="new")
    if problem == "unknown_product":
        changed.products[0].product_id = "invented"
        outputs = [changed]
    elif problem == "wrong_unit":
        await store.cache_skus([sku("new").model_copy(update={"package_unit": "ml"})])
        outputs = [changed]
    else:
        outputs = [UnexpectedModelBehavior("bad"), UnexpectedModelBehavior("bad again")]
    editor = Editor(outputs)
    result = await Shopper(store, catalog, Products(), editor).edit(
        "u", "bad-edit", initial.data["cart_id"], "Правка", RunUsage()
    )
    if problem == "wrong_unit":
        assert result.status == "partial" and result.data["items"] == []
    else:
        assert result.error.code == ("PRODUCT_NOT_FOUND" if problem == "unknown_product" else "MODEL_FORMAT")
        assert len((await store.list_objects("u", "cart")).data) == 1
    assert await store.get_object("u", "cart", initial.data["cart_id"]) == initial


def test_revision_rejects_boolean_and_string_day():
    from pydantic import ValidationError

    for invalid in (True, "2"):
        data = plan().model_dump()
        data["days"] = invalid
        with pytest.raises(ValidationError):
            PlanRevision.model_validate(data)
        data["days"] = 2
        data["meals"][0]["day"] = invalid
        with pytest.raises(ValidationError):
            PlanRevision.model_validate(data)


def test_reassembling_unresolved_product_preserves_reason(product):
    products = [CartProduct(product_id=product.product_id, amount=50, recipe_ids=["r1"])]
    unresolved = [
        {"product_id": product.product_id, "recipe_ids": ["r1"], "reason": "Овсянка: Фасовка неизвестна"}
    ]
    for _ in range(3):
        data, _, _ = assemble(
            products, {product.product_id: product}, {}, unresolved, {}, CartLimits(budget_rub=None)
        )
        assert data["unresolved"] == unresolved
        unresolved = data["unresolved"]
