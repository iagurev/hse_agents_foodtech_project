from loguru import logger
from pydantic import JsonValue, ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from app.llm.gateway import Generator
from app.models import Choice, Selection


async def select_products(
    generator: Generator, payload: dict[str, JsonValue], usage: RunUsage
) -> dict[str, Choice]:
    """Выбирает товары и один раз перепроверяет пропуски при наличии вариантов.

    Уже выбранные товары не меняются. Ошибка дополнительной попытки не теряет
    найденную часть; отсутствующие кандидаты не запускают бессмысленный повтор.

    :param generator: Общая граница модельных вызовов с лимитом запросов
    :param payload: Потребности, кандидаты с расчётами и условия пользователя
    :param usage: Счётчики текущего хода
    :return: Выбор по каждому обработанному продукту
    """
    selection = await generator.generate("product_selection", Selection, payload, usage)
    choices = {choice.product_id: choice for choice in selection.choices}
    pending, selected_cost = set(), 0.0
    for product_id, candidates in payload["candidates"].items():
        choice = choices.get(product_id)
        selected = next((row for row in candidates if choice and row["sku_id"] == choice.sku_id), None)
        if selected:
            selected_cost += selected["purchase"]["cost_rub"]
        elif candidates:
            pending.add(product_id)
    if not pending:
        return choices
    repair = payload | {
        "products": [row for row in payload["products"] if row["product_id"] in pending],
        "catalog": [row for row in payload["catalog"] if row["product_id"] in pending],
        "candidates": {key: rows for key, rows in payload["candidates"].items() if key in pending},
        "selected_cost_rub": round(selected_cost, 2),
        "feedback": {
            "instruction": "Проверь пропущенные позиции по каждому кандидату; сохраняй все ограничения.",
            "previous_choices": [
                choice.model_dump(mode="json") for key, choice in choices.items() if key in pending
            ],
        },
    }
    try:
        revised = await generator.generate("product_selection", Selection, repair, usage)
    except (ModelHTTPError, UnexpectedModelBehavior, ValidationError, UsageLimitExceeded) as exc:
        logger.bind(error_type=type(exc).__name__, products=len(pending)).warning(
            "Product selection recheck failed; preserving selected items"
        )
    else:
        choices.update(
            (choice.product_id, choice) for choice in revised.choices if choice.product_id in pending
        )
    return choices
