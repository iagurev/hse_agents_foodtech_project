import copy

import pytest
from pydantic import ValidationError
from pydantic_ai.usage import RunUsage

from app.assistant.tools import build_toolset
from app.models import Meal, PersonContext, PlanDraft, PlanLimits, PlanRevision
from app.planning import Planner, evaluate


class ScriptedGenerator:
    def __init__(self, outputs: list) -> None:
        self.outputs, self.calls = list(outputs), []

    async def generate(
        self, prompt: str, schema: type, payload: dict, usage: RunUsage, effort: str = "low"
    ) -> object:
        self.calls.append(copy.deepcopy(payload))
        usage.requests += 1
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def draft(recipe_id: str = "r1", servings: float = 1.0) -> PlanDraft:
    return PlanDraft(
        meals=[Meal(day=1, meal="breakfast", recipe_id=recipe_id, servings=servings)],
        explanation="",
    )


def test_tool_contracts() -> None:
    tools = build_toolset().tools
    assert len(tools) == 13
    for name in ("list_plans", "list_carts"):
        assert tools[name].function_schema.json_schema["properties"] == {}
    schema = tools["generate_plan"].function_schema.json_schema
    assert schema["required"] == ["request", "days", "limits"]
    assert schema["$defs"]["PlanLimits"]["required"] == ["max_difficulty", "budget_rub"]
    with pytest.raises(ValidationError):
        PlanLimits.model_validate({})
    assert "user_id" not in str(schema)


def test_metrics_and_exact_checks(recipe) -> None:
    limits = PlanLimits(max_difficulty="easy", budget_rub=100.0)
    metrics, issues = evaluate(draft(servings=2.0), {"r1": recipe}, limits)
    assert metrics["totals"]["kcal"] == 360
    assert metrics["totals"]["mean_rub"] == 40
    assert issues == []
    difficult = recipe.model_copy(update={"difficulty": "advanced"})
    _, issues = evaluate(draft(servings=10.0), {"r1": difficult}, limits)
    assert {i.code for i in issues} == {"DIFFICULTY_EXCEEDED", "BUDGET_EXCEEDED"}
    _, issues = evaluate(draft("missing"), {"r1": recipe}, limits)
    assert [i.code for i in issues] == ["RECIPE_NOT_FOUND"]
    bad = draft()
    bad.meals[0] = bad.meals[0].model_copy(update={"servings": float("nan")})
    _, issues = evaluate(bad, {"r1": recipe}, limits)
    assert [i.code for i in issues] == ["INVALID_NUMBER"]
    # Отсутствие полноты меню не становится пятым критерием.
    _, issues = evaluate(PlanDraft(meals=[], explanation=""), {}, limits)
    assert issues == []


async def test_one_repair_full_catalog_and_preserved_limits(store, catalog) -> None:
    generator = ScriptedGenerator([draft("unknown"), draft("still_missing")])
    planner = Planner(store, catalog, generator)
    limits = PlanLimits(max_difficulty="easy", budget_rub=50.0)
    result = await planner.run("u", "operation", "Запрос", 7, limits, PersonContext(), RunUsage())
    assert result.status == "needs_revision"
    assert result.data["plan_id"]
    assert len(generator.calls) == 2
    assert generator.calls[0]["catalog"] == generator.calls[1]["catalog"]
    assert generator.calls[1]["limits"] == limits.model_dump()
    assert generator.calls[1]["feedback"][0]["code"] == "RECIPE_NOT_FOUND"
    assert "catalog" not in result.data
    repeated = await planner.run("u", "operation", "Запрос", 7, limits, PersonContext(), RunUsage())
    assert repeated == result
    assert len(generator.calls) == 2


@pytest.mark.parametrize("servings", [0.0, -1.0])
def test_invalid_servings_do_not_reduce_totals_or_bypass_budget(recipe, servings):
    candidate = draft(servings=6.0)
    candidate.meals.append(draft(servings=servings).meals[0])
    metrics, issues = evaluate(candidate, {"r1": recipe}, PlanLimits(max_difficulty=None, budget_rub=100.0))
    assert metrics["totals"]["kcal"] == 1080.0
    assert metrics["totals"]["mean_rub"] == 120.0
    assert [issue.code for issue in issues] == ["INVALID_NUMBER", "BUDGET_EXCEEDED"]
    assert issues[0].path == "meals.1.servings"
    assert issues[0].expected == "finite number > 0"


@pytest.mark.parametrize("operation", ["generate", "edit"])
@pytest.mark.parametrize("repaired", [False, True])
async def test_servings_repair_is_bounded_and_preserves_readable_versions(
    store, catalog, operation, repaired
):
    from app.assistant.tools import PlanResult

    limits = PlanLimits(max_difficulty="easy", budget_rub=100.0)
    final_servings = 0.5 if repaired else 0.0
    candidates = [draft(servings=-1.0), draft(servings=final_servings)]
    if operation == "edit":
        outputs = [draft()] + [
            PlanRevision(**candidate.model_dump(), days=1, limits=limits) for candidate in candidates
        ]
    else:
        outputs = candidates
    generator = ScriptedGenerator(outputs)
    planner = Planner(store, catalog, generator)
    initial = None
    if operation == "edit":
        initial = await planner.run("u", "initial", "Завтрак", 1, limits, PersonContext(), RunUsage())
        result = await planner.edit("u", "change", initial.data["plan_id"], "Измени порции", RunUsage())
    else:
        result = await planner.run("u", "create", "Завтрак", 1, limits, PersonContext(), RunUsage())

    assert len(generator.calls) == (3 if initial else 2)
    feedback = generator.calls[-1]["feedback"]
    assert feedback[0]["code"] == "INVALID_NUMBER"
    assert feedback[0]["repair_scope"] == "meals.0.servings"
    assert result.status == ("ok" if repaired else "needs_revision")
    assert [issue.code for issue in result.issues] == ([] if repaired else ["INVALID_NUMBER"])
    assert result.data["totals"]["kcal"] == (90.0 if repaired else 0.0)
    assert result.data["totals"]["mean_rub"] == (10.0 if repaired else 0.0)
    assert result.data["limits"] == limits.model_dump()
    saved = await store.get_object("u", "plan", result.data["plan_id"])
    assert saved == result
    assert PlanResult.from_result(saved).data.meals[0].servings == final_servings
    if initial:
        assert result.data["parent_id"] == initial.data["plan_id"]
        assert await store.get_object("u", "plan", initial.data["plan_id"]) == initial


