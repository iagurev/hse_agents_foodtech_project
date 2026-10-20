from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import cleandoc
from typing import ClassVar, Literal, Self, get_type_hints

from opentelemetry.trace import get_current_span
from pydantic import Field
from pydantic_ai import RunContext, Tool
from pydantic_ai.toolsets import FunctionToolset

from app.assistant.context import Context
from app.integrations import IntegrationError
from app.models import (
    CartLimits,
    CartProduct,
    CartSource,
    Cost,
    Difficulty,
    Meal,
    Number,
    Nutrition,
    PlanLimits,
    Product,
    RecipeSearchLimits,
    Record,
    Result,
    StoredCartSource,
    UnitConversion,
    UnresolvedProduct,
)
from app.tracing import outcome

# Общие основы контрактов. Description — метаданные, не аргумент инструмента.


class ToolInput(Record):
    description: ClassVar[str]


class ToolResult[Data](Result):
    """Публичный результат с проверяемым содержимым; хранилище остаётся универсальным."""

    data: Data | None = Field(
        default=None, description="Типизированный результат операции; null при ошибке без данных"
    )

    @classmethod
    def from_result(cls, result: Result) -> Self:
        """Проверяет результат сервиса перед передачей агенту.

        :param result: Результат хранилища или операции
        :return: Результат по публичной схеме
        """
        return cls.model_validate(result.model_dump())


# Общие структуры плана


class PlanSummary(Record):
    plan_id: str = Field(description="ID доступной версии плана или черновика")
    parent_id: str | None = Field(description="ID предыдущей версии, null для первого создания")
    status: Literal["ok", "needs_revision", "partial", "error"] = Field(
        description="Статус сохранённой версии"
    )


class PlanTotals(Record):
    kcal: Number | None = Field(description="Рассчитанные килокалории; null при переполнении")
    protein_g: Number | None = Field(description="Белки в граммах; null при переполнении")
    fat_g: Number | None = Field(description="Жиры в граммах; null при переполнении")
    carbs_g: Number | None = Field(description="Углеводы в граммах; null при переполнении")
    mean_rub: Number | None = Field(description="Средняя оценка цены ингредиентов; null при переполнении")
    min_rub: Number | None = Field(description="Нижняя оценка цены; null при переполнении")
    max_rub: Number | None = Field(description="Верхняя оценка цены; null при переполнении")


class PlanView(PlanSummary):
    meals: list[Meal] = Field(description="Полное меню сохранённой версии")
    days: int = Field(description="Количество относительных дней плана")
    limits: PlanLimits = Field(description="Ограничения этой версии, бюджет без прибавки допуска")
    explanation: str = Field(description="Короткое пояснение результата и нерешённых требований")
    request: str = Field(description="Поручение, по которому создана эта версия")
    totals: PlanTotals = Field(description="КБЖУ и стоимость всего меню; не персональная норма")
    daily: dict[str, PlanTotals] = Field(description="Те же рассчитанные показатели по номерам дней")


# --- list_plans ---


class ListPlansInput(ToolInput):
    description: ClassVar[str] = """
        Показывает метаданные нескрытых планов и черновиков пользователя. Используй для выбора ID;
        содержимое получает get_plan. Аргументов и пагинации нет. Ничего не изменяет.
    """

    # Публичный вызов: {}. Полей нет.


class PlanListResult(ToolResult[list[PlanSummary]]):
    data: list[PlanSummary] | None = Field(
        default=None,
        description="Метаданные нескрытых планов и черновиков пользователя; пустой список, если их нет",
    )


async def list_plans(ctx: RunContext[Context], args: ListPlansInput) -> PlanListResult:
    return PlanListResult.from_result(await ctx.deps.store.list_objects(ctx.deps.user_id, "plan"))


# --- get_plan ---


