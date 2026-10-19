import asyncio
import hashlib
import json
import math
import time

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import RunUsage

from app.catalog import Catalog
from app.integrations import IntegrationError, Products
from app.llm import Generator
from app.models import (
    CartLimits,
    CartProduct,
    CartRevision,
    CartSource,
    Issue,
    NormalizedProduct,
    PersonContext,
    PlanSource,
    Product,
    RecipesSource,
    Result,
    Selection,
    SkuAmount,
    SkuCartRevision,
    SkusSource,
    StoredCartSource,
    StoredSkusSource,
    budget_ceiling,
)
from app.storage import Store


def aggregate(products: list[CartProduct]) -> list[CartProduct]:
    """Складывает количества одного product_id в единице его справочника.

    :param products: Продукты рецептов или разобранного запроса
    :return: Один элемент на product_id с назначением и количеством
    """
    result: dict[str, CartProduct] = {}
    for item in products:
        if item.product_id not in result:
            result[item.product_id] = item.model_copy(deep=True)
            continue
        target = result[item.product_id]
        if target.requested_sku_id != item.requested_sku_id:
            raise ValueError("Для одного продукта указаны несовместимые требования к SKU")
        target.amount = None if target.amount is None or item.amount is None else target.amount + item.amount
        target.recipe_ids = sorted(set(target.recipe_ids + item.recipe_ids))
    return list(result.values())


def cart_total(
    quantities: dict[str, int], sku_prices: dict[str, float], limits: CartLimits
) -> tuple[float | None, list[Issue]]:
    """Считает только известную стоимость; проверяет бюджет с допуском 5%.

    :param quantities: Упаковки выбранных SKU
    :param sku_prices: Достоверные известные цены
    :param limits: Бюджет пользователя
    :return: Сумма известной части (null при отсутствии цен) и замечания
    """
    if quantities and not any(sku in sku_prices for sku in quantities):
        return None, []
    try:
        total = round(sum(q * sku_prices[sku] for sku, q in quantities.items() if sku in sku_prices), 2)
    except OverflowError:
        total = float("inf")
    issues = []
    if not math.isfinite(total):
        total = None
        issues.append(
            Issue(
                code="INVALID_NUMBER",
                path="subtotal_rub",
                message="Переполнение стоимости",
                actual="overflow",
                expected="finite number",
                requirement_source="numeric format",
                repair_scope="products.amount",
            )
        )
    elif limits.budget_rub is not None and total > budget_ceiling(limits.budget_rub):
        issues.append(
            Issue(
                code="BUDGET_EXCEEDED",
                path="subtotal_rub",
                message="Найденная часть дороже бюджета с допуском 5%",
                actual=total,
                expected=budget_ceiling(limits.budget_rub),
                requirement_source="limits.budget_rub",
                repair_scope="products",
            )
        )
    return total, issues


