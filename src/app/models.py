import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, field_validator

Difficulty = Literal["easy", "intermediate", "advanced"]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
IssueCode = Literal["RECIPE_NOT_FOUND", "INVALID_NUMBER", "DIFFICULTY_EXCEEDED", "BUDGET_EXCEEDED"]


class Record(BaseModel):
    """Базовая сериализуемая модель; неизвестные поля не принимаются."""

    model_config = ConfigDict(extra="forbid")


class PlanLimits(Record):
    max_difficulty: Difficulty | None = Field(description="Конечный допустимый уровень; null — без предела")
    budget_rub: Number | None = Field(
        description="Бюджет всего плана из запроса без прибавки погрешности; null — без предела"
    )


class CartLimits(Record):
    budget_rub: Number | None = Field(
        description="Бюджет всей покупки из запроса без прибавки погрешности; null — без предела"
    )


class Nutrition(Record):
    kcal: Number = Field(description="Килокалории")
    protein_g: Number = Field(description="Белки, г")
    fat_g: Number = Field(description="Жиры, г")
    carbs_g: Number = Field(description="Углеводы, г")


class Cost(Record):
    mean_rub: Number = Field(description="Средняя оценка цены, рубли")
    min_rub: Number = Field(description="Нижняя оценка цены, рубли")
    max_rub: Number = Field(description="Верхняя оценка цены, рубли")


class NormalizedProduct(Record):
    """Общий продукт; показатели относятся к base_amount в указанной единице."""

    product_id: str
    name: str
    unit: Literal["g", "ml", "pcs"]
    base_amount: Annotated[Number, Field(gt=0)]
    nutrition: Nutrition | None
    cost: Cost | None
    restrictions: str


class Ingredient(Record):
    product_id: str
    amount: Number


