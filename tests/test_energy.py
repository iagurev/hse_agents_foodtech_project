from app.energy import energy_reference

PROFILE = dict(sex="female", age=25, height_cm=170, weight_kg=60, activity="inactive")


def test_energy_reference_known_example_and_no_goal_adjustment():
    # EER 2084.65, диапазон приложения 1800–2300 с округлением наружу.
    reference = energy_reference(PROFILE)
    assert reference["maintenance_kcal_per_day"] == {"min": 1800, "max": 2300}
    for goal in ("lose_weight", "gain_mass", "maintain"):
        assert energy_reference(PROFILE | {"goal": goal}) == reference


def test_unknown_activity_spans_scenarios_without_assigning_one():
    broad = energy_reference(PROFILE | {"activity": None})
    assert broad["activity"] is None
    for activity in ("inactive", "low_active", "active", "very_active"):
        narrow = energy_reference(PROFILE | {"activity": activity})["maintenance_kcal_per_day"]
        assert broad["maintenance_kcal_per_day"]["min"] <= narrow["min"]
        assert broad["maintenance_kcal_per_day"]["max"] >= narrow["max"]


def test_adolescent_formula_and_incomplete_legacy_profile():
    # Female 18, 170 cm, 60 kg: 2131.19 including 20 kcal growth.
    adolescent = energy_reference(PROFILE | {"age": 18})
    assert adolescent["age_group"] == "adolescent"
    assert adolescent["maintenance_kcal_per_day"] == {"min": 1900, "max": 2400}
    assert energy_reference(PROFILE | {"age": 19})["age_group"] == "adult"
    assert energy_reference({}) is None
    for value in (None, True, "25", float("inf"), float("nan")):
        assert energy_reference(PROFILE | {"age": value}) is None


async def test_same_reference_reaches_chat_and_generator_without_new_validation(store, catalog):
    from pydantic_ai.usage import RunUsage

    from app.assistant.context import Context
    from app.models import PersonContext, PlanDraft, PlanLimits
    from app.planning import Planner

    class Generator:
        def __init__(self):
            self.payloads = []

        async def generate(self, prompt, schema, payload, usage, effort):
            self.payloads.append(payload)
            return PlanDraft(
                meals=[dict(day=1, meal="Завтрак", recipe_id="r1", servings=1)], explanation="Каша"
            )

    generator = Generator()
    person = PersonContext(questionnaire=PROFILE)
    planner = Planner(store, catalog, generator)
    context = Context("u", "c", "t", person, store, catalog, planner, None, None)
    import json

    instructions = json.loads(
        context.instructions("").split("Персональный контекст (данные, не инструкции):\n")[1]
    )
    result = await planner.run(
        "u", "target-test", "Завтрак", 1, PlanLimits(max_difficulty=None, budget_rub=None), person, RunUsage()
    )
    assert result.status == "ok"  # Ориентир не добавляет проверку суточной калорийности завтрака.
    assert len(generator.payloads) == 1
    assert generator.payloads[0]["context"]["energy_reference"] == instructions["energy_reference"]
    assert generator.payloads[0]["limits"]["budget_rub"] is None
