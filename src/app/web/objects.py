import asyncio

from fastapi import HTTPException
from pydantic import JsonValue

from app.catalog.store import Catalog
from app.integrations.vkusvill import IntegrationError
from app.models import CartLimits, PlanDraft, PlanLimits, Result, SkuAmount, StoredSkusSource
from app.planning.calculations import evaluate
from app.shopping.service import Shopper
from app.web.contracts import CartEdit, PlanEdit
from app.web.storage import WebStore


class ObjectEditor:
    """Применяет ограниченные ручные изменения без модельных вызовов."""

    def __init__(self, store: WebStore, catalog: Catalog, shopper: Shopper) -> None:
        self.store, self.catalog, self.shopper = store, catalog, shopper
        self.locks: dict[str, asyncio.Lock] = {}

    async def plan(self, user_id: str, object_id: str, edit: PlanEdit) -> Result:
        async with self.locks.setdefault(object_id, asyncio.Lock()):
            key = f"web:plan:{object_id}:{edit.operation_id}"
            previous = await self.store.replay(user_id, key)
            if previous:
                return previous
            current = await self.store.require_current(user_id, "plan", object_id)
            indices = [m.index for m in edit.meals]
            if len(set(indices)) != len(indices) or any(i >= len(current["meals"]) for i in indices):
                raise HTTPException(422, "Строки плана изменились; откройте актуальную версию")
            meals = [current["meals"][m.index] | {"servings": m.servings} for m in edit.meals]
            draft = PlanDraft(meals=meals, explanation=current["explanation"])
            recipes = await self.catalog.recipes_by_ids([m.recipe_id for m in draft.meals])
            metrics, issues = evaluate(draft, recipes, PlanLimits.model_validate(current["limits"]))
            payload = {k: v for k, v in current.items() if k not in {"plan_id", "parent_id", "status"}}
            return await self.store.save_object(
                user_id,
                "plan",
                payload | draft.model_dump(mode="json") | metrics,
                Result(status="needs_revision" if issues else "ok", issues=issues),
                key,
                object_id,
            )

    async def cart(self, user_id: str, object_id: str, edit: CartEdit) -> Result:
        async with self.locks.setdefault(object_id, asyncio.Lock()):
            key = f"web:cart:{object_id}:{edit.operation_id}"
            previous = await self.store.replay(user_id, key)
            if previous:
                return previous
            current = await self.store.require_current(user_id, "cart", object_id)
            allowed = {(i.get("sku_id") or (i.get("product") or {}).get("sku_id")) for i in current["items"]}
            ids = [i.sku_id for i in edit.items]
            if len(set(ids)) != len(ids) or not set(ids) <= allowed:
                raise HTTPException(422, "Добавьте или замените товар через ассистента")
            indices = edit.unresolved_indices
            if len(set(indices)) != len(indices) or any(
                i < 0 or i >= len(current["unresolved"]) for i in indices
            ):
                raise HTTPException(422, "Некорректные нерешённые позиции")
            limits = CartLimits.model_validate(current["limits"])
            items = [SkuAmount(**i.model_dump()) for i in edit.items]
            try:
                payload, issues, quantities = await self.shopper.assemble_skus(
                    items, limits, current["items"]
                )
            except IntegrationError as exc:
                raise HTTPException(422, str(exc)) from exc
            payload["unresolved"] = [current["unresolved"][i] for i in indices]
            if payload["unresolved"]:
                payload["total_is_complete"] = False
            # Пустая независимая покупка тоже сохраняется; публичный create_cart требует min_length=1.
            source = StoredSkusSource(type="skus", items=items)
            return await self.shopper.save(
                user_id, key, source, limits, payload, issues, quantities, object_id
            )

    async def marks(self, user_id: str, cart_id: str) -> list[str]:
        result = await self.store.get_object(user_id, "cart", cart_id)
        if not result.data:
            raise HTTPException(404, "Корзина не найдена")
        async with self.store.connection() as db:
            rows = await (
                await db.execute("SELECT sku_id FROM purchased WHERE object_id=?", (cart_id,))
            ).fetchall()
        return [r["sku_id"] for r in rows]

    async def mark(self, user_id: str, cart_id: str, sku_id: str, purchased: bool) -> dict[str, JsonValue]:
        current = await self.store.require_current(user_id, "cart", cart_id)
        if sku_id not in {
            (i.get("sku_id") or (i.get("product") or {}).get("sku_id")) for i in current["items"]
        }:
            raise HTTPException(404, "Товар не найден")
        async with self.store.connection() as db:
            if purchased:
                await db.execute("INSERT OR IGNORE INTO purchased VALUES(?,?)", (cart_id, sku_id))
            else:
                await db.execute("DELETE FROM purchased WHERE object_id=? AND sku_id=?", (cart_id, sku_id))
            await db.commit()
        return {"purchased": await self.marks(user_id, cart_id)}
