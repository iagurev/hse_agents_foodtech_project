import json

from pydantic import BaseModel, JsonValue
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, ToolReturnPart, UserPromptPart

from app.catalog import Catalog
from app.models import Result
from app.web.storage import WebStore


def artifacts(content: object) -> list[dict[str, JsonValue]]:
    """Проецирует только известные структуры результатов инструментов.

    :param content: SDK-результат, не текст ответа агента
    :return: Карточки с идентификаторами и краткими данными
    """
    if isinstance(content, BaseModel):
        content = content.model_dump(mode="json")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return []
    if not isinstance(content, dict):
        return []
    data = content.get("data")
    if isinstance(data, dict) and isinstance(data.get("recipes"), list):
        data = data["recipes"]
    rows = data if isinstance(data, list) else [data]
    cards = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for kind in ("plan", "cart", "recipe"):
            key = f"{kind}_id"
            if row.get(key):
                cards.append(
                    {
                        "kind": kind,
                        "id": row[key],
                        "title": row.get("title"),
                        "days": row.get("days"),
                        "status": row.get("status", content.get("status")),
                        "image_url": row.get("image_url"),
                        "total": row.get("subtotal_rub"),
                        "cost": row.get("totals", {}).get("mean_rub"),
                    }
                )
                break
    return cards


def project_messages(raw: str) -> dict[str, JsonValue]:
    messages = ModelMessagesTypeAdapter.validate_json(raw)
    users, texts, cards = [], [], []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                users.append(part.content)
            elif isinstance(part, TextPart):
                texts.append(part.content)
            elif isinstance(part, ToolReturnPart):
                cards.extend(artifacts(part.content))
    unique = {f"{c['kind']}:{c['id']}": c for c in cards}
    return {"text": "\n".join(users), "output": "\n\n".join(texts), "artifacts": list(unique.values())}


async def transcript(store: WebStore, chat_id: str) -> list[dict[str, JsonValue]]:
    """Читает весь UI-журнал, включая заблокированные ходы, независимо от сводки."""
    async with store.connection() as db:
        turns = await (
            await db.execute("SELECT * FROM turns WHERE chat_id=? ORDER BY seq", (chat_id,))
        ).fetchall()
        runs = await (
            await db.execute("SELECT * FROM web_runs WHERE chat_id=? ORDER BY created_at", (chat_id,))
        ).fetchall()
    run_map = {r["turn_id"]: dict(r) for r in runs}
    result = []
    for row in turns:
        run = run_map.pop(row["turn_id"], {})
        result.append(
            {
                "turn_id": row["turn_id"],
                "state": run.get("state", "complete"),
                **project_messages(row["messages"]),
            }
        )
    for row in run_map.values():
        result.append(
            {
                "turn_id": row["turn_id"],
                "state": row["state"],
                "text": row["text"],
                "output": row["output"],
                "artifacts": [],
            }
        )
    return result


async def recipe_view(catalog: Catalog, recipe_id: str) -> dict[str, JsonValue] | None:
    index = await catalog.recipes_by_ids([recipe_id])
    recipe = index.get(recipe_id)
    if recipe is None:
        return None
    products = await catalog.products()
    result = recipe.model_dump(mode="json")
    result["ingredients"] = [
        i.model_dump(mode="json")
        | {
            "name": products[i.product_id].name if i.product_id in products else i.product_id,
            "unit": products[i.product_id].unit if i.product_id in products else "",
        }
        for i in recipe.ingredients
    ]
    return result


async def cart_view(catalog: Catalog, result: Result) -> Result:
    """Раскрывает названия пропусков и объединяет одинаковые SKU только для UI."""
    if not isinstance(result.data, dict):
        return result
    data = dict(result.data)
    products = await catalog.products()
    data["unresolved"] = [
        dict(row)
        | {"product_name": products[row["product_id"]].name if row.get("product_id") in products else None}
        for row in data.get("unresolved", [])
    ]
    rows = {}
    for row in data.get("items", []):
        sku_id = row.get("sku_id") or (row.get("product") or {}).get("sku_id")
        if sku_id in rows:
            rows[sku_id]["packages"] += row["packages"]
        else:
            rows[sku_id] = dict(row) | {"sku_id": sku_id}
    data["items"] = list(rows.values())
    return result.model_copy(update={"data": data})