async def test_failed_edit_keeps_successful_version(store, catalog) -> None:
    bad = PlanRevision(
        **draft("bad").model_dump(), days=1, limits=PlanLimits(max_difficulty=None, budget_rub=None)
    )
    generator = ScriptedGenerator([draft(), bad, bad])
    planner = Planner(store, catalog, generator)
    limits = PlanLimits(max_difficulty=None, budget_rub=None)
    first = await planner.run("u", "first", "Создай", 1, limits, PersonContext(), RunUsage())
    edited = await planner.edit("u", "second", first.data["plan_id"], "Измени", RunUsage())
    assert edited.status == "needs_revision"
    assert edited.data["parent_id"] == first.data["plan_id"]
    assert await store.get_object("u", "plan", first.data["plan_id"]) == first
    assert edited.data["plan_id"] != first.data["plan_id"]


async def test_format_repair_counts_towards_two_calls(store, catalog) -> None:
    try:
        PlanDraft.model_validate(
            {
                "meals": [{"day": 1, "meal": "breakfast", "recipe_id": "r1", "servings": "x"}],
                "explanation": "",
            }
        )
    except ValidationError as error:
        invalid = error
    generator = ScriptedGenerator([invalid, draft("bad")])
    result = await Planner(store, catalog, generator).run(
        "u",
        "format",
        "Запрос",
        1,
        PlanLimits(max_difficulty=None, budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert result.status == "needs_revision"
    assert len(generator.calls) == 2


async def test_catalog_search_and_batch(catalog, recipe) -> None:
    result = await catalog.search("овсян")
    assert result.data["recipes"][0]["recipe_id"] == recipe.recipe_id
    details = await catalog.get(["r1", "missing", "r1"])
    assert len(details.data["recipes"]) == 1
    assert set(details.data) == {"recipes"}
    assert details.data["recipes"][0]["steps"]


async def test_catalog_not_cut_for_size(store, catalog, recipe, product, catalog_writer) -> None:
    many = [recipe.model_copy(update={"recipe_id": f"r{i}"}) for i in range(700)]
    catalog = catalog_writer([product], many)
    generator = ScriptedGenerator([draft("r699")])
    result = await Planner(store, catalog, generator).run(
        "u", "large", "Меню", 1, PlanLimits(max_difficulty=None, budget_rub=None), PersonContext(), RunUsage()
    )
    assert result.status == "ok"
    assert len(generator.calls[0]["catalog"]) == 700


@pytest.mark.parametrize("mean,exceeded", [(94.0, False), (105.0, False), (105.01, True)])
def test_plan_budget_five_percent_tolerance(recipe, mean, exceeded):
    recipe = recipe.model_copy(update={"cost": recipe.cost.model_copy(update={"mean_rub": mean})})
    limits = PlanLimits(max_difficulty=None, budget_rub=100.0)
    _, issues = evaluate(draft(), {"r1": recipe}, limits)
    assert bool(issues) == exceeded
    assert limits.budget_rub == 100
    if exceeded:
        assert issues[0].expected == 105


def test_fixed_search_schemas_and_plan_output():
    tools = build_toolset().tools
    for name, argument in [
        ("search_recipes", "query"),
        ("search_products", "query"),
        ("get_product_analogs", "sku_id"),
    ]:
        schema = tools[name].function_schema.json_schema
        assert set(schema["properties"]) == (
            {argument, "constraints"} if name == "search_recipes" else {argument}
        )
    assert set(PlanDraft.model_json_schema()["properties"]) == {"meals", "explanation"}


async def test_recipe_projection_photos_and_fixed_top_ten(store, catalog, recipe, product, catalog_writer):
    recipes = [recipe.model_copy(update={"recipe_id": f"r{i}"}) for i in range(15)]
    catalog = catalog_writer([product], recipes)
    result = await catalog.search("каша")
    assert set(result.data) == {"recipes"}
    assert len(result.data["recipes"]) == 10
    card = result.data["recipes"][0]
    assert card["image_url"] == recipe.image_url
    assert card["restrictions"] == recipe.restrictions
    assert not {"techniques", "composition", "provenance", "estimates"} & card.keys()
    details = await catalog.get([card["recipe_id"], "absent"])
    assert set(details.data) == {"recipes"}
    full = details.data["recipes"][0]
    assert full["image_url"] == recipe.image_url
    assert full["ingredients"][0] == {"product_id": "oats", "amount": 50.0, "name": "Овсянка", "unit": "g"}
    assert not {"techniques", "composition", "provenance", "estimates"} & full.keys()
    assert (await catalog.search("!!!")).data == {"recipes": []}
    generator = ScriptedGenerator([draft()])
    await Planner(store, catalog, generator).run(
        "u",
        "projection",
        "Меню",
        1,
        PlanLimits(max_difficulty=None, budget_rub=None),
        PersonContext(),
        RunUsage(),
    )
    assert all("image_url" not in r for r in generator.calls[0]["catalog"])