class PlanId(ToolInput):
    description: ClassVar[str] = """
        Получает сохранённое меню, порции, итоги, limits и замечания. Не ищет инструкции рецептов
        и не изменяет объект. Для полной рецептуры используй get_recipes.
        В data поля totals и daily содержат рассчитанные КБЖУ и оценку цены всего меню и отдельных дней,
        даже если пользователь не задавал целевые КБЖУ. needs_revision означает черновик: issues содержит
        замечания проверки, explanation — пояснение составленного меню или причин, почему оно не получилось.
        NOT_FOUND означает, что указанная версия недоступна текущему пользователю.
    """

    plan_id: str = Field(description="ID доступной версии плана или черновика")


class PlanResult(ToolResult[PlanView]):
    data: PlanView | None = Field(
        default=None,
        description="Запрошенная версия плана с полным меню и рассчитанными показателями; null, если недоступна",
    )


async def get_plan(ctx: RunContext[Context], args: PlanId) -> PlanResult:
    return PlanResult.from_result(await ctx.deps.store.get_object(ctx.deps.user_id, "plan", args.plan_id))


# --- generate_plan ---


class GeneratePlanInput(ToolInput):
    description: ClassVar[str] = """
        Создаёт и сохраняет меню по полному подготовленному каталогу. Не требует предварительного
        search_recipes и не подбирает SKU. Передай условия диалога в request, пределы явно в limits.
        Корзина не создаётся. Включает максимум одну коррекцию, в том числе при ошибке формата.

        В data находятся plan_id, выбранные рецепты и порции, explanation, totals и daily.
        В meals находятся recipe_id, а не названия блюд. Пользователь видит меню в карточке плана.
        На обычную просьбу составить меню ответь коротким подтверждением и существенными замечаниями;
        повторно открывать рецепты и пересказывать всю карточку для этого не требуется.
        Не заменяй карточку таблицей внутренних ID: они нужны для вызовов tools, а не для чтения меню.
        Если пользователь просит подробный пересказ или обсуждает конкретные блюда, получи их через get_recipes:
        нельзя восстанавливать названия, состав или способ готовки по ID и исходному запросу.
        Totals и daily — рассчитанные КБЖУ и оценка цены ингредиентов, независимо от наличия числовых целей.
        Статус ok означает сохранение и прохождение четырёх проверок: ID рецептов, числового формата,
        сложности и бюджета; отдельной проверки всех смысловых пожеланий нет.
        needs_revision возвращает сохранённый черновик. В issues указаны конкретные замечания проверок
        с message, actual и expected; explanation объясняет результат, в том числе отсутствие подходящего меню.
        При needs_revision верни доступный результат и кратко передай указанные причины: подбор завершён.
        Не продолжай составлять тот же план через поиск рецептов и не перезапускай генерацию автоматически.
        Если пользователь запретил менять условия, не предлагай их ослабить.
        Статус error содержит причину в error.message:
        EMPTY_CATALOG — база пуста, MODEL_FORMAT — ответ не удалось разобрать после коррекции.
    """

    request: str = Field(description="Задача, пожелания и существенные временные условия текущего диалога")

    days: int = Field(ge=1, description="Число относительных дней, без даты начала и количества людей")

    limits: PlanLimits = Field(
        description="Явные пределы по анкете и диалогу; null в поле снимает предел. Бюджет без прибавки 5%"
    )


class GeneratePlanResult(ToolResult[PlanView]):
    data: PlanView | None = Field(
        default=None,
        description="Созданный план или адресуемый черновик с меню и итогами; null при технической ошибке",
    )


async def generate_plan(ctx: RunContext[Context], args: GeneratePlanInput) -> GeneratePlanResult:
    d = ctx.deps
    return GeneratePlanResult.from_result(
        await d.planner.run(
            d.user_id,
            d.operation_id(ctx.tool_call_id),
            args.request,
            args.days,
            args.limits,
            d.person,
            ctx.usage,
            progress=d.progress("generate_plan", ctx.tool_call_id),
        )
    )


# --- edit_plan ---


