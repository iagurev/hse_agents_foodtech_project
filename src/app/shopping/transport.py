from pydantic import JsonValue


def selection_input(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Размещает название, потребность и варианты каждого ингредиента рядом.

    :param payload: Пакет выбора с отдельными индексами продуктов и кандидатов
    :return: Те же данные без необходимости сопоставлять удалённые друг от друга ID
    """
    catalog = {row["product_id"]: row for row in payload["catalog"]}
    return {
        **{key: value for key, value in payload.items() if key not in {"products", "catalog", "candidates"}},
        "items": [
            catalog[product["product_id"]]
            | product
            | {"candidates": payload["candidates"][product["product_id"]]}
            for product in payload["products"]
        ],
    }
