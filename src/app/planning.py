import math

from pydantic import JsonValue, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import RunUsage

from app.catalog import Catalog
from app.energy import energy_reference
from app.llm import Generator
from app.models import (
    Issue,
    PersonContext,
    PlanDraft,
    PlanLimits,
    PlanRevision,
    Recipe,
    Result,
    budget_ceiling,
)
from app.progress import Progress
from app.storage import Store
from app.tracing import stage


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


class Planner:
    """Создание меню из каталога и независимое редактирование полной версии."""

    def __init__(self, store: Store, catalog: Catalog, generator: Generator) -> None:
        self.store, self.catalog, self.generator = store, catalog, generator

    async def run(
        self,
        user_id: str,
        operation_id: str,
        request: str,
        days: int,
        limits: PlanLimits,
        context: PersonContext,
        usage: RunUsage,
        progress: Progress | None = None,
    ) -> Result:
        """Создаёт новую адресуемую версию, сохраняя предыдущую.

        :param user_id: Владелец
        :param operation_id: Ключ доставки
        :param request: Полный запрос с действующими условиями
        :param days: Число относительных дней
        :param limits: Обязательные явные пределы
        :param context: Персональный контекст
        :param progress: Этапы для интерфейса текущего вызова
        :param usage: Общий счётчик вызовов
        :return: План, черновик либо техническая ошибка
        """
        previous = await self.store.replay(user_id, operation_id)
        if previous:
            return previous
        progress = progress or Progress()
        await progress.report("plan_prepare")
        with stage("plan.catalog"):
            recipes = await self.catalog.all()
        if not recipes:
            return Result.failed("EMPTY_CATALOG", "Подготовленный каталог пуст; требуется импорт рецептов")
        products = await self.catalog.products()
        index = {f"r{i}": r for i, r in enumerate(sorted(recipes, key=lambda r: r.recipe_id), 1)}
        payload = {
            "operation": "generate",
            "request": request,
            "days": days,
            "limits": limits.model_dump(mode="json"),
            "context": context.model_dump(mode="json")
            | {"energy_reference": energy_reference(context.questionnaire)},
            "catalog": [r.planning_card(products) | {"recipe_id": alias} for alias, r in index.items()],
        }
        for attempt in range(2):
            await progress.report("plan_generate" if attempt == 0 else "plan_correct")
            try:
                candidate = await self.generator.generate("meal_plan", PlanDraft, payload, usage, "low")
            except (UnexpectedModelBehavior, ValidationError) as exc:
                if attempt:
                    return Result.failed("MODEL_FORMAT", "Модель дважды не вернула результат по схеме")
                payload["feedback"] = {
                    "kind": "format",
                    "message": str(exc)[:2000],
                    "numeric_error_code": "INVALID_NUMBER",
                    "instruction": "Исправь формат по схеме; числа должны быть конечными",
                }
                payload["operation"] = "repair"
                continue
            await progress.report("plan_check")
            with stage("plan.validate"):
                metrics, issues = evaluate(candidate, index, limits)
            if not issues or attempt:
                candidate = candidate.model_copy(
                    update={
                        "meals": [
                            meal.model_copy(update={"recipe_id": index[meal.recipe_id].recipe_id})
                            if meal.recipe_id in index
                            else meal
                            for meal in candidate.meals
                        ]
                    }
                )
                result = Result(status="needs_revision" if issues else "ok", issues=issues)
                await progress.report("plan_save")
                return await self.store.save_object(
                    user_id,
                    "plan",
                    candidate.model_dump(mode="json")
                    | metrics
                    | {"days": days, "limits": limits.model_dump(mode="json"), "request": request},
                    result,
                    operation_id,
                )
            payload["candidate"] = candidate.model_dump(mode="json")
            payload["feedback"] = [i.model_dump(mode="json") for i in issues]
            payload["operation"] = "repair"
        raise AssertionError("Unreachable")

    async def edit(
        self, user_id: str, operation_id: str, plan_id: str, request: str, usage: RunUsage
    ) -> Result:
        """Редактирует полную версию без каталога и персонального контекста.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param plan_id: Исходная версия
        :param request: Полное поручение с выбранными ID и условиями
        :param usage: Счётчики всего хода
        :return: Новая версия, черновик либо техническая ошибка
        """
        previous = await self.store.replay(user_id, operation_id)
        if previous:
            return previous
        source = await self.store.get_object(user_id, "plan", plan_id)
        if source.status == "error":
            return source
        payload = {"current_plan": source.data, "request": request}
        fixed_limits = None
        for attempt in range(2):
            try:
                candidate = await self.generator.generate("edit_plan", PlanRevision, payload, usage, "low")
            except (UnexpectedModelBehavior, ValidationError) as exc:
                if attempt:
                    return Result.failed("MODEL_FORMAT", "Редактор дважды не вернул результат по схеме")
                payload["feedback"] = {"kind": "format", "message": str(exc)[:2000]}
                continue
            if fixed_limits is None:
                fixed_limits = candidate.limits.model_copy(deep=True)
            candidate.limits = fixed_limits.model_copy(deep=True)
            recipes = await self.catalog.recipes_by_ids([m.recipe_id for m in candidate.meals])
            metrics, issues = evaluate(candidate, recipes, fixed_limits)
            if not issues or attempt:
                return await self.store.save_object(
                    user_id,
                    "plan",
                    candidate.model_dump(mode="json") | metrics | {"request": request},
                    Result(status="needs_revision" if issues else "ok", issues=issues),
                    operation_id,
                    plan_id,
                )
            payload["candidate"] = candidate.model_dump(mode="json")
            payload["feedback"] = [issue.model_dump(mode="json") for issue in issues]
            payload["fixed_limits"] = fixed_limits.model_dump(mode="json")
        raise AssertionError("Unreachable")
