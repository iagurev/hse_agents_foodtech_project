from typing import Literal

from pydantic import Field

from app.models import Difficulty, Record


class SearchConstraints(Record):
    """Явные условия поиска; пустые списки не объявляют рецепт безопасным."""

    exclude_products: list[str] = Field(
        default_factory=list, max_length=30, description="Исключённые продукты: названия или product_id"
    )
    forbidden_equipment: list[str] = Field(
        default_factory=list, max_length=20, description="Недоступная техника: духовка, тостер и т.п."
    )
    max_total_minutes: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="Явный предел общего времени"
    )
    max_difficulty: Difficulty | None = Field(default=None, description="Предельная сложность")
    meal_roles: list[Literal["breakfast", "lunch", "dinner", "snack", "dessert"]] = Field(
        default_factory=list, description="Явно запрошенные роли; пусто — без фильтра"
    )
    diet: Literal["vegetarian", "vegan"] | None = Field(
        default=None, description="Явно выбранный тип питания; не медицинская гарантия"
    )
    allergens: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Аллергены, отсутствие которых требуется подтвердить; неизвестное не подходит",
    )


class SearchDiagnostic(Record):
    """Диагностика поиска, независимая от замечаний проверки меню."""

    code: Literal[
        "UNRESOLVED_CONSTRAINT",
        "INSUFFICIENT_EVIDENCE",
        "RETRIEVAL_DEGRADED",
        "CONFLICTING_CONSTRAINTS",
        "NO_RELEVANT_MATCH",
        "CANDIDATE_POOL_EXPANDED",
    ] = Field(description="Причина ограничения либо деградации поиска; отдельна от кодов проверки меню")
    message: str = Field(description="Короткое пояснение ограничения выдачи")


class Selection(Record):
    """Существующие ID и доказуемые ограничения результата."""

    recipe_ids: list[str] = Field(default_factory=list)
    diagnostics: list[SearchDiagnostic] = Field(default_factory=list)
    index_id: str = ""
