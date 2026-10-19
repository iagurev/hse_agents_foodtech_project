import json
from copy import deepcopy

import pytest
from pydantic import ValidationError
from pydantic_ai.usage import RunUsage

from app.models import Cost, Ingredient, Meal, Nutrition, PersonContext, PlanDraft, PlanLimits, Recipe
from app.planning import Planner


def test_round_recipe_numbers_without_rounding_ingredients_or_source_products(recipe, product):
    data = recipe.model_dump()
    data["nutrition"] = dict(kcal=180.5, protein_g=6.49, fat_g=0.5, carbs_g=30.01)
    data["cost"] = dict(mean_rub=2.675, min_rub=1.005, max_rub=9.999)
    data["ingredients"][0]["amount"] = 0.25
    value = Recipe.model_validate(data)
    stored = json.loads(value.model_dump_json())
    assert stored["nutrition"] == dict(kcal=181, protein_g=6, fat_g=1, carbs_g=30)
    assert all(type(v) is int for v in stored["nutrition"].values())
    assert stored["cost"] == dict(mean_rub=2.68, min_rub=1.01, max_rub=10.0)
    assert value.ingredients[0].amount == 0.25
    assert Recipe.model_validate_json(value.model_dump_json()) == value
    assert Nutrition(kcal=1.25, protein_g=0.33, fat_g=0.5, carbs_g=0.75).protein_g == 0.33
    assert Cost(mean_rub=2.675, min_rub=1.005, max_rub=9.999).mean_rub == 2.675
    assert product.base_amount == 100.0


@pytest.mark.parametrize("field", ["nutrition", "cost"])
def test_recipe_rounding_keeps_numeric_validation(recipe, field):
    data = recipe.model_dump()
    key = next(iter(data[field]))
    data[field][key] = float("nan")
    with pytest.raises(ValidationError):
        Recipe.model_validate(data)


def test_projection_deduplicates_names_and_retains_specific_limits(recipe, product):
    duplicate = product.model_copy(update={"product_id": "other"})
    value = recipe.model_copy(deep=True)
    value.ingredients += [
        Ingredient(product_id="oats", amount=1.0),
        Ingredient(product_id="other", amount=2.0),
    ]
    value.restrictions = (
        "Авторская адаптация: состав, граммовки и порции заданы редактором. "
        "Цена снимка 17.10.2026, без округления упаковок. "
        "Шаги даны на 4 порций; состав и показатели карточки — на одну. "
        "Масса яиц указана без скорлупы. "
        "Содержит овёс; отсутствие глютена не подтверждено. Нужна духовка."
    )
    before = deepcopy(value.model_dump())
    card = value.planning_card({"oats": product, "other": duplicate})
    assert card["ingredients"] == ["Овсянка"]
    assert card["restrictions"] == "Содержит овёс; отсутствие глютена не подтверждено. Нужна духовка."
    assert not {"image_url", "steps", "source_url"} & card.keys()
    assert card["equipment"] == value.equipment
    assert card["base_serving"] == value.base_serving
    assert value.model_dump() == before
    assert value.card({"oats": product, "other": duplicate})["ingredients"][0]["product_id"] == "oats"


async def test_planner_uses_projection_for_both_attempts(store, catalog):
    class Generator:
        def __init__(self):
            self.calls = []

        async def generate(self, prompt, schema, payload, usage, effort):
            self.calls.append(deepcopy(payload))
            return PlanDraft(
                meals=[
                    Meal(
                        day=1,
                        meal="breakfast",
                        recipe_id="missing" if len(self.calls) == 1 else "r1",
                        servings=1,
                    )
                ],
                explanation="",
            )

    generator = Generator()
    result = await Planner(store, catalog, generator).run(
        "u",
        "compact",
        "Завтрак",
        1,
        PlanLimits(max_difficulty=None, budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == "ok"
    for payload in generator.calls:
        assert payload["catalog"][0]["ingredients"] == ["Овсянка"]
        assert payload["catalog"][0]["restrictions"] == "Овёс; отсутствие глютена не подтверждено."
    assert generator.calls[0]["catalog"] == generator.calls[1]["catalog"]
