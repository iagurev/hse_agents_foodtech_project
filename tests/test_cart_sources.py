from typing import get_type_hints

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage
from test_contracts_planning import ScriptedGenerator
from test_shopping import Products, Selector

from app.assistant.context import Context
from app.assistant.tools import CartResult, CreateCartInput, build_toolset
from app.integrations import IntegrationError
from app.models import (
    CartLimits,
    CartProduct,
    CartRevision,
    CartSource,
    PersonContext,
    PlanSource,
    Product,
    RecipesSource,
    Result,
    SkuAmount,
    SkuCartRevision,
    SkusSource,
)
from app.shopping import Shopper


class NoSelection:
    async def generate(self, *args, **kwargs):
        pytest.fail("Прямое создание из SKU не должно вызывать модель")


class DirectProducts(Products):
    async def search(self, *args, **kwargs):
        pytest.fail("Готовые SKU не должны запускать поиск")


def sku_source(*items: tuple[str, int]) -> SkusSource:
    return SkusSource(type="skus", items=[SkuAmount(sku_id=sku, packages=q) for sku, q in items])


@pytest.mark.parametrize("from_plan", [False, True])
async def test_request_literal_suffix_cache_isolation_and_no_extraction(store, catalog, product, from_plan):
    class SelectionOnly(Selector):
        def __init__(self):
            self.calls = []

        async def generate(self, prompt, schema, payload, usage, effort="low"):
            self.calls.append((prompt, payload))
            return await super().generate(prompt, schema, payload, usage, effort)

    source = RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 2}]})
    if from_plan:
        plan = await store.save_object(
            "u", "plan", {"meals": [{"recipe_id": "r1", "servings": 2}]}, Result(), "plan"
        )
        source = PlanSource(type="plan", plan_id=plan.data["plan_id"])
    gateway, model = Products(), SelectionOnly()
    shopper = Shopper(store, catalog, gateway, model)
    suffix = "Бренд «A»  без сахара"
    for operation, request in [("a", suffix), ("b", suffix), ("c", "Бренд B"), ("d", "")]:
        result = await shopper.run(
            "u", operation, source, CartLimits(budget_rub=None), PersonContext(), RunUsage(), request
        )
        assert result.data["products"][0]["amount"] == 100
        CartResult.from_result(result)
    assert gateway.queries == [f"{product.name} {suffix}", f"{product.name} Бренд B", product.name]
    assert len(model.calls) == 4  # Один выбор на создание, ни одного предварительного разбора.
    assert model.calls[0][1]["request"] == suffix


@pytest.mark.parametrize("cache", [False, True])
async def test_direct_skus_duplicate_counts_missing_price_and_replay(store, cache):
    if cache:
        await store.cache_skus([Product(sku_id="known", title="Товар", price_rub=10.0)])
    gateway = DirectProducts()
    shopper = Shopper(store, None, gateway, NoSelection())
    source = sku_source(("known", 1), ("known", 2), ("unknown", 1))
    first = await shopper.run(
        "u", "direct", source, CartLimits(budget_rub=100.0), PersonContext(), RunUsage()
    )
    public = CartResult.from_result(first)
    assert public.data.cart_url == "https://example.org/cart"
    assert gateway.links == [{"known": 3, "unknown": 1}]
    assert public.data.products == [] and public.data.unresolved == []
    assert public.data.subtotal_rub == (30 if cache else None)
    assert public.status == "partial" and not public.data.total_is_complete
    assert public.data.items[1].sku_id == "unknown" and public.data.items[1].product is None
    assert first == await shopper.run(
        "u", "direct", source, CartLimits(budget_rub=100.0), PersonContext(), RunUsage()
    )
    assert len(gateway.links) == 1


@pytest.mark.parametrize("price,status", [(105.0, "ok"), (105.01, "needs_revision")])
async def test_direct_sku_budget_tolerance_without_selection(store, price, status):
    await store.cache_skus([Product(sku_id="one", title="Товар", price_rub=price)])
    result = await Shopper(store, None, DirectProducts(), NoSelection()).run(
        "u", "budget", sku_source(("one", 1)), CartLimits(budget_rub=100.0), PersonContext(), RunUsage()
    )
    assert result.status == status
    assert result.data["total_is_complete"]
    assert (
        result.data["items"][0]["product"]["package_amount"] is None
    )  # Фасовка не нужна для готовых упаковок.