class EditPlanInput(ToolInput):
    description: ClassVar[str] = """
        Изменяет существующий план по самодостаточному текстовому поручению и сохраняет полную новую
        версию. Сначала выбери нужные новые рецепты через поиск или известные ID. Укажи в request,
        какие дни и приёмы пищи изменить, выбранные recipe_id, порции и существенные условия.
        Раскрой отсылки вроде «второй вариант»: редактор не получает историю диалога или полный каталог.
        Не требуется передавать структуру меню: инструмент сам загружает текущий план.

        Ограничения сохраняются из плана. Их меняют только явные указания в request; не прибавляй
        погрешность к бюджету и не согласовывай её — фиксированный допуск 5% учитывает код.
        Предыдущая версия остаётся доступной, корзина не меняется. Максимум одна внутренняя коррекция.
        В data возвращаются новый plan_id, parent_id, полное меню, limits, explanation, totals и daily.
        ok означает прохождение проверок ID рецептов, числового формата, сложности и бюджета.
        needs_revision — сохранённый черновик с конкретными issues; сообщи результат и причины,
        не повторяя ту же безрезультатную правку автоматически. error.message содержит техническую причину:
        NOT_FOUND — версия недоступна; MODEL_FORMAT — редактор не вернул результат по схеме.
    """

    plan_id: str = Field(description="ID доступной версии плана или черновика")

    request: str = Field(
        description=(
            "Самодостаточная правка: дни, приёмы пищи, выбранные recipe_id, порции и условия. "
            "Раскрой отсылки к диалогу; явно укажи изменение бюджета или сложности, если оно требуется"
        ),
    )


class EditPlanResult(ToolResult[PlanView]):
    data: PlanView | None = Field(
        default=None,
        description="Полная новая версия плана или черновик; parent_id указывает исходную версию",
    )


async def edit_plan(ctx: RunContext[Context], args: EditPlanInput) -> EditPlanResult:
    d = ctx.deps
    return EditPlanResult.from_result(
        await d.planner.edit(
            d.user_id, d.operation_id(ctx.tool_call_id), args.plan_id, args.request, ctx.usage
        )
    )


# Общие структуры рецептов


class IngredientName(Record):
    product_id: str = Field(description="ID нормализованного ингредиента")
    name: str = Field(description="Название ингредиента из справочника")


class IngredientDetails(IngredientName):
    amount: Number = Field(description="Количество на базовую порцию рецепта")
    unit: Literal["g", "ml", "pcs"] = Field(description="Единица количества ингредиента")


class RecipeCard(Record):
    recipe_id: str = Field(description="ID рецепта для получения деталей и операций с меню")
    title: str = Field(description="Понятное название рецепта")
    ingredients: list[IngredientName] = Field(description="Полный состав по нормализованным продуктам")
    meal_roles: list[str] = Field(description="Подходящие приёмы пищи")
    difficulty: Difficulty = Field(description="Единая оценка сложности приготовления")
    active_minutes: Number = Field(description="Время активной готовки в минутах")
    total_minutes: Number = Field(description="Полное время, включая ожидание, в минутах")
    equipment: list[str] = Field(description="Необходимое оборудование")
    base_serving: str = Field(description="Что означает одна базовая порция")
    nutrition: Nutrition = Field(description="КБЖУ одной базовой порции")
    cost: Cost = Field(description="Оценка стоимости ингредиентов одной базовой порции")
    restrictions: str = Field(description="Особенности состава и ограничения, для которых рецепт не подходит")
    image_url: str | None = Field(description="Фотография рецепта, если известна")


class RecipeDetails(RecipeCard):
    ingredients: list[IngredientDetails] = Field(
        description="Состав с количеством на базовую порцию и единицами"
    )
    steps: list[str] = Field(description="Последовательность приготовления")
    source_url: str = Field(description="Ссылка на первоисточник рецепта")


class RecipeSearchData(Record):
    recipes: list[RecipeCard] = Field(description="До 10 найденных карточек; пусто, если совпадений нет")


class RecipeDetailsData(Record):
    recipes: list[RecipeDetails] = Field(description="Полные найденные рецепты; неизвестные ID пропущены")


# --- search_recipes ---