class Shopper:
    """Агрегация продуктов, кэш кандидатов, пакетный выбор и сохранение корзины."""

    def __init__(
        self, store: Store, catalog: Catalog, products: Products, generator: Generator | None
    ) -> None:
        self.store, self.catalog, self.products, self.generator = store, catalog, products, generator
        self.semaphore = asyncio.Semaphore(4)

    async def candidates(self, product: NormalizedProduct, request: str = "") -> list[Product]:
        query = f"{product.name} {request}" if request else product.name
        # Новый формат MCP: id и рубли; прежние HTTP-кандидаты не переиспользуются.
        key = hashlib.sha256(f"mcp-v1:{query}".encode()).hexdigest()
        async with self.store.connection() as db:
            row = await (
                await db.execute("SELECT payload,updated_at FROM sku_cache WHERE cache_key=?", (key,))
            ).fetchone()
        if row and time.time() - row["updated_at"] < 86400:
            products = [Product.model_validate(p) for p in json.loads(row["payload"])][:10]
            await self.store.cache_skus(products)
            return products
        async with self.semaphore:
            products = (await self.products.search(query, 10))[:10]
        async with self.store.connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO sku_cache VALUES(?,?,?)",
                (key, json.dumps([p.model_dump() for p in products]), time.time()),
            )
            await db.commit()
        await self.store.cache_skus(products)
        return products

    async def source_products(self, user_id: str, source: PlanSource | RecipesSource) -> list[CartProduct]:
        if isinstance(source, PlanSource):
            plan = await self.store.get_object(user_id, "plan", source.plan_id)
            if plan.status == "error":
                raise LookupError("Исходный план не найден")
            items = plan.data["meals"]
        else:
            items = [item.model_dump() for item in source.items]
        index = await self.catalog.recipes_by_ids([row["recipe_id"] for row in items])
        products = []
        for row in items:
            recipe = index.get(row["recipe_id"])
            if recipe is None:
                raise LookupError(f"Рецепт {row['recipe_id']} не найден")
            products.extend(
                CartProduct(
                    product_id=i.product_id, amount=i.amount * row["servings"], recipe_ids=[recipe.recipe_id]
                )
                for i in recipe.ingredients
            )
        return aggregate(products)

    async def run(
        self,
        user_id: str,
        operation_id: str,
        source: CartSource,
        limits: CartLimits,
        context: PersonContext,
        usage: RunUsage,
        request: str = "",
    ) -> Result:
        """Создаёт новую корзину из существующих нормализованных продуктов.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param source: План, рецепты с порциями или готовые SKU
        :param limits: Исходный бюджет пользователя
        :param context: Персональный контекст
        :param usage: Общий счётчик вызовов
        :param request: Буквальное поисковое уточнение для каждого ингредиента; пусто для SKU
        :return: Сохранённая корзина с нерешёнными продуктами
        """
        previous = await self.store.replay(user_id, operation_id)
        if previous:
            return previous
        if isinstance(source, SkusSource):
            if request:
                return Result.failed("INVALID_CART_REQUEST", "Для готовых SKU request должен быть пустым")
            payload, issues, quantities = await self.assemble_skus(source.items, limits)
            return await self.save(user_id, operation_id, source, limits, payload, issues, quantities)
        catalog = await self.catalog.products()
        products = await self.source_products(user_id, source)
        unresolved = []
        unknown = [p.product_id for p in products if p.product_id not in catalog]
        if unknown:
            return Result.failed(
                "PRODUCT_NOT_FOUND", "Продукты отсутствуют в справочнике: " + ", ".join(unknown)
            )
        try:
            products = aggregate(products)
        except ValueError as exc:
            return Result.failed("INVALID_CART_PRODUCTS", str(exc))
        outcomes = await asyncio.gather(
            *(self.candidates(catalog[p.product_id], request) for p in products), return_exceptions=True
        )
        candidates: dict[str, list[Product]] = {}
        problems = {}
        for product, outcome in zip(products, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, IntegrationError):
                    raise outcome
                candidates[product.product_id] = []
                problems[product.product_id] = str(outcome)
            else:
                candidates[product.product_id] = outcome
        choices = {}
        if any(candidates.values()):
            selection = await self.generator.generate(
                "product_selection",
                Selection,
                {
                    "products": [p.model_dump(mode="json") for p in products],
                    "catalog": [
                        catalog[p.product_id].model_dump(
                            mode="json", include={"product_id", "name", "unit", "restrictions"}
                        )
                        for p in products
                    ],
                    "candidates": {k: [p.model_dump(mode="json") for p in v] for k, v in candidates.items()},
                    "context": context.model_dump(mode="json"),
                    "request": request,
                    "limits": limits.model_dump(mode="json"),
                },
                usage,
            )
            choices = {c.product_id: c for c in selection.choices}
        chosen = {}
        for product in products:
            choice = choices.get(product.product_id)
            chosen[product.product_id] = next(
                (sku for sku in candidates[product.product_id] if choice and sku.sku_id == choice.sku_id),
                None,
            )
            if product.product_id not in problems and choice:
                problems[product.product_id] = choice.reason
        payload, issues, quantities = self.assemble(products, catalog, chosen, unresolved, problems, limits)
        return await self.save(user_id, operation_id, source, limits, payload, issues, quantities)

    @staticmethod
    def assemble(
        products: list[CartProduct],
        catalog: dict[str, NormalizedProduct],
        chosen: dict[str, Product | None],
        unresolved: list[dict[str, JsonValue]],
        problems: dict[str, str],
        limits: CartLimits,
    ) -> tuple[dict[str, JsonValue], list[Issue], dict[str, int]]:
        """Пересчитывает полную корзину по достоверным данным SKU.

        :param products: Полный состав покупки
        :param catalog: Справочник единиц
        :param chosen: Выбранные фактические SKU по продукту
        :param unresolved: Нерешённые позиции редактора или разбора
        :param problems: Технические причины отсутствия SKU
        :param limits: Действующий бюджет
        :return: Содержимое версии, замечания и упаковки для ссылки
        """
        pending = {row["product_id"]: row["reason"] for row in unresolved if row["product_id"]}
        unresolved = [row for row in unresolved if row["product_id"] is None]
        selected = []
        quantities: dict[str, int] = {}
        sku_prices: dict[str, float] = {}
        for product in products:
            sku = chosen.get(product.product_id)
            reason = problems.get(product.product_id) or pending.get(product.product_id) or "Товар не выбран"
            if sku and product.requested_sku_id and sku.sku_id != product.requested_sku_id:
                reason, sku = "Запрошенный SKU отсутствует среди подходящих кандидатов", None
            if sku and (
                product.amount is None
                or product.amount <= 0
                or not sku.package_amount
                or sku.package_amount <= 0
                or sku.package_unit != catalog[product.product_id].unit
                or not math.isfinite(product.amount / sku.package_amount)
            ):
                reason, sku = "Нельзя рассчитать упаковки: уточните количество или фасовку/единицу", None
            if sku is None:
                unresolved.append(
                    {
                        "product_id": product.product_id,
                        "recipe_ids": product.recipe_ids,
                        "reason": f"{catalog[product.product_id].name}: {reason}",
                    }
                )
                continue
            count = math.ceil(product.amount / sku.package_amount)
            quantities[sku.sku_id] = quantities.get(sku.sku_id, 0) + count
            sku_prices[sku.sku_id] = sku.price_rub
            selected.append(
                {
                    "product_id": product.product_id,
                    "sku_id": sku.sku_id,
                    "product": sku.model_dump(mode="json"),
                    "packages": count,
                }
            )
        total, issues = cart_total(quantities, sku_prices, limits)
        return (
            {
                "products": [p.model_dump(mode="json") for p in products],
                "items": selected,
                "unresolved": unresolved,
                "subtotal_rub": total,
                "total_is_complete": not unresolved and bool(products) and total is not None,
            },
            issues,
            quantities,
        )

    async def assemble_skus(
        self,
        items: list[SkuAmount],
        limits: CartLimits,
        snapshots: list[dict[str, JsonValue]] | None = None,
    ) -> tuple[dict[str, JsonValue], list[Issue], dict[str, int]]:
        """Собирает явно выбранные упаковки; карточки нужны только для известной цены.

        :param items: Полный набор SKU и упаковок
        :param limits: Действующий бюджет
        :param snapshots: Карточки предыдущей версии для сохранения неизменённых товаров
        :return: Корзина, замечания бюджета и количества для ссылки
        """
        quantities: dict[str, int] = {}
        for item in items:
            quantities[item.sku_id] = quantities.get(item.sku_id, 0) + item.packages
        cards = await self.store.get_skus(list(quantities))
        for row in snapshots or []:
            if row.get("product"):
                card = Product.model_validate(row["product"])
                if card.sku_id in quantities:
                    cards[card.sku_id] = card
        total, issues = cart_total(quantities, {k: v.price_rub for k, v in cards.items()}, limits)
        return (
            {
                "products": [],
                "items": [
                    {
                        "product_id": None,
                        "sku_id": sku_id,
                        "product": cards[sku_id].model_dump(mode="json") if sku_id in cards else None,
                        "packages": count,
                    }
                    for sku_id, count in quantities.items()
                ],
                "unresolved": [],
                "subtotal_rub": total,
                "total_is_complete": bool(quantities)
                and all(k in cards for k in quantities)
                and total is not None,
            },
            issues,
            quantities,
        )

    async def save(
        self,
        user_id: str,
        operation_id: str,
        source: StoredCartSource,
        limits: CartLimits,
        payload: dict[str, JsonValue],
        issues: list[Issue],
        quantities: dict[str, int],
        parent_id: str | None = None,
    ) -> Result:
        """Создаёт ссылку один раз и сохраняет новую версию даже при отказе интеграции.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param source: Источник исходной покупки
        :param limits: Действующий бюджет
        :param payload: Пересчитанная корзина
        :param issues: Замечания проверки
        :param quantities: Количество упаковок по SKU
        :param parent_id: Предыдущая версия при редактировании
        :return: Сохранённая версия с фактическим статусом
        """
        url, link_error = None, None
        if quantities:
            try:
                url = await self.products.cart_link(quantities)
            except IntegrationError as exc:
                link_error = str(exc)
        status = (
            "needs_revision"
            if issues
            else "partial"
            if not payload["total_is_complete"] or link_error
            else "ok"
        )
        return await self.store.save_object(
            user_id,
            "cart",
            payload
            | {
                "source": source.model_dump(mode="json"),
                "cart_url": url,
                "link_error": link_error,
                "limits": limits.model_dump(mode="json"),
            },
            Result(status=status, issues=issues),
            operation_id,
            parent_id,
        )

    async def edit(
        self, user_id: str, operation_id: str, cart_id: str, request: str, usage: RunUsage
    ) -> Result:
        """Редактирует полную корзину без повторного подбора её SKU.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param cart_id: Исходная версия
        :param request: Самодостаточное поручение
        :param usage: Счётчики всего хода
        :return: Новая версия либо техническая ошибка
        """
        previous = await self.store.replay(user_id, operation_id)
        if previous:
            return previous
        current = await self.store.get_object(user_id, "cart", cart_id)
        if current.status == "error":
            return current
        source = TypeAdapter(StoredCartSource).validate_python(current.data["source"])
        direct = isinstance(source, (SkusSource, StoredSkusSource))
        relevant = (
            {}
            if direct
            else await self.catalog.edit_products(
                request, [p["product_id"] for p in current.data["products"]]
            )
        )
        mentioned = await self.store.mentioned_skus(request)
        payload = {
            "current_cart": current.data,
            "request": request,
            "products": [p.model_dump(mode="json") for p in relevant.values()],
            "selected_skus": [p.model_dump(mode="json") for p in mentioned.values()],
        }
        fixed_limits = None
        catalog = {} if direct else await self.catalog.products()
        schema = SkuCartRevision if direct else CartRevision
        for attempt in range(2):
            try:
                candidate = await self.generator.generate("edit_cart", schema, payload, usage, "low")
            except (UnexpectedModelBehavior, ValidationError) as exc:
                if attempt:
                    return Result.failed("MODEL_FORMAT", "Редактор дважды не вернул результат по схеме")
                payload["feedback"] = {"kind": "format", "message": str(exc)[:2000]}
                continue
            if fixed_limits is None:
                fixed_limits = candidate.limits.model_copy(deep=True)
            candidate.limits = fixed_limits.model_copy(deep=True)
            if isinstance(candidate, SkuCartRevision):
                data, issues, quantities = await self.assemble_skus(
                    candidate.items, fixed_limits, current.data["items"]
                )
                retained = (
                    current.data.get("unresolved", [])
                    if candidate.unresolved is None
                    else [p.model_dump(mode="json") for p in candidate.unresolved]
                )
                data["unresolved"].extend(p for p in retained if p not in data["unresolved"])
                if data["unresolved"]:
                    data["total_is_complete"] = False
            else:
                unknown = [p.product_id for p in candidate.products if p.product_id not in catalog]
                if unknown:
                    return Result.failed(
                        "PRODUCT_NOT_FOUND", "Продукты отсутствуют в справочнике: " + ", ".join(unknown)
                    )
                try:
                    products = aggregate(candidate.products)
                except ValueError as exc:
                    return Result.failed("INVALID_CART_PRODUCTS", str(exc))
                cached = await self.store.get_skus(
                    [p.requested_sku_id for p in products if p.requested_sku_id]
                )
                snapshots = {
                    row["product_id"]: Product.model_validate(row["product"]) for row in current.data["items"]
                }
                chosen, problems = {}, {}
                for product in products:
                    old = snapshots.get(product.product_id)
                    sku_id = product.requested_sku_id
                    # Неизменённый товар сохраняет прежний снимок, даже если общий кэш уже обновился.
                    chosen[product.product_id] = old if old and old.sku_id == sku_id else cached.get(sku_id)
                    if sku_id and chosen[product.product_id] is None:
                        problems[product.product_id] = f"Нет данных SKU {sku_id}; выполните поиск товара"
                data, issues, quantities = self.assemble(
                    products,
                    catalog,
                    chosen,
                    [p.model_dump(mode="json") for p in candidate.unresolved],
                    problems,
                    fixed_limits,
                )
            if not issues or attempt:
                return await self.save(
                    user_id, operation_id, source, fixed_limits, data, issues, quantities, cart_id
                )
            payload["candidate"] = candidate.model_dump(mode="json")
            payload["feedback"] = [issue.model_dump(mode="json") for issue in issues]
            payload["fixed_limits"] = fixed_limits.model_dump(mode="json")
        raise AssertionError("Unreachable")