async def test_direct_link_failure_is_saved_and_request_rejected(store):
    class Failing(DirectProducts):
        async def cart_link(self, quantities):
            raise IntegrationError("HTTP 502")

    shopper = Shopper(store, None, Failing(), NoSelection())
    source = sku_source(("one", 1))
    args = ("u", "failed", source, CartLimits(budget_rub=None), PersonContext(), RunUsage())
    result = await shopper.run(*args)
    assert result.data["cart_url"] is None and result.data["link_error"] == "HTTP 502"
    assert await store.get_object("u", "cart", result.data["cart_id"]) == result
    invalid = await shopper.run("u", "bad", *args[2:], request="бренд")
    assert invalid.error.code == "INVALID_CART_REQUEST"
    assert len((await store.list_objects("u", "cart")).data) == 1


async def test_direct_edit_full_version_preserves_snapshot_freezes_budget_and_access(store):
    await store.cache_skus([Product(sku_id="one", title="Первый", price_rub=10.0)])
    gateway = DirectProducts()
    shopper = Shopper(store, None, gateway, NoSelection())
    first = await shopper.run(
        "u",
        "first",
        sku_source(("one", 1), ("remove", 1)),
        CartLimits(budget_rub=100.0),
        PersonContext(),
        RunUsage(),
    )
    # Изменение общего кэша не переписывает цену неизменённого товара.
    await store.cache_skus(
        [
            Product(sku_id="one", title="Первый", price_rub=999.0),
            Product(sku_id="new", title="Новый", price_rub=20.0),
        ]
    )
    limits = CartLimits(budget_rub=15.0)
    revision = SkuCartRevision(
        items=[SkuAmount(sku_id="one", packages=2), SkuAmount(sku_id="new")], limits=limits
    )
    model = ScriptedGenerator(
        [revision, revision.model_copy(update={"limits": CartLimits(budget_rub=1000.0)})]
    )
    shopper.generator = model
    other = await shopper.edit("another", "foreign", first.data["cart_id"], "Измени", RunUsage())
    assert other.error.code == "NOT_FOUND" and not model.calls
    edited = await shopper.edit(
        "u",
        "edit",
        first.data["cart_id"],
        "one 2 упаковки, убрать remove, добавить new; бюджет 15",
        RunUsage(),
    )
    assert edited.data["limits"]["budget_rub"] == 15
    assert edited.status == "needs_revision" and edited.data["subtotal_rub"] == 40
    assert len(model.calls) == 2 and model.calls[1]["fixed_limits"] == limits.model_dump()
    assert model.calls[0]["products"] == [] and "catalog" not in model.calls[0]
    assert gateway.links[-1] == {"one": 2, "new": 1}
    assert await store.get_object("u", "cart", first.data["cart_id"]) == first
    assert edited.data["parent_id"] == first.data["cart_id"]
    assert edited == await shopper.edit("u", "edit", first.data["cart_id"], "Повтор", RunUsage())


