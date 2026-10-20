import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx


class Workflow:
    """Живой приёмочный сценарий через те же HTTP/SSE, что использует браузер."""

    def __init__(self, client: httpx.AsyncClient, output: Path) -> None:
        self.client, self.output = client, output
        self.report: dict = {"turns": [], "checks": {}}
        self.chat_id = ""

    async def request(self, method: str, route: str, **kwargs: object) -> object:
        """Проверяет HTTP-ответ и возвращает JSON."""
        response = await self.client.request(method, route, **kwargs)
        response.raise_for_status()
        return response.json()

    def save(self) -> None:
        """Сохраняет промежуточные результаты без cookies и паролей."""
        self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2))

    def check(self, name: str, condition: bool) -> None:
        """Фиксирует нарушение, продолжая проверку остальных независимых действий."""
        self.report["checks"][name] = bool(condition)
        self.save()

    async def turn(self, name: str, text: str, references: list[dict] | None = None) -> dict:
        """Отправляет обычное поручение и замеряет полный ход, включая guard."""
        turn_id = uuid.uuid4().hex
        started = time.monotonic()
        await self.request(
            "POST",
            f"/api/chats/{self.chat_id}/turns",
            json={"turn_id": turn_id, "text": text, "references": references or []},
        )
        final = {}
        async with self.client.stream(
            "GET", f"/api/chats/{self.chat_id}/turns/{turn_id}/events", timeout=660
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    final = json.loads(line[6:])
        record = {
            "name": name,
            "text": text,
            "wall_seconds": round(time.monotonic() - started, 2),
            **final,
        }
        self.report["turns"].append(record)
        self.save()
        print(
            json.dumps(
                {k: record.get(k) for k in ("name", "state", "wall_seconds", "output")}, ensure_ascii=False
            ),
            flush=True,
        )
        if final.get("state") != "complete":
            raise AssertionError(f"{name}: {final.get('state')}")
        return final

    async def run(self, args: argparse.Namespace) -> None:
        """Создаёт отдельный аккаунт с заданным профилем и проверяет операции."""
        profile = json.loads(args.profile.read_text())
        days, budget = args.days, profile.get("monthly_budget")
        login = "workflow-check-" + uuid.uuid4().hex[:10]
        me = await self.request(
            "POST",
            "/api/auth/register",
            json={"login": login, "password": uuid.uuid4().hex, "profile": profile},
        )
        chat = await self.request("POST", "/api/chats")
        self.chat_id = chat["chat_id"]
        self.report |= {"login": login, "user_id": me["user_id"], "chat_id": self.chat_id, "profile": profile}
        self.save()
        await self.turn(
            "plan", args.plan_request or f"Составь план питания на {days} дней с учётом моей анкеты."
        )
        plans = await self.request("GET", "/api/plans")
        if not plans:
            raise AssertionError("План не создан")
        plan = plans[0]
        self.report["plan"] = plan
        self.save()
        self.check("plan_ready", plan["status"] == "ok")
        self.check("all_days", len(plan["data"]["daily"]) == days)
        if args.energy_range:
            low, high = args.energy_range
            self.check(
                "requested_energy_range",
                all(low <= day["kcal"] <= high for day in plan["data"]["daily"].values()),
            )
        recipes = await self.request("GET", "/api/recipes")
        self.report["recipes"] = recipes
        ranks = {"easy": 0, "intermediate": 1, "advanced": 2}
        if profile.get("cooking_difficulty"):
            self.check(
                "difficulty",
                all(ranks[r["difficulty"]] <= ranks[profile["cooking_difficulty"]] for r in recipes),
            )
        if args.plan_only:
            return
        plan_ref = [{"kind": "plan", "id": plan["data"]["plan_id"]}]
        await self.turn("cart", "Собери покупки по этому меню.", plan_ref)
        carts = await self.request("GET", "/api/carts")
        cart = carts[0]
        self.report["cart"] = cart
        self.save()
        self.check("cart_ready", cart["status"] == "ok")
        self.check("cart_complete", cart["data"]["total_is_complete"] and bool(cart["data"]["cart_links"]))
        await self.turn(
            "replace",
            f"В этом списке покупок замени {args.replace_from} на {args.replace_to}, остальные товары сохрани.",
            [{"kind": "cart", "id": cart["data"]["cart_id"]}],
        )
        updated = (await self.request("GET", "/api/carts"))[0]
        self.report["updated_cart"] = updated
        self.save()
        self.check("replacement_ready", updated["status"] == "ok")
        self.check("replacement_version", updated["data"]["parent_id"] == cart["data"]["cart_id"])
        old_items = {r["product_id"]: r for r in cart["data"]["items"]}
        new_items = {r["product_id"]: r for r in updated["data"]["items"]}
        replaced_ids = {
            key
            for key, row in old_items.items()
            if args.match_from in row["product"]["title"].lower()
            and args.match_to not in row["product"]["title"].lower()
        }
        self.check(
            "requested_product_replaced",
            bool(replaced_ids)
            and all(
                key in new_items and args.match_to in new_items[key]["product"]["title"].lower()
                for key in replaced_ids
            ),
        )
        self.check(
            "other_items_preserved",
            all(
                key in new_items and row == new_items[key]
                for key, row in old_items.items()
                if key not in replaced_ids
            ),
        )
        recipe = recipes[0]
        ref = [{"kind": "recipe", "id": recipe["recipe_id"], "servings": 1}]
        await self.turn(
            "discuss",
            "Хочу обсудить этот рецепт. Сколько времени займёт приготовление и что можно подготовить заранее?",
            ref,
        )
        analogs = await self.turn(
            "analogs", f"Найди {args.analogs} похожих рецептов с учётом моей анкеты.", ref
        )
        self.check(
            "requested_analog_count",
            len({a["id"] for a in analogs["artifacts"] if a["kind"] == "recipe"}) == args.analogs,
        )
        self.check("analogs_are_other_recipes", ref[0]["id"] not in {a["id"] for a in analogs["artifacts"]})
        analog_ids = {a["id"] for a in analogs["artifacts"] if a["kind"] == "recipe"}
        self.report["analogs"] = await asyncio.gather(
            *(self.request("GET", f"/api/recipes/{recipe_id}") for recipe_id in sorted(analog_ids))
        )
        self.save()
        await self.turn(
            "recipe_cart", "Собери покупки для этого рецепта на две порции.", [ref[0] | {"servings": 2}]
        )
        self.report["recipe_cart"] = (await self.request("GET", "/api/carts"))[0]
        self.check("recipe_cart_ready", self.report["recipe_cart"]["status"] == "ok")
        await self.turn(
            "external_meal",
            f"Убери из этого плана приём пищи «{args.external_meal}» каждый день: я ем вне дома, "
            f"это {args.external_kcal:g} ккал и {args.external_cost:g} рублей ежедневно. "
            "Учти это при корректировке домашней части питания и бюджета.",
            plan_ref,
        )
        self.report["updated_plan"] = (await self.request("GET", "/api/plans"))[0]
        self.save()
        self.check("updated_plan_ready", self.report["updated_plan"]["status"] == "ok")
        self.check(
            "external_meal_removed",
            not any(
                args.external_meal.casefold() in m["meal"].casefold()
                for m in self.report["updated_plan"]["data"]["meals"]
            ),
        )
        self.check(
            "external_meal_energy_preserved",
            all(
                abs(day["kcal"] + args.external_kcal - plan["data"]["daily"][key]["kcal"])
                <= plan["data"]["daily"][key]["kcal"] * 0.1
                for key, day in self.report["updated_plan"]["data"]["daily"].items()
            ),
        )
        if budget:
            old_budget = plan["data"]["limits"]["budget_rub"]
            self.check(
                "external_meal_budget_reserved",
                self.report["updated_plan"]["data"]["limits"]["budget_rub"]
                == old_budget - days * args.external_cost,
            )


async def main() -> None:
    """Запускает явно запрошенные живые проверки с настоящими платными моделями."""
    parser = argparse.ArgumentParser(description="Живые вызовы LLM/MCP; создаёт отдельные тестовые аккаунты.")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--profile", type=Path, required=True, help="JSON анкеты")
    parser.add_argument("--plan-request", help="Полный текст поручения вместо стандартного")
    parser.add_argument("--energy-range", type=float, nargs=2, metavar=("MIN", "MAX"))
    parser.add_argument("--replace-from", default="обычное молоко")
    parser.add_argument("--replace-to", default="безлактозное молоко")
    parser.add_argument("--match-from", default="молоко", help="Фрагмент названия исходного товара")
    parser.add_argument("--match-to", default="безлактоз", help="Фрагмент названия замены")
    parser.add_argument("--analogs", type=int, default=5)
    parser.add_argument("--external-meal", default="обед")
    parser.add_argument("--external-kcal", type=float, default=500)
    parser.add_argument("--external-cost", type=float, default=300)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    failed = False
    for index in range(args.runs):
        async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
            workflow = Workflow(client, args.output / f"run-{index + 1}.json")
            try:
                await workflow.run(args)
            except (AssertionError, httpx.HTTPError, IndexError) as exc:
                workflow.report["failure"] = str(exc)
                workflow.save()
                failed = True
            failed |= not all(workflow.report["checks"].values())
    if failed:
        raise SystemExit("Есть непройденные проверки; см. сохранённые отчёты.")


if __name__ == "__main__":
    asyncio.run(main())
