import math
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, DecimalException

from pydantic import JsonValue

from app.models import (
    CartLimits,
    CartProduct,
    Issue,
    NormalizedProduct,
    Product,
    UnitConversion,
    budget_ceiling,
)


def valid_sale_quantity(quantity: float, sku: Product | None) -> bool:
    """Разрешает дробные сотые только для подтверждённой продажи за кг/л."""
    step = Decimal("0.01") if sku and sku.sale_unit else Decimal(1)
    try:
        return math.isfinite(quantity) and quantity > 0 and Decimal(str(quantity)) % step == 0
    except DecimalException:
        return False


def purchase_quote(
    amount: float | None, unit: str, sku: Product, conversion: UnitConversion | None = None
) -> dict[str, float] | None:
    """Рассчитывает покупку одинаково для выбора модели и сохранения корзины.

    :param amount: Потребность в единице нормализованного продукта
    :param unit: g, ml либо pcs нормализованного продукта
    :param sku: Цена и подтверждённая фасовка единицы продажи
    :param conversion: Явная справочная оценка для несовпадающей единицы фасовки
    :return: Единицы продажи, покрытие и цена покупки; None при недостатке данных
    """
    if (
        amount is None
        or not math.isfinite(amount)
        or amount <= 0
        or not sku.package_amount
        or sku.package_amount <= 0
        or (sku.package_unit != unit and conversion is None)
    ):
        return None
    step = Decimal("0.01") if sku.sale_unit else Decimal(1)
    try:
        package_amount = Decimal(str(sku.package_amount))
        if sku.package_unit != unit:
            package_amount *= Decimal(str(conversion.factor))
        quantity = (Decimal(str(amount)) / package_amount / step).to_integral_value(
            rounding=ROUND_CEILING
        ) * step
        cost = quantity * Decimal(str(sku.price_rub))
        result = {
            "quantity": float(quantity),
            "covered_amount": float(quantity * package_amount),
            "cost_rub": float(cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }
    except (DecimalException, OverflowError):
        return None
    return result if all(math.isfinite(v) for v in result.values()) else None


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
    quantities: dict[str, float], sku_prices: dict[str, float], limits: CartLimits
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
        total = float(
            sum(
                (Decimal(str(q)) * Decimal(str(sku_prices[sku]))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                for sku, q in quantities.items()
                if sku in sku_prices
            )
        )
    except (OverflowError, DecimalException):
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


def assemble(
    products: list[CartProduct],
    catalog: dict[str, NormalizedProduct],
    chosen: dict[str, Product | None],
    unresolved: list[dict[str, JsonValue]],
    problems: dict[str, str],
    limits: CartLimits,
) -> tuple[dict[str, JsonValue], list[Issue], dict[str, float]]:
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
    quantities: dict[str, float] = {}
    sku_prices: dict[str, float] = {}
    for product in products:
        sku = chosen.get(product.product_id)
        reason = problems.get(product.product_id) or pending.get(product.product_id) or "Товар не выбран"
        if sku and product.requested_sku_id and sku.sku_id != product.requested_sku_id:
            reason, sku = "Выбранного товара нет среди подходящих вариантов", None
        normalized = catalog[product.product_id]
        conversion = normalized.conversions.get(sku.package_unit) if sku else None
        quote = purchase_quote(product.amount, normalized.unit, sku, conversion) if sku else None
        if sku and quote is None:
            reason, sku = "Нельзя рассчитать упаковки: уточните количество или фасовку/единицу", None
        if sku is None:
            prefix = f"{catalog[product.product_id].name}: "
            unresolved.append(
                {
                    "product_id": product.product_id,
                    "recipe_ids": product.recipe_ids,
                    "reason": reason if reason.startswith(prefix) else prefix + reason,
                }
            )
            continue
        count = quote["quantity"]
        quantities[sku.sku_id] = round(quantities.get(sku.sku_id, 0) + count, 2)
        sku_prices[sku.sku_id] = sku.price_rub
        selected.append(
            {
                "product_id": product.product_id,
                "sku_id": sku.sku_id,
                "product": sku.model_dump(mode="json"),
                "packages": count,
                "quantity_basis": conversion.model_dump()
                if conversion and sku.package_unit != normalized.unit
                else None,
            }
        )
    total, issues = cart_total(quantities, sku_prices, limits)
    return (
        {
            "products": [
                p.model_dump(mode="json")
                | {
                    "requested_sku_id": next(
                        (row["sku_id"] for row in selected if row["product_id"] == p.product_id),
                        p.requested_sku_id,
                    )
                }
                for p in products
            ],
            "items": selected,
            "unresolved": unresolved,
            "subtotal_rub": total,
            "total_is_complete": not unresolved and bool(products) and total is not None,
        },
        issues,
        quantities,
    )