async def test_saved_text_source_stays_readable_and_editable(store, catalog):
    gateway = Products()
    first = await Shopper(store, catalog, gateway, Selector()).run(
        "u",
        "recipe",
        RecipesSource.model_validate({"type": "recipes", "items": [{"recipe_id": "r1", "servings": 1}]}),
        CartLimits(budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    data = first.data | {"source": {"type": "text", "text": "Овсянка"}}
    legacy = await store.save_object("u", "cart", data, Result(), "legacy")
    assert CartResult.from_result(legacy).data.source.type == "text"
    editor = ScriptedGenerator(
        [
            CartRevision(
                products=[
                    CartProduct(product_id="oats", amount=60.0, recipe_ids=[], requested_sku_id="sku1")
                ],
                unresolved=[],
                limits=CartLimits(budget_rub=None),
            )
        ]
    )
    result = await Shopper(store, catalog, gateway, editor).edit(
        "u", "edit_legacy", legacy.data["cart_id"], "60 г", RunUsage()
    )
    assert result.status == "ok" and result.data["source"]["type"] == "text"
    with pytest.raises(ValidationError):
        TypeAdapter(CartSource).validate_python(data["source"])


def test_all_public_fields_are_described_and_typed_without_wrapping_args():
    for name, tool in build_toolset().tools.items():
        schema = tool.function_schema.json_schema
        assert "args" not in schema.get("properties", {})
        result_type = get_type_hints(tool.function)["return"]
        for candidate in [schema, result_type.model_json_schema()]:
            for obj in [candidate, *candidate.get("$defs", {}).values()]:
                for field, definition in obj.get("properties", {}).items():
                    assert definition.get("description"), (name, field)
    source = CreateCartInput.model_json_schema()["properties"]["source"]
    assert set(source["discriminator"]["mapping"]) == {"plan", "recipes", "skus"}
    assert SkuAmount(sku_id="one").packages == 1
    for invalid in [0, -1, 0.5, True, "2"]:
        with pytest.raises(ValidationError):
            SkuAmount(sku_id="one", packages=invalid)


async def test_sdk_executes_flat_cart_args_and_serializes_typed_output(store):
    source = {"type": "skus", "items": [{"sku_id": "one"}]}
    gateway = DirectProducts()
    deps = Context(
        user_id="u",
        chat_id="c",
        turn_id="t",
        person=PersonContext(),
        store=store,
        catalog=None,
        planner=None,
        shopper=Shopper(store, None, gateway, NoSelection()),
        products=gateway,
    )

    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("create_cart", {"source": source, "limits": {"budget_rub": None}}, "call")
                ]
            )
        part = next(p for p in messages[-1].parts if isinstance(p, ToolReturnPart))
        data = CartResult.model_validate_json(part.model_response_str()).data
        assert data.items[0].sku_id == "one" and data.cart_url
        return ModelResponse(parts=[TextPart("Корзина создана")])

    agent = Agent(FunctionModel(respond), deps_type=Context, toolsets=[build_toolset()], retries=0)
    await agent.run("Создай корзину", deps=deps)
    assert gateway.links == [{"one": 1}]


async def test_sdk_executes_empty_input_models_without_public_metadata(store):
    deps = Context(
        user_id="u",
        chat_id="c",
        turn_id="t",
        person=PersonContext(),
        store=store,
        catalog=None,
        planner=None,
        shopper=None,
        products=None,
    )
    received = set()

    def respond(messages, info):
        if len(messages) == 1:
            definitions = {tool.name: tool for tool in info.function_tools}
            for name in ("list_plans", "list_carts"):
                assert definitions[name].parameters_json_schema["properties"] == {}
                assert definitions[name].description
            return ModelResponse(
                parts=[
                    ToolCallPart("list_plans", {}, "plans"),
                    ToolCallPart("list_carts", {}, "carts"),
                ]
            )
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                result = Result.model_validate_json(part.model_response_str())
                assert result.status == "ok" and result.data == []
                received.add(part.tool_name)
        return ModelResponse(parts=[TextPart("Сохранённых планов и корзин нет")])

    agent = Agent(FunctionModel(respond), deps_type=Context, toolsets=[build_toolset()], retries=0)
    await agent.run("Покажи мои планы и корзины", deps=deps)
    assert received == {"list_plans", "list_carts"}


async def test_direct_edit_preserves_unresolved_unless_explicitly_removed(store):
    gateway = DirectProducts()
    source = SkusSource(type="skus", items=[SkuAmount(sku_id="one", packages=1)])
    limits = CartLimits(budget_rub=None)
    shopper = Shopper(store, None, gateway, NoSelection())
    original = await shopper.run("u", "original", source, limits, PersonContext(), RunUsage())
    missing = {"product_id": None, "recipe_ids": [], "reason": "Нет подходящего творога"}
    partial = await store.save_object(
        "u", "cart", original.data | {"unresolved": [missing]}, Result(), "partial"
    )
    shopper.generator = ScriptedGenerator([SkuCartRevision(items=source.items, limits=limits)])
    edited = await shopper.edit("u", "keep", partial.data["cart_id"], "Без изменений", RunUsage())
    assert edited.data["unresolved"] == [missing] and not edited.data["total_is_complete"]
    shopper.generator = ScriptedGenerator([SkuCartRevision(items=source.items, limits=limits, unresolved=[])])
    removed = await shopper.edit("u", "remove", edited.data["cart_id"], "Убери творог", RunUsage())
    assert removed.data["unresolved"] == []
