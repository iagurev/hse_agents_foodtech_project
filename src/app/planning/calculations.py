import math

from pydantic import JsonValue

from app.models import (
    Issue,
    PlanDraft,
    PlanLimits,
    Recipe,
    budget_ceiling,
)


def evaluate(
    plan: PlanDraft, recipes: dict[str, Recipe], limits: PlanLimits
) -> tuple[dict[str, JsonValue], list[Issue]]:
    """Пересчитывает показатели и проверяет ID, сложность и бюджет.

    PlanDraft проверяет формат чисел; здесь дополнительно проверяется положительность порций.
    Смысловые проверки сюда не добавляются.
    :param plan: Выбор модели
    :param recipes: Каталог
    :param limits: Явные пределы
    :return: Итоги и замечания из согласованного набора
    """
    totals = dict(kcal=0.0, protein_g=0.0, fat_g=0.0, carbs_g=0.0, mean_rub=0.0, min_rub=0.0, max_rub=0.0)
    daily: dict[str, dict[str, float]] = {}
    issues: list[Issue] = []
    ranks = {"easy": 0, "intermediate": 1, "advanced": 2}
    for i, meal in enumerate(plan.meals):
        if (
            isinstance(meal.servings, bool)
            or not isinstance(meal.servings, (int, float))
            or not math.isfinite(meal.servings)
            or meal.servings <= 0
        ):
            issues.append(
                Issue(
                    code="INVALID_NUMBER",
                    path=f"meals.{i}.servings",
                    message="Количество порций должно быть конечным числом больше нуля",
                    actual=str(meal.servings),
                    expected="finite number > 0",
                    requirement_source="numeric format",
                    repair_scope=f"meals.{i}.servings",
                )
            )
            continue
        recipe = recipes.get(meal.recipe_id)
        if recipe is None:
            issues.append(
                Issue(
                    code="RECIPE_NOT_FOUND",
                    path=f"meals.{i}.recipe_id",
                    message="Рецепта нет в каталоге",
                    actual=meal.recipe_id,
                    expected="recipe_id из catalog",
                    requirement_source="catalog",
                    repair_scope=f"meals.{i}",
                )
            )
            continue
        if limits.max_difficulty and ranks[recipe.difficulty] > ranks[limits.max_difficulty]:
            issues.append(
                Issue(
                    code="DIFFICULTY_EXCEEDED",
                    path=f"meals.{i}.recipe_id",
                    message="Сложность рецепта превышает предел",
                    actual=recipe.difficulty,
                    expected=limits.max_difficulty,
                    requirement_source="limits.max_difficulty",
                    repair_scope=f"meals.{i}",
                )
            )
        values = recipe.nutrition.model_dump() | recipe.cost.model_dump()
        day = daily.setdefault(str(meal.day), dict.fromkeys(totals, 0.0))
        for key, value in values.items():
            totals[key] += value * meal.servings
            day[key] += value * meal.servings
    for key, value in totals.items():
        if not math.isfinite(value):
            issues.append(
                Issue(
                    code="INVALID_NUMBER",
                    path=f"totals.{key}",
                    message="Переполнение при пересчёте",
                    actual=str(value),
                    expected="finite number",
                    requirement_source="numeric format",
                    repair_scope="meals.servings",
                )
            )
    totals = {k: round(v, 2) if math.isfinite(v) else None for k, v in totals.items()}
    if (
        limits.budget_rub is not None
        and totals["mean_rub"] is not None
        and totals["mean_rub"] > budget_ceiling(limits.budget_rub)
    ):
        issues.append(
            Issue(
                code="BUDGET_EXCEEDED",
                path="totals.mean_rub",
                message="Средняя стоимость выше бюджета с допуском 5%",
                actual=totals["mean_rub"],
                expected=budget_ceiling(limits.budget_rub),
                requirement_source="limits.budget_rub",
                repair_scope="meals",
            )
        )
    return {
        "totals": totals,
        "daily": {
            d: {k: round(v, 2) if math.isfinite(v) else None for k, v in row.items()}
            for d, row in daily.items()
        },
    }, issues
