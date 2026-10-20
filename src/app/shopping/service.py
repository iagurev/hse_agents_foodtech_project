import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import RunUsage

from app.catalog import Catalog
from app.integrations import CART_SKU_LIMIT, IntegrationError, Products
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
)
from app.progress import Progress
from app.shopping.calculations import aggregate, assemble, cart_total, purchase_quote, valid_sale_quantity
from app.storage import Store
from app.tracing import stage


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
            for sku in products:
                sku.price_source = sku.price_source or "Кэш поиска MCP ВкусВилла"
                sku.price_observed_at = (
                    sku.price_observed_at or datetime.fromtimestamp(row["updated_at"], UTC).isoformat()
                )
        else:
            async with self.semaphore:
                products = (await self.products.search(query, 10))[:10]
            async with self.store.connection() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO sku_cache VALUES(?,?,?)",
                    (key, json.dumps([p.model_dump() for p in products]), time.time()),
                )
                await db.commit()
        # При совпадении ID ответ MCP, включая кэш поиска, важнее снимка каталога.
        ids = {p.sku_id for p in products}
        products.extend(p for p in await self.catalog.linked_skus(product.product_id) if p.sku_id not in ids)
        products = await self.catalog.enrich_skus(products)
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
        progress: Progress | None = None,
    ) -> Result:
        """Создаёт новую корзину из существующих нормализованных продуктов.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param source: План, рецепты с порциями или готовые SKU
        :param limits: Исходный бюджет пользователя
        :param context: Персональный контекст
        :param usage: Общий счётчик вызовов
        :param progress: Этапы и счётчики поиска текущего вызова
        :param request: Буквальное поисковое уточнение для каждого ингредиента; пусто для SKU
        :return: Сохранённая корзина с нерешёнными продуктами
        """
        previous = await self.store.replay(user_id, operation_id)
        if previous:
            return previous
        progress = progress or Progress()
        if isinstance(source, SkusSource):
            if request:
                return Result.failed("INVALID_CART_REQUEST", "Для готовых SKU request должен быть пустым")
            await progress.report("cart_calculate")
            try:
                payload, issues, quantities = await self.assemble_skus(source.items, limits)
            except IntegrationError as exc:
                return Result.failed("INVALID_CART_QUANTITY", str(exc))
            return await self.save(
                user_id, operation_id, source, limits, payload, issues, quantities, progress=progress
            )
        await progress.report("cart_prepare")
        catalog = await self.catalog.products()
        with stage("cart.aggregate"):
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
        completed = found = 0
        await progress.report("cart_search", completed=0, total=len(products), found=0)

        async def search(product: CartProduct) -> list[Product] | IntegrationError:
            nonlocal completed, found
            try:
                result = await self.candidates(catalog[product.product_id], request)
            except IntegrationError as exc:
                result = exc
            completed += 1
            found += int(isinstance(result, list) and bool(result))
            await progress.report("cart_search", completed=completed, found=found)
            return result

        with stage("cart.search", {"products": len(products)}):
            outcomes = await asyncio.gather(*(search(p) for p in products), return_exceptions=True)
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
            await progress.report("cart_select")
            recipes = await self.catalog.recipes_by_ids(sorted({r for p in products for r in p.recipe_ids}))
            selection = await self.generator.generate(
                "product_selection",
                Selection,
                {
                    "products": [p.model_dump(mode="json") for p in products],
                    "catalog": [
                        catalog[p.product_id].model_dump(
                            mode="json", include={"product_id", "name", "unit", "restrictions", "conversions"}
                        )
                        for p in products
                    ],
                    "candidates": {
                        product.product_id: [
                            sku.model_dump(mode="json")
                            | {
                                "purchase": purchase_quote(
                                    product.amount,
                                    catalog[product.product_id].unit,
                                    sku,
                                    catalog[product.product_id].conversions.get(sku.package_unit),
                                )
                            }
                            for sku in candidates[product.product_id]
                        ]
                        for product in products
                    },
                    "context": context.model_dump(mode="json"),
                    "recipes": [
                        r.model_dump(include={"recipe_id", "title", "steps"}) for r in recipes.values()
                    ],
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
        await progress.report("cart_calculate")
        with stage("cart.calculate"):
            payload, issues, quantities = assemble(products, catalog, chosen, unresolved, problems, limits)
        return await self.save(
            user_id, operation_id, source, limits, payload, issues, quantities, progress=progress
        )

    async def assemble_skus(
        self,
        items: list[SkuAmount],
        limits: CartLimits,
        snapshots: list[dict[str, JsonValue]] | None = None,
    ) -> tuple[dict[str, JsonValue], list[Issue], dict[str, float]]:
        """Собирает явно выбранные упаковки; карточки нужны только для известной цены.

        :param items: Полный набор SKU и упаковок
        :param limits: Действующий бюджет
        :param snapshots: Карточки предыдущей версии для сохранения неизменённых товаров
        :return: Корзина, замечания бюджета и количества для ссылки
        """
        quantities: dict[str, float] = {}
        for item in items:
            quantities[item.sku_id] = round(quantities.get(item.sku_id, 0) + item.packages, 2)
        cards = await self.store.get_skus(list(quantities))
        for row in snapshots or []:
            if row.get("product"):
                card = Product.model_validate(row["product"])
                if card.sku_id in quantities:
                    cards[card.sku_id] = card
        invalid = [
            item.sku_id for item in items if not valid_sale_quantity(item.packages, cards.get(item.sku_id))
        ]
        if invalid:
            raise IntegrationError(
                "Дробное количество разрешено только для весового товара: " + ", ".join(invalid)
            )
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
        quantities: dict[str, float],
        parent_id: str | None = None,
        progress: Progress | None = None,
    ) -> Result:
        """Создаёт ссылки по лимиту SKU и сохраняет одну версию, включая отказы отдельных частей.

        :param user_id: Владелец
        :param operation_id: Ключ повторной доставки
        :param source: Источник исходной покупки
        :param limits: Действующий бюджет
        :param payload: Пересчитанная корзина
        :param issues: Замечания проверки
        :param quantities: Количество упаковок по SKU
        :param progress: Этапы создания ссылки и сохранения
        :param parent_id: Предыдущая версия при редактировании
        :return: Сохранённая версия с фактическим статусом
        """
        progress = progress or Progress()
        links, errors = [], []
        if quantities:
            await progress.report("cart_link")
        rows = list(quantities.items())
        for offset in range(0, len(rows), CART_SKU_LIMIT):
            group = dict(rows[offset : offset + CART_SKU_LIMIT])
            link = {"sku_ids": list(group), "url": None, "error": None}
            try:
                link["url"] = await self.products.cart_link(group)
            except IntegrationError as exc:
                link["error"] = str(exc)
                errors.append(f"Часть {len(links) + 1}: {exc}" if len(rows) > CART_SKU_LIMIT else str(exc))
            links.append(link)
        link_error = "; ".join(errors) or None
        url = links[0]["url"] if len(links) == 1 else None
        status = (
            "needs_revision"
            if issues
            else "partial"
            if not payload["total_is_complete"] or link_error
            else "ok"
        )
        await progress.report("cart_save")
        return await self.store.save_object(
            user_id,
            "cart",
            payload
            | {
                "source": source.model_dump(mode="json"),
                "cart_url": url,
                "cart_links": links,
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
                try:
                    data, issues, quantities = await self.assemble_skus(
                        candidate.items, fixed_limits, current.data["items"]
                    )
                except IntegrationError as exc:
                    return Result.failed("INVALID_CART_QUANTITY", str(exc))
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
                        problems[product.product_id] = "Нет данных о выбранном товаре; нужен повторный поиск"
                data, issues, quantities = assemble(
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