class RecipeQuery(ToolInput):
    description: ClassVar[str] = """
        Ищет короткие карточки по одному конкретному блюду, названию или сочетанию продуктов
        в подготовленной базе приложения.
        В query укажи, что ищем; условия передай отдельно в limits. Например, для «омлета
        с грибами без духовки» query — «омлет с грибами», forbidden_equipment — ["духовка"].
        Поиск не извлекает ограничения из текста и сам не читает анкету или память.
        Передавай только ограничения, заданные в запросе, анкете или памяти; остальные поля — null.
        Не добавляй ограничения по собственному предположению о блюде.
        Бюджет относится к одной базовой порции, а не всему плану: backend сравнивает среднюю
        оценку стоимости с допуском 5%, который не нужно прибавлять или уточнять у пользователя.
        Соответствие пожеланиям пользователя дополнительно оцени по карточкам;
        поиск не подтверждает аллергенную безопасность. Возвращает до 10 карточек; размер выдачи фиксирован.
        Не создаёт меню, не загружает рецепты из интернета.
        Пустой список означает, что по запросу в базе ничего не нашлось; достаточно кратко сообщить об этом.
        Причину отсутствия записей результат не устанавливает. Близкие варианты предлагай
        по возвращённым карточкам, обозначив существенное отличие от запроса.
        Если рекомендуешь часть найденных блюд, открой выбранные recipe_id через get_recipes:
        тогда в чате появятся карточки именно твоей подборки, а не всех поисковых кандидатов.
    """

    query: str = Field(description="Блюдо, название или желаемые ингредиенты; ограничения передай в limits")

    limits: RecipeSearchLimits = Field(
        description="Явные условия поиска; все поля обязательны, null — без ограничения. Backend ничего не наследует"
    )


class RecipeSearchResult(ToolResult[RecipeSearchData]):
    data: RecipeSearchData | None = Field(
        default=None, description="Результат поиска с короткими карточками кандидатов"
    )


async def search_recipes(ctx: RunContext[Context], args: RecipeQuery) -> RecipeSearchResult:
    search = ctx.deps.retriever.search if ctx.deps.retriever is not None else ctx.deps.catalog.search
    return RecipeSearchResult.from_result(await search(args.query, args.limits))


# --- get_recipes ---


class RecipeIds(ToolInput):
    description: ClassVar[str] = """
        Получает состав с количествами, инструкции и источники нескольких рецептов одним вызовом.
        Открытые рецепты становятся итоговой подборкой карточек в текущем ответе. Передай все блюда,
        которые рекомендуешь пользователю; поисковые кандидаты в этом случае отдельно не показываются.
        Используй известные ID из карточек или плана. Неизвестные ID пропускаются; результат содержит только найденные рецепты.
    """

    recipe_ids: list[str] = Field(
        description="Один или несколько известных ID рецептов; неизвестные пропускаются"
    )


class RecipeDetailsResult(ToolResult[RecipeDetailsData]):
    data: RecipeDetailsData | None = Field(
        default=None, description="Найденные рецепты с количествами, инструкциями и первоисточниками"
    )


async def get_recipes(ctx: RunContext[Context], args: RecipeIds) -> RecipeDetailsResult:
    return RecipeDetailsResult.from_result(await ctx.deps.catalog.get(args.recipe_ids))


# Общие структуры корзины


class CartSummary(Record):
    cart_id: str = Field(description="ID доступной версии корзины или черновика")
    parent_id: str | None = Field(description="ID предыдущей версии, null для первого создания")
    status: Literal["ok", "needs_revision", "partial", "error"] = Field(
        description="Статус сохранённой версии"
    )


class CartItem(Record):
    product_id: str | None = Field(description="Нормализованный продукт; null для прямого выбора SKU")
    sku_id: str | None = Field(
        default=None, description="Явный SKU; у прежних записей находится в product.sku_id"
    )
    product: Product | None = Field(
        description="Достоверная карточка SKU; null, если прямой SKU отсутствует в кэше"
    )
    packages: Number = Field(description="Количество единиц продажи: упаковки либо кг/л для весового SKU")
    quantity_basis: UnitConversion | None = Field(
        default=None, description="Справочная оценка перевода единиц с источником; null при прямом расчёте"
    )


