from copy import deepcopy

import pytest
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from app.models import Choice, Selection
from app.shopping.selection import select_products
from app.shopping.transport import selection_input


def payload():
    return {
        "products": [
            {"product_id": key, "amount": 10, "recipe_ids": ["recipe"], "requested_sku_id": None}
            for key in ("a", "b", "missing")
        ],
        "catalog": [
            {"product_id": key, "name": name, "unit": "g", "restrictions": "", "conversions": {}}
            for key, name in [("a", "Овсянка"), ("b", "Корица"), ("missing", "Редкий продукт")]
        ],
        "candidates": {
            "a": [{"sku_id": "oats", "title": "Овсянка", "purchase": {"cost_rub": 100}}],
            "b": [{"sku_id": "cinnamon", "title": "Корица", "purchase": {"cost_rub": 50}}],
            "missing": [],
        },
        "context": {"questionnaire": {"restrictions": "Без рыбы"}},
        "limits": {"budget_rub": 200},
        "request": "",
        "recipes": [{"recipe_id": "recipe", "title": "Каша", "steps": ["Добавьте корицу"]}],
    }


def selection(**skus):
    return Selection(choices=[Choice(product_id=key, sku_id=sku, reason="") for key, sku in skus.items()])


class Generator:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    async def generate(self, prompt, schema, payload, usage, effort="low"):
        self.calls.append(deepcopy(payload))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def test_selection_transport_keeps_ingredients_next_to_own_candidates():
    original = payload()
    packed = selection_input(original)
    assert packed["items"][1]["name"] == "Корица"
    assert packed["items"][1]["amount"] == 10
    assert packed["items"][1]["candidates"][0]["sku_id"] == "cinnamon"
    assert packed["items"][2]["candidates"] == []
    assert packed["context"] == original["context"] and packed["limits"] == original["limits"]
    assert original == payload()


@pytest.mark.parametrize("omission", [None, "invented"])
async def test_recheck_only_missing_choices_and_ignore_changes_to_confirmed_items(omission):
    generator = Generator(
        [
            selection(a="oats", b=omission),
            selection(a="changed", b="cinnamon", missing="invented"),
        ]
    )
    choices = await select_products(generator, payload(), RunUsage())
    assert choices["a"].sku_id == "oats" and choices["b"].sku_id == "cinnamon"
    assert "missing" not in choices
    retry = generator.calls[1]
    assert [row["product_id"] for row in retry["products"]] == ["b"]
    assert set(retry["candidates"]) == {"b"}
    assert retry["selected_cost_rub"] == 100
    assert retry["context"] == payload()["context"] and retry["limits"] == payload()["limits"]


@pytest.mark.parametrize(
    "failure", [None, ModelHTTPError(503, "unavailable", {}), UsageLimitExceeded("limit")]
)
async def test_recheck_is_bounded_and_preserves_initial_choices_on_failure(failure):
    generator = Generator(
        [
            selection(a="oats", b=None),
            failure or selection(b=None),
        ]
    )
    result = await select_products(generator, payload(), RunUsage())
    assert len(generator.calls) == 2 and result["a"].sku_id == "oats"
    assert result["b"].sku_id is None


async def test_complete_selection_does_not_recheck_absent_candidates():
    generator = Generator([selection(a="oats", b="cinnamon", missing=None)])
    await select_products(generator, payload(), RunUsage())
    assert len(generator.calls) == 1
