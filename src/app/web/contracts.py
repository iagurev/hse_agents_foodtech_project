from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.models import Difficulty, Number, Record

Budget = Literal["15-20", "20-25", "25-30", "30-35", "35-40", "40-50", "unlimited"]


class Profile(Record):
    """Явно заполненная анкета; отсутствие необязательного выбора не создаёт лимит."""

    goal: Literal["lose_weight", "gain_mass", "healthier", "maintain", "ideas"]
    sex: Literal["female", "male"]
    age: Annotated[int, Field(strict=True, ge=14, le=120)]
    height_cm: Annotated[Number, Field(ge=150, le=230)]
    weight_kg: Annotated[Number, Field(ge=50, le=200)]
    activity: Literal["inactive", "low_active", "active", "very_active"] | None = None
    monthly_budget: Budget | None = None
    cooking_difficulty: Difficulty | None = None
    restrictions: str = ""


class Credentials(Record):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class Registration(Credentials):
    profile: Profile


class MemoryUpdate(Record):
    text: str
    revision: int = Field(ge=0)


class ChatUpdate(Record):
    title: str = Field(min_length=1, max_length=120)


class ObjectTitle(Record):
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> object:
        """Нормализует пробелы перед проверкой длины названия."""
        return " ".join(value.split()) if isinstance(value, str) else value


class ObjectReference(Record):
    """Объект поручения; название и доступ проверяет сервер, ID не включается в текст UI."""

    kind: Literal["plan", "cart", "recipe"]
    id: str = Field(min_length=1, max_length=128)
    servings: Annotated[Number, Field(gt=0)] | None = None


class TurnRequest(Record):
    turn_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    references: list[ObjectReference] = Field(default_factory=list, max_length=50)


class ServingEdit(Record):
    index: int = Field(ge=0)
    servings: Annotated[Number, Field(gt=0)]


class PlanEdit(Record):
    operation_id: str = Field(min_length=1, max_length=128)
    meals: list[ServingEdit] = Field(description="Все сохраняемые строки исходной версии; отсутствие удаляет")


class PackageEdit(Record):
    sku_id: str
    packages: Number = Field(gt=0, multiple_of=0.01)


class CartEdit(Record):
    operation_id: str = Field(min_length=1, max_length=128)
    items: list[PackageEdit]
    unresolved_indices: list[int] = Field(description="Индексы сохраняемых нерешённых строк")


class PurchasedUpdate(Record):
    sku_id: str
    purchased: bool