class CartLink(Record):
    sku_ids: list[str] = Field(description="SKU этой части покупки; не более 30 на магазинную ссылку")
    url: str | None = Field(description="Ссылка части; null при отказе. Заказ не оформлен")
    error: str | None = Field(description="Ошибка создания именно этой части; null при успехе")


class CartView(CartSummary):
    source: StoredCartSource = Field(description="Источник первоначального создания; при правках сохраняется")
    limits: CartLimits = Field(description="Бюджет этой версии без прибавки допуска")
    products: list[CartProduct] = Field(
        description="Агрегированные ингредиенты; пусто для прямого выбора SKU"
    )
    items: list[CartItem] = Field(description="Выбранные SKU и упаковки")
    unresolved: list[UnresolvedProduct] = Field(description="Ингредиенты без подходящего SKU и их назначение")
    subtotal_rub: Number | None = Field(
        description="Стоимость позиций с известной ценой; null, если нет цен или переполнение"
    )
    total_is_complete: bool = Field(
        description="Все позиции подобраны и их цена известна; не подтверждение наличия ссылки"
    )
    cart_url: str | None = Field(
        description="Единственная ссылка для прежних клиентов; null при нескольких частях или отказе"
    )
    cart_links: list[CartLink] = Field(
        default_factory=list,
        description="Части одной покупки с общим итогом: ссылки и ошибки по группам SKU; у старых версий пусто, используй cart_url",
    )
    link_error: str | None = Field(description="Причина отказа создания ссылки, если была попытка")


# --- create_cart ---


class CreateCartInput(ToolInput):
    description: ClassVar[str] = """
        Создаёт корзину из сохранённого плана, списка рецептов с порциями или готовых SKU.
        Для плана и рецептов сам получает ингредиенты, агрегирует их по product_id, ищет до 10 SKU
        на продукт, дополняет их точными связями каталога и выбирает товары одним модельным вызовом. Request добавляется как есть к каждому
        названию в поисковом запросе; отдельного разбора текста нет, состав и количества он не меняет.
        Для готовых SKU только объединяет упаковки и создаёт ссылку, без поиска и модели.
        Если пользователь назвал товары свободным текстом, сначала найди конкретные SKU через search_products,
        затем передай выбранные ID и упаковки в source.type=skus. Не передавай список покупок в request.
        Ссылка не оформляет заказ, исходный план не изменяется.

        Результат содержит одну сохранённую покупку с общим итогом и нерешённые ингредиенты с recipe_ids.
        При числе SKU больше 30 cart_links содержит несколько магазинных ссылок. Покажи все успешные
        части; ошибка одной не отменяет остальные. Не создавай новую покупку только ради разделения.
        Цена прямых SKU берётся из кэша, отсутствие карточки не блокирует ссылку. При неизвестной цене
        total_is_complete=false, subtotal_rub содержит только известную часть или null; соблюдение полного
        бюджета не подтверждено. Partial также означает неполный подбор или отказ ссылки в link_error.
        needs_revision содержит замечания бюджета. Error сообщает техническую причину; NOT_FOUND —
        исходный объект недоступен, INVALID_CART_REQUEST — непустой request для прямых SKU.

        Частичный результат сам по себе не требует повторного подбора. Если пользователь уже разрешил
        подходящие замены или другую фасовку и есть конкретное изменение, которое может решить проблему,
        продолжи через edit_cart с последним полученным cart_id без повторного согласия.
        Исправление существующей покупки не требует create_cart; независимую корзину создавай,
        когда пользователь просит отдельную покупку. Не повторяй безрезультатный подбор с теми же
        условиями и данными. Если продолжить в рамках поручения нельзя, покажи найденную часть,
        доступную ссылку и оставшиеся потребности с их назначением.
    """

    source: CartSource = Field(
        description="Один источник: сохранённый план, рецепты с порциями или готовые SKU"
    )

    limits: CartLimits = Field(
        description="Бюджет всей покупки без прибавки погрешности; null — без предела. Автоподстановки нет"
    )

    request: str = Field(
        default="",
        description=(
            "Только явно заданное пользователем общее поисковое уточнение. По умолчанию пустая строка. "
            "Этот текст добавляется КАЖДОМУ названию ингредиента без изменений, "
            "например «бренд X». Не передавай сюда весь диалог, список покупок, бюджет, исключение продуктов "
            "или поручение изменить количества. Пожелание «подешевле» тоже не поисковое уточнение: "
            "бюджет учитывается при выборе найденных товаров. Без общего уточнения и для готовых SKU оставь пустую строку"
        ),
    )