class Recipe(Record):
    recipe_id: str
    title: str
    ingredients: list[Ingredient]
    meal_roles: list[str]
    difficulty: Difficulty
    active_minutes: Number
    total_minutes: Number
    equipment: list[str]
    base_serving: str
    nutrition: Nutrition
    cost: Cost
    restrictions: str
    steps: list[str]
    source_url: str
    image_url: str | None

    @field_validator("nutrition")
    @classmethod
    def round_nutrition(cls, value: Nutrition) -> Nutrition:
        """Округляет показатели базовой порции до целых.

        :param value: КБЖУ рецепта, не исходные маркировки продуктов
        :return: КБЖУ с округлением половин вверх
        """
        return Nutrition(
            **{
                k: int(Decimal(str(v)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                for k, v in value.model_dump().items()
            }
        )

    @field_validator("cost")
    @classmethod
    def round_cost(cls, value: Cost) -> Cost:
        """Округляет стоимость порции до копеек.

        :param value: Средняя оценка и диапазон цены
        :return: Значения с двумя десятичными разрядами
        """
        return Cost(
            **{
                k: float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                for k, v in value.model_dump().items()
            }
        )

    @field_serializer("nutrition")
    def serialize_nutrition(self, value: Nutrition) -> dict[str, int]:
        """Сохраняет целые показатели без дробного суффикса.

        :param value: Округлённые КБЖУ
        :return: Целочисленные значения для JSON и БД
        """
        return {k: int(v) for k, v in value.model_dump().items()}

    def card(self, products: dict[str, NormalizedProduct]) -> dict[str, JsonValue]:
        """Создаёт проекцию без шагов и количеств, сохраняя весь состав.

        :param products: Справочник для раскрытия названий ингредиентов
        :return: Компактная карточка того же рецепта
        """
        data = self.model_dump(mode="json", exclude={"steps", "source_url"})
        data["ingredients"] = [
            {"product_id": i.product_id, "name": products[i.product_id].name} for i in self.ingredients
        ]
        return data

    def planning_card(self, products: dict[str, NormalizedProduct]) -> dict[str, JsonValue]:
        """Готовит внутреннюю проекцию выбора, сохраняя весь состав.

        :param products: Справочник полных названий ингредиентов
        :return: Карточка без технических ID продуктов, фото и служебных примечаний
        """
        data = self.card(products)
        data.pop("image_url")
        data["ingredients"] = list(dict.fromkeys(products[i.product_id].name for i in self.ingredients))
        notes = (
            r"Авторская адаптация: состав, граммовки и порции заданы редактором\.",
            r"КБЖУ по средним маркировкам категорий, без учёта потерь и очистки\.",
            r"Цена снимка \d{2}\.\d{2}\.\d{4}, без округления упаковок\.",
            r"Время — ориентир исходника, не замер адаптации; фото исходника\.",
            r"Аллергены проверяйте у выбранных SKU\.",
            r"Шаги даны на \d+ порций; состав и показатели карточки — на одну\.",
            r"Масса яиц указана без скорлупы\.",
            r"КБЖУ рассчитаны по точным количествам оставшихся ингредиентов и средним показателям "
            r"категорий ВкусВилла, без поправки на потери при приготовлении\.",
            r"Стоимость рассчитана по ценам снимка ВкусВилла от \d{2}\.\d{2}\.\d{4}, "
            r"без округления до полных упаковок\.",
            r"Адаптация: исключены необязательные компоненты — [^.]+\.",
            r"КБЖУ и цена пересчитаны для этого состава; ссылка ведёт на исходный рецепт\.",
            r"Показатели обновлены по полностью пересобранному справочнику после проверки всех SKU\.",
        )
        restrictions = self.restrictions
        for note in notes:
            restrictions = re.sub(note, "", restrictions)
        data["restrictions"] = " ".join(restrictions.split())
        for key in ("active_minutes", "total_minutes"):
            if data[key].is_integer():
                data[key] = int(data[key])
        return data


class Meal(Record):
    day: Annotated[int, Field(strict=True)] = Field(description="Номер относительного дня")
    meal: str = Field(description="Название приёма пищи")
    recipe_id: str = Field(description="ID выбранного рецепта")
    servings: Number = Field(description="Количество базовых порций рецепта; должно быть больше нуля")


class PlanDraft(Record):
    meals: list[Meal]
    explanation: str = Field(description="Короткое пояснение решений и нерешённых требований")


class PlanRevision(PlanDraft):
    """Полная содержательная версия меню после редактирования."""

    days: Annotated[int, Field(strict=True)]
    limits: PlanLimits


class Issue(Record):
    code: IssueCode = Field(description="Тип замечания из четырёх согласованных проверок")
    path: str = Field(description="Поле результата, к которому относится замечание")
    message: str = Field(description="Понятное объяснение проблемы")
    actual: JsonValue = Field(description="Полученное значение")
    expected: JsonValue = Field(description="Ожидаемое значение или допустимая граница")
    requirement_source: str = Field(description="Источник проверяемого условия")
    repair_scope: str = Field(description="Какие поля требуется исправить")


class Failure(Record):
    code: str = Field(description="Технический код причины")
    message: str = Field(description="Понятное описание причины ошибки")


class Result(Record):
    status: Literal["ok", "needs_revision", "partial", "error"] = Field(
        default="ok", description="Итог: выполнено, черновик, частичный результат или техническая ошибка"
    )
    data: JsonValue = None
    issues: list[Issue] = Field(
        default_factory=list, description="Замечания числового формата, ID рецептов, сложности или бюджета"
    )
    error: Failure | None = Field(default=None, description="Техническая ошибка; null, если её нет")

    @classmethod
    def failed(cls, code: str, message: str) -> "Result":
        """Формирует техническую ошибку, не критерий качества плана.

        :param code: Технический код
        :param message: Понятное описание
        :return: Результат без выдуманных данных
        """
        return cls(status="error", error=Failure(code=code, message=message))


class RecipeAmount(Record):
    recipe_id: str = Field(description="ID существующего рецепта")
    servings: Number = Field(gt=0, description="Количество базовых порций рецепта")


class PlanSource(Record):
    type: Literal["plan"] = Field(description="Покупка ингредиентов сохранённого плана")
    plan_id: str = Field(description="ID доступной пользователю версии плана")


class RecipesSource(Record):
    type: Literal["recipes"] = Field(description="Покупка ингредиентов перечисленных рецептов")
    items: list[RecipeAmount] = Field(min_length=1, description="Рецепты и количество порций каждого")


class SkuAmount(Record):
    """Явно выбранный товар; количество задаётся целыми упаковками."""

    sku_id: str = Field(min_length=1, description="Конкретный SKU магазина, не product_id ингредиента")
    packages: Annotated[int, Field(strict=True, gt=0)] = Field(
        default=1, description="Количество упаковок; если не указано, одна упаковка"
    )


class SkusSource(Record):
    type: Literal["skus"] = Field(description="Готовый выбор SKU для прямого создания ссылки")
    items: list[SkuAmount] = Field(min_length=1, description="Выбранные SKU и количества упаковок")


class StoredSkusSource(SkusSource):
    """Сохранённая независимая покупка допускает удаление последней позиции."""

    items: list[SkuAmount] = Field(
        description="Сохранённые SKU и упаковки; пусто после удаления всех позиций"
    )


class TextSource(Record):
    """Только чтение происхождения ранее сохранённых корзин; новый вызов не принимает text."""

    type: Literal["text"] = Field(description="Прежний текстовый источник сохранённой корзины")
    text: str = Field(description="Исходный текст ранее созданной корзины")


CartSource = Annotated[PlanSource | RecipesSource | SkusSource, Field(discriminator="type")]
StoredCartSource = Annotated[
    PlanSource | RecipesSource | StoredSkusSource | TextSource, Field(discriminator="type")
]


class CartProduct(Record):
    """Количество продукта справочника в его единице; отдельного ID строки нет."""

    product_id: str = Field(description="ID нормализованного продукта справочника")
    amount: Number | None = Field(description="Количество в единице продукта; null — не установлено")
    recipe_ids: list[str] = Field(
        description="Рецепты, которым нужен этот продукт; пусто для отдельной покупки"
    )
    requested_sku_id: str | None = Field(default=None, description="Выбранный SKU или null, если не выбран")


class UnresolvedProduct(Record):
    product_id: str | None = Field(description="ID ингредиента; null, если его нет в справочнике")
    recipe_ids: list[str] = Field(description="Рецепты, которым не хватает продукта")
    reason: str = Field(description="Понятная причина, почему позиция не подобрана")


class CartRevision(Record):
    """Полный состав покупки; выбранный SKU записывается в requested_sku_id."""

    products: list[CartProduct]
    unresolved: list[UnresolvedProduct]
    limits: CartLimits


class SkuCartRevision(Record):
    """Полная содержательная версия корзины с прямым выбором SKU."""

    items: list[SkuAmount] = Field(description="Все SKU новой версии, включая неизменённые, в упаковках")
    limits: CartLimits = Field(description="Бюджет исходной корзины с явно запрошенными изменениями")
    unresolved: list[UnresolvedProduct] | None = Field(
        default=None,
        description="Все оставшиеся нерешённые позиции; null сохраняет прежние, [] удаляет все",
    )


class Product(Record):
    sku_id: str = Field(description="Идентификатор конкретного товара магазина")
    title: str = Field(description="Название товара из интеграции")
    price_rub: Number = Field(description="Цена одной упаковки в рублях")
    package_amount: Number | None = Field(
        default=None, description="Количество в упаковке; null — неизвестно"
    )
    package_unit: str | None = Field(default=None, description="Единица фасовки: g, ml или pcs")
    source_url: str | None = Field(default=None, description="Ссылка на товар при наличии")
    composition: str | None = Field(default=None, description="Состав, возвращённый магазином, если доступен")
    package_basis: str | None = Field(default=None, description="Источник данных о фасовке")


class Choice(Record):
    product_id: str
    sku_id: str | None
    reason: str


class Selection(Record):
    choices: list[Choice]


class PersonContext(Record):
    questionnaire: dict[str, JsonValue] = Field(default_factory=dict)
    memory: str = ""
    memory_revision: int = 0
    dialogue: str = ""


def budget_ceiling(budget_rub: float) -> float:
    """Применяет фиксированный допуск 5% к верхнему бюджету.

    :param budget_rub: Сумма пользователя без прибавки
    :return: Допустимая сумма с округлением до копеек
    """
    return float(
        (Decimal(str(budget_rub)) * Decimal("1.05")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
