import json
import math
from datetime import datetime
from hashlib import sha256
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, ToolReturnPart, UserPromptPart

from app.catalog.store import Catalog
from app.models import Result
from app.web.storage import WebStore


def image_url(recipe_id: str, source: str | None) -> str | None:
    """Возвращает внутренний адрес с ключом кэша по исходному URL."""
    if not source:
        return None
    revision = sha256(source.encode()).hexdigest()[:16]
    return f"/api/recipes/{quote(recipe_id, safe='')}/image?v={revision}"


def image_source(source: str | None) -> str | None:
    """Разрешает только известные источники; ВкусВилл доставляется через wsrv.nl."""
    url = urlsplit(source or "")
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
        return None
    if url.hostname == "vkusvill.ru":
        return "https://wsrv.nl/?" + urlencode({"url": source, "w": 960, "output": "webp"})
    return source if url.hostname == "images.gastronom.ru" else None


async def object_labels(store: WebStore, catalog: Catalog, user_id: str) -> dict[str, dict]:
    """Готовит общие названия цепочек без изменения сохранённого содержимого."""
    metadata = await store.object_metadata(user_id)
    recipe_ids = list(
        {
            row["recipe_id"]
            for m in metadata.values()
            if m["root_data"].get("source", {}).get("type") == "recipes"
            for row in m["root_data"]["source"]["items"]
        }
    )
    recipes = await catalog.recipes_by_ids(recipe_ids) if recipe_ids else {}
    titles = {}

    def title(meta: dict) -> str:
        root = meta["root_id"]
        if root in titles:
            return titles[root]
        data = meta["root_data"]
        date = (
            " · " + datetime.fromtimestamp(meta["created_at"], ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
            if meta["created_at"] is not None
            else ""
        )
        if meta["title"]:
            value = meta["title"]
        elif root.startswith("plan_"):
            days = data.get("days", 0)
            word = (
                "день"
                if days % 10 == 1 and days % 100 != 11
                else ("дня" if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14) else "дней")
            )
            value = f"Меню на {days} {word}{date}"
        else:
            source = data.get("source", {})
            plan = metadata.get(source.get("plan_id")) if source.get("type") == "plan" else None
            if plan:
                value = title(plan)
            elif source.get("type") == "recipes":
                names = list(
                    dict.fromkeys(
                        recipes[r["recipe_id"]].title for r in source["items"] if r["recipe_id"] in recipes
                    )
                )
                value = ", ".join(names[:2]) + (f" и ещё {len(names) - 2}" if len(names) > 2 else "")
                value = value or "Покупки" + date
            else:
                value = "Покупки" + date
        titles[root] = value if len(value) <= 80 else value[:77].rstrip() + "…"
        return titles[root]

    return {
        oid: {
            "root_id": m["root_id"],
            "title": title(m),
            "hidden": bool(m["hidden"]),
            "sequence": m["sequence"],
        }
        for oid, m in metadata.items()
    }


async def object_view(
    store: WebStore, catalog: Catalog, user_id: str, result: Result, labels: dict | None = None
) -> Result:
    """Добавляет UI-метаданные и группирует товары, сохраняя ID исходной версии."""
    if not isinstance(result.data, dict):
        return result
    if "cart_id" in result.data:
        result = await cart_view(catalog, result)
    if labels is None:
        labels = await object_labels(store, catalog, user_id)
    oid = result.data.get("plan_id") or result.data.get("cart_id")
    meta = {k: v for k, v in labels.get(oid, {}).items() if k != "sequence"}
    return result.model_copy(update={"data": result.data | meta})


async def project_cards(
    store: WebStore, catalog: Catalog, user_id: str, cards: list[dict], labels: dict | None = None
) -> list[dict]:
    """Выбирает новейшую из полученных в этом ответе версий каждой цепочки."""
    if labels is None:
        labels = await object_labels(store, catalog, user_id)
    opened = {c["id"] for c in cards if c.get("_opened")}
    merged = {}
    for card in cards:
        if card["kind"] == "recipe" and opened and card["id"] not in opened:
            continue
        meta = labels.get(card["id"], {})
        key = (card["kind"], meta.get("root_id", card["id"]))
        previous = merged.get(key, {})
        if labels.get(previous.get("id"), {}).get("sequence", 0) > meta.get("sequence", 0):
            continue
        merged[key] = {k: v for k, v in card.items() if k != "_opened"} | {
            k: v for k, v in meta.items() if k != "sequence"
        }
        if previous.get("selectable"):
            merged[key]["selectable"] = True
    return list(merged.values())


def artifacts(content: object, tool_name: str | None = None) -> list[dict[str, JsonValue]]:
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
                        "image_url": image_url(row[key], row.get("image_url")) if kind == "recipe" else None,
                        "base_serving": row.get("base_serving"),
                        "selectable": kind == "recipe" and tool_name in {"search_recipes", "get_recipes"},
                        "_opened": kind == "recipe" and tool_name == "get_recipes",
                        "minutes": row.get("total_minutes"),
                        "kcal": row.get("nutrition", {}).get("kcal"),
                        "total": row.get("subtotal_rub"),
                        "cost": (row.get("cost", {}) if kind == "recipe" else row.get("totals", {})).get(
                            "mean_rub"
                        ),
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
                cards.extend(artifacts(part.content, part.tool_name))
    return {"text": "\n".join(users), "output": "\n\n".join(texts), "artifacts": cards}


def execution_time(run: dict) -> dict[str, JsonValue]:
    """Раскрывает только длительность из отчёта, не usage или внутренние данные.

    :param run: Сохранённое веб-выполнение
    :return: Длительность либо null для старого хода без измерения
    """
    seconds = json.loads(run.get("report", "{}") or "{}").get("elapsed_seconds")
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        seconds = None
    return {"elapsed_seconds": seconds, "elapsed_is_partial": run.get("state") == "interrupted"}


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
        projected = project_messages(row["messages"])
        result.append(
            {
                "turn_id": row["turn_id"],
                "state": run.get("state", "complete"),
                **projected,
                "text": run.get("text", projected["text"]),
                "references": json.loads(run.get("references_json", "[]")),
                **execution_time(run),
            }
        )
    for row in run_map.values():
        result.append(
            {
                "turn_id": row["turn_id"],
                "state": row["state"],
                "text": row["text"],
                "references": json.loads(row["references_json"]),
                "output": row["output"],
                "artifacts": [],
                **execution_time(row),
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
    result["image_url"] = image_url(recipe_id, recipe.image_url)
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