class CartResult(ToolResult[CartView]):
    data: CartView | None = Field(
        default=None,
        description="Созданная корзина, в том числе частичная: позиции, расчёт, ссылка и нерешённые продукты",
    )


async def create_cart(ctx: RunContext[Context], args: CreateCartInput) -> CartResult:
    d = ctx.deps
    try:
        return CartResult.from_result(
            await d.shopper.run(
                d.user_id,
                d.operation_id(ctx.tool_call_id),
                args.source,
                args.limits,
                d.person,
                ctx.usage,
                args.request,
                progress=d.progress("create_cart", ctx.tool_call_id),
            )
        )
    except LookupError as exc:
        return CartResult.failed("NOT_FOUND", str(exc))


# --- list_carts ---


class ListCartsInput(ToolInput):
    description: ClassVar[str] = """
        Показывает метаданные нескрытых корзин и черновиков пользователя. Аргументов и пагинации нет.
        Для содержимого используй get_cart. Ничего не изменяет.
    """

    # Публичный вызов: {}. Полей нет.


class CartListResult(ToolResult[list[CartSummary]]):
    data: list[CartSummary] | None = Field(
        default=None,
        description="Метаданные нескрытых корзин и черновиков пользователя; пустой список, если их нет",
    )


async def list_carts(ctx: RunContext[Context], args: ListCartsInput) -> CartListResult:
    return CartListResult.from_result(await ctx.deps.store.list_objects(ctx.deps.user_id, "cart"))


# --- get_cart ---


class CartId(ToolInput):
    description: ClassVar[str] = """
        Получает состав, упаковки, стоимость, ссылку и нерешённые потребности сохранённой корзины.
        Ничего не изменяет и не обновляет товары автоматически.
        В data поле unresolved содержит причины и recipe_ids для недостающих продуктов.
        При total_is_complete=false subtotal_rub — только стоимость найденной части.
        cart_links — части покупки со ссылками и ошибками; у прежних записей ссылка в cart_url.
        link_error обобщает отказы создания ссылок. Partial означает неполный подбор
        или недоступную ссылку; needs_revision содержит замечания бюджета в issues.
        NOT_FOUND означает, что указанная версия недоступна текущему пользователю.
    """

    cart_id: str = Field(description="ID доступной версии корзины или черновика")


class GetCartResult(ToolResult[CartView]):
    data: CartView | None = Field(
        default=None,
        description="Запрошенная версия корзины с сохранённым составом и ссылкой; null, если недоступна",
    )


async def get_cart(ctx: RunContext[Context], args: CartId) -> GetCartResult:
    return GetCartResult.from_result(await ctx.deps.store.get_object(ctx.deps.user_id, "cart", args.cart_id))


# --- edit_cart ---


