from typing import Annotated

from pydantic import Field, JsonValue

from app.models import Meal, Number, PlanDraft, PlanLimits, PlanRevision, Record


class RepeatedMeal(Record):
    """Одна неизменная позиция меню для одного или нескольких дней."""

    days: list[Annotated[int, Field(strict=True)]] = Field(
        min_length=1, description="Относительные дни с одинаковыми рецептом, приёмом пищи и порцией"
    )
    meal: str
    recipe_id: str
    servings: Number


class RepeatedPlan(Record):
    """Компактный ответ генератора; публичный план по-прежнему содержит отдельные дни."""

    meals: list[RepeatedMeal]
    explanation: str

    def expand(self) -> PlanDraft:
        """Разворачивает повторения до обычных проверок и вычислений.

        :return: Полный план без изменения ID, названий и порций
        """
        meals = [
            Meal(day=day, meal=row.meal, recipe_id=row.recipe_id, servings=row.servings)
            for row in self.meals
            for day in row.days
        ]
        return PlanDraft(meals=sorted(meals, key=lambda row: row.day), explanation=self.explanation)


class RepeatedRevision(RepeatedPlan):
    """Компактная полная редакция меню с действующими условиями."""

    days: Annotated[int, Field(strict=True)]
    limits: PlanLimits

    def expand(self) -> PlanRevision:
        """Разворачивает записи, сохраняя продолжительность и ограничения.

        :return: Обычная полная редакция для расчётов и сохранения
        """
        plan = super().expand()
        return PlanRevision(
            meals=plan.meals, explanation=plan.explanation, days=self.days, limits=self.limits
        )


def planning_input(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Сжимает повторяющиеся ключи каталога без потери данных.

    Стабильная таблица предшествует запросу и анкете, чтобы провайдер мог
    переиспользовать префикс и между разными пользователями.

    :param payload: Исходные карточки и условия генерации либо коррекции
    :return: Полный каталог в табличной форме и неизменённые условия
    """
    cards = payload.get("catalog")
    if not cards:
        return payload
    columns = ["recipe_id", "title", "nutrition", "cost"] + [
        field for field in cards[0] if field not in ("recipe_id", "title", "nutrition", "cost", "difficulty")
    ]
    nested = {field: list(cards[0][field]) for field in ("nutrition", "cost")}
    groups: dict[str, list[JsonValue]] = {}
    for card in cards:
        groups.setdefault(card["difficulty"], []).append(
            [
                [card[field][key] for key in nested[field]] if field in nested else card[field]
                for field in columns
            ]
        )
    return {
        "catalog": {"columns": columns, "nested_columns": nested, "by_difficulty": groups},
        **{key: value for key, value in payload.items() if key != "catalog"},
    }
