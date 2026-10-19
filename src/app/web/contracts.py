from typing import Annotated, Literal

from pydantic import Field

from app.models import Difficulty, Number, Record

Budget = Literal["15-20", "20-25", "25-30", "30-35", "35-40", "40-50", "unlimited"]


class Profile(Record):
    """Явно заполненная анкета; отсутствие необязательного выбора не создаёт лимит."""

    goal: Literal["lose_weight", "gain_mass", "healthier", "maintain", "ideas"]
    sex: Literal["female", "male"]
    age: Annotated[int, Field(strict=True, ge=14, le=120)]
    height_cm: Annotated[Number, Field(ge=150, le=230)]
    weight_kg: Annotated[Number, Field(ge=50, le=200)]
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


class TurnRequest(Record):
    turn_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)


class ServingEdit(Record):
    index: int = Field(ge=0)
    servings: Annotated[Number, Field(gt=0)]


class PlanEdit(Record):
    operation_id: str = Field(min_length=1, max_length=128)
    meals: list[ServingEdit] = Field(description="Все сохраняемые строки исходной версии; отсутствие удаляет")


class PackageEdit(Record):
    sku_id: str
    packages: Annotated[int, Field(strict=True, gt=0)]


class CartEdit(Record):
    operation_id: str = Field(min_length=1, max_length=128)
    items: list[PackageEdit]
    unresolved_indices: list[int] = Field(description="Индексы сохраняемых нерешённых строк")


class PurchasedUpdate(Record):
    sku_id: str
    purchased: bool