class EditCartInput(ToolInput):
    description: ClassVar[str] = """
        Изменяет корзину по самодостаточному поручению и сохраняет полную новую версию.
        Передай конкретные продукты, количества с единицами и выбранные sku_id. Новые SKU предварительно
        найди через search_products или get_product_analogs; редактор не ищет замены сам.
        В корзине из готовых SKU правка работает с полным списком единиц продажи: целые упаковки либо дробные кг/л.
        Для нового нормализованного продукта можно указать название: внутри выполняется локальный поиск
        по существующему справочнику. Новые записи справочника не создаются. Раскрой отсылки вроде
        «возьми второй»: история диалога редактору не передаётся. Всю структуру корзины копировать не нужно.

        Сохраняет незатронутые позиции и их SKU, пересчитывает упаковки и стоимость, получает новые ссылки.
        Бюджет сохраняется из корзины; явное изменение суммы или снятие ограничения передай в request.
        Допуск 5% учитывает код, его не надо добавлять к сумме или согласовывать. Исходный план не меняется.
        В data: новый cart_id, parent_id, products, items, limits, subtotal_rub, total_is_complete и cart_links.
        Каждая магазинная ссылка содержит до 30 разных SKU; для всей покупки нужны все успешные ссылки.
        partial означает нерешённые позиции в unresolved (с recipe_ids) или отказ ссылки в link_error.
        При total_is_complete=false subtotal_rub — стоимость найденной части. Ссылка не оформляет заказ.
        needs_revision содержит замечания проверки после максимум одной коррекции. error.message сообщает
        техническую причину: NOT_FOUND, MODEL_FORMAT, PRODUCT_NOT_FOUND или INVALID_CART_PRODUCTS.
        Предыдущая версия сохраняется. При partial продолжай, если есть конкретная ещё не выполненная
        замена, уже разрешённая пользователем; не повторяй безрезультатную правку с теми же данными.
    """

    cart_id: str = Field(description="ID доступной версии корзины или черновика")

    request: str = Field(
        description=(
            "Самодостаточная правка: продукты, количества с единицами, выбранные sku_id, "
            "явное изменение бюджета. Раскрой отсылки. Для корзины из SKU укажи количества упаковок"
        ),
    )


class EditCartResult(ToolResult[CartView]):
    data: CartView | None = Field(
        default=None,
        description="Полная новая версия корзины; parent_id указывает исходную, cart_links относятся к новой",
    )


async def edit_cart(ctx: RunContext[Context], args: EditCartInput) -> EditCartResult:
    d = ctx.deps
    return EditCartResult.from_result(
        await d.shopper.edit(
            d.user_id, d.operation_id(ctx.tool_call_id), args.cart_id, args.request, ctx.usage
        )
    )


# --- search_products ---


class ProductQuery(ToolInput):
    description: ClassVar[str] = """
        Ищет SKU в магазине по названию и существенным свойствам. Возвращает кандидатов,
        фасовку и цену; возвращает до 10 кандидатов и не меняет корзину. Не обещай отсутствия аллергена без данных о составе.
    """

    query: str = Field(description="Название и существенные свойства искомого товара магазина")


class ProductSearchResult(ToolResult[list[Product]]):
    data: list[Product] | None = Field(
        default=None,
        description="До 10 найденных SKU с доступными данными о составе, цене и фасовке; пусто без совпадений",
    )


async def search_products(ctx: RunContext[Context], args: ProductQuery) -> ProductSearchResult:
    try:
        rows = (await ctx.deps.products.search(args.query, 10))[:10]
        rows = await ctx.deps.catalog.enrich_skus(rows)
        await ctx.deps.store.cache_skus(rows)
        return ProductSearchResult(data=[r.model_dump(mode="json") for r in rows[:10]])
    except IntegrationError as exc:
        return ProductSearchResult.failed("CATALOG_UNAVAILABLE", str(exc))


# --- get_product_analogs ---


class SkuId(ToolInput):
    description: ClassVar[str] = """
        Получает аналоги конкретного SKU через подключённый сервис. Не принимает product_id общего
        ингредиента и не заменяет товар в корзине. Для замены используй edit_cart.
        Возвращает до 10 аналогов. Если подключение недоступно, возвращает ошибку, а не выдуманные аналоги.
    """

    sku_id: str = Field(description="SKU исходного товара для поиска аналогов, не product_id ингредиента")


class ProductAnalogsResult(ToolResult[list[Product]]):
    data: list[Product] | None = Field(
        default=None,
        description="До 10 аналогов выбранного SKU; null при ошибке подключения, без подмены результата",
    )


async def get_product_analogs(ctx: RunContext[Context], args: SkuId) -> ProductAnalogsResult:
    try:
        rows = (await ctx.deps.products.analogs(args.sku_id, 10))[:10]
        rows = await ctx.deps.catalog.enrich_skus(rows)
        await ctx.deps.store.cache_skus(rows)
        return ProductAnalogsResult(data=[p.model_dump(mode="json") for p in rows])
    except IntegrationError as exc:
        return ProductAnalogsResult.failed("ANALOGS_UNAVAILABLE", str(exc))


# Общая память


class MemoryData(Record):
    memory: str = Field(description="Актуальная полная редакция общей памяти")
    memory_revision: int = Field(description="Версия записи для runtime; при конфликте нужна для объединения")


# --- update_memory ---


class MemoryInput(ToolInput):
    description: ClassVar[str] = """
        Заменяет общую память полной короткой редакцией. Сохраняй только явно сообщённые устойчивые
        факты о питании, готовке и покупках, без догадок, дублей анкеты и разовых исключений.
        Используй при новом факте, явно сообщённом устойчивом изменении или уточнении, просьбе запомнить,
        забыть либо исправить память. За основу text бери именно текущий memory, а не весь персональный
        контекст. Добавляй только новое устойчивое изменение из диалога: сведения из questionnaire
        уже доступны отдельно и не нуждаются в переносе. Сохрани остальные факты memory, объедини повторы;
        временные исключения остаются в диалоге. Одна просьба показать память не требует записи.
        text доступен всем чатам; пустая строка очищает общую память, но не удаляет переписку.
        Не восстанавливай удалённое из старой истории без нового сообщения пользователя об этих сведениях
        или их явного подтверждения. При MEMORY_CONFLICT запись не применена: data содержит актуальные memory
        и memory_revision. Объедини нужное изменение с этой редакцией и повтори запись;
        runtime обновляет версию. Успех подтверждается статусом ok и новой редакцией в data.
    """

    text: str = Field(
        description="Полная новая короткая редакция memory; пустая строка очищает её. "
        "Только устойчивые факты, без разовых разрешений и пояснений о них даже в скобках."
    )


class MemoryResult(ToolResult[MemoryData]):
    data: MemoryData | None = Field(
        default=None,
        description="Сохранённая редакция памяти либо актуальная редакция для объединения при MEMORY_CONFLICT",
    )


async def update_memory(ctx: RunContext[Context], args: MemoryInput) -> MemoryResult:
    d = ctx.deps
    result = await d.store.update_memory(
        d.user_id, d.person.memory_revision, args.text, d.operation_id(ctx.tool_call_id)
    )
    if isinstance(result.data, dict):
        d.person.memory = result.data["memory"]
        d.person.memory_revision = result.data["memory_revision"]
    return MemoryResult.from_result(result)


def traced_tool[Input: ToolInput, Output: ToolResult](
    fn: Callable[[RunContext[Context], Input], Awaitable[Output]],
) -> Callable[[RunContext[Context], Input], Awaitable[Output]]:
    """Добавляет статус предметного результата к уже созданному SDK span инструмента."""

    @wraps(fn)
    async def execute(ctx: RunContext[Context], args: Input) -> Output:
        span = get_current_span()
        span.set_attribute("langfuse.observation.input", args.model_dump_json())
        result = await fn(ctx, args)
        outcome(span, result.model_dump(mode="json"))
        return result

    return execute


def build_toolset() -> FunctionToolset[Context]:
    """Собирает явный реестр; схемы выводятся SDK из единственных типов.

    :return: Набор 12 доменных инструментов и служебной памяти
    """
    reads = [
        list_plans,
        get_plan,
        search_recipes,
        get_recipes,
        list_carts,
        get_cart,
        search_products,
        get_product_analogs,
    ]
    writes = [generate_plan, edit_plan, create_cart, edit_cart, update_memory]
    return FunctionToolset(
        tools=[
            Tool(
                traced_tool(fn),
                description=cleandoc(get_type_hints(fn)["args"].description),
                max_retries=0,
                docstring_format="sphinx",
                sequential=fn in writes,
            )
            for fn in reads + writes
        ]
    )
