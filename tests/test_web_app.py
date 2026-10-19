import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from app.config import Config
from app.integrations import IntegrationError
from app.models import CartLimits, PlanDraft, PlanLimits, Result, SkuAmount, SkusSource
from app.planning import evaluate
from app.shopping import Shopper
from app.web.contracts import Profile
from app.web.guard import GuardDecision
from app.web.presentation import artifacts, project_messages
from app.web.server import create_app
from app.web.storage import WebStore

PROFILE = dict(
    goal="healthier",
    sex="female",
    age=25,
    height_cm=170.0,
    weight_kg=60.0,
    monthly_budget="20-25",
    cooking_difficulty="easy",
    restrictions="Без рыбы",
)


class Products:
    def __init__(self):
        self.links = 0
        self.fail = False

    async def cart_link(self, quantities):
        self.links += 1
        if self.fail:
            raise IntegrationError("Сервис недоступен")
        return "https://example.org/cart"


class Guard:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.wait = None

    async def check(self, text, profile, recent, usage):
        usage.requests += 1
        self.calls.append((text, recent))
        if self.wait:
            await self.wait.wait()
        if self.fail:
            raise TimeoutError()
        return GuardDecision(allowed=text != "blocked", message="Запрос отклонён")


class Harness:
    def __init__(self, store, shopper):
        self.store, self.shopper = store, shopper
        self.calls = 0
        self.wait = None
        self.initial_usage = []

    async def run(self, user_id, chat_id, text, turn_id, emit, usage, task_seconds):
        self.calls += 1
        self.initial_usage.append(usage.requests)
        await emit({"type": "text_delta", "text": "Ответ"})
        if self.wait:
            await self.wait.wait()
        raw = ModelMessagesTypeAdapter.dump_json(
            [ModelRequest(parts=[UserPromptPart(text)]), ModelResponse(parts=[TextPart("Ответ")])]
        ).decode()
        await self.store.save_turn(chat_id, turn_id, raw, True)
        return {"text": "Ответ", "requests": usage.requests + 1, "error": None}


@pytest.fixture
async def web(store, catalog):
    webstore = WebStore(store.path)
    products = Products()
    harness = Harness(webstore, Shopper(webstore, catalog, products, None))
    guard = Guard()
    config = Config(_env_file=None, database_path=store.path, turn_timeout_seconds=10)
    app = create_app(config, store=webstore, catalog=catalog, session=harness, guard=guard)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/auth/register", json={"login": "x", "password": "1", "profile": PROFILE}
            )
            assert response.status_code == 200
            yield SimpleNamespace(
                client=client,
                store=webstore,
                harness=harness,
                guard=guard,
                products=products,
                app=app,
                uid=response.json()["user_id"],
                catalog=catalog,
            )


async def chat(web):
    return (await web.client.post("/api/chats")).json()["chat_id"]


async def complete(web, cid):
    await web.app.state.runs.tasks[cid]
    return (await web.client.get(f"/api/chats/{cid}/messages")).json()


async def test_registration_cookie_origin_and_isolation(web):
    assert (await web.client.get("/api/me")).json()["login"] == "x"
    response = await web.client.put("/api/profile", json=PROFILE, headers={"Origin": "http://attacker"})
    assert response.status_code == 403
    cid = await chat(web)
    duplicate = await web.client.post(
        "/api/auth/register", json={"login": "x", "password": "1", "profile": PROFILE}
    )
    assert duplicate.status_code == 409
    await web.client.post("/api/auth/logout")
    assert (await web.client.get("/api/me")).status_code == 401
    assert (
        await web.client.post("/api/auth/login", json={"login": "x", "password": "wrong"})
    ).status_code == 401
    other = await web.client.post(
        "/api/auth/register", json={"login": "y", "password": "a", "profile": PROFILE}
    )
    assert "HttpOnly" in other.headers["set-cookie"] and "SameSite=lax" in other.headers["set-cookie"]
    assert (await web.client.get(f"/api/chats/{cid}/messages")).status_code == 404
    assert (
        await web.client.post(f"/api/chats/{cid}/turns", json={"turn_id": "evil", "text": "Hi"})
    ).status_code == 404


@pytest.mark.parametrize("budget", [None, "15-20", "20-25", "25-30", "30-35", "35-40", "40-50", "unlimited"])
async def test_profile_budgets(web, budget):
    value = PROFILE | {"monthly_budget": budget}
    response = await web.client.put("/api/profile", json=value)
    assert response.status_code == 200
    assert response.json()["profile"]["monthly_budget"] == budget
    assert "budget_rub" not in response.json()["profile"]


@pytest.mark.parametrize(
    "field,value", [("age", 13), ("height_cm", 149.0), ("weight_kg", 201.0), ("monthly_budget", "50-60")]
)
def test_profile_validation(field, value):
    with pytest.raises(ValidationError):
        Profile(**(PROFILE | {field: value}))


async def test_guard_excludes_blocked_and_failures_from_history(web):
    cid = await chat(web)
    await web.client.post(f"/api/chats/{cid}/turns", json={"turn_id": "a", "text": "blocked"})
    messages = await complete(web, cid)
    assert messages[0]["text"] == "blocked" and messages[0]["state"] == "blocked"
    assert web.harness.calls == 0
    assert not (await web.store.history(cid))[2]
    web.guard.fail = True
    await web.client.post(f"/api/chats/{cid}/turns", json={"turn_id": "b", "text": "hello"})
    assert (await complete(web, cid))[-1]["state"] == "error"
    assert not (await web.store.history(cid))[2]
    web.guard.fail = False
    await web.client.post(f"/api/chats/{cid}/turns", json={"turn_id": "c", "text": "ok"})
    await complete(web, cid)
    assert web.guard.calls[-1][1] == [] and web.harness.initial_usage == [1]


async def test_active_run_idempotency_reconnect_cancel_delete(web):
    cid = await chat(web)
    web.harness.wait = asyncio.Event()
    body = {"turn_id": "a", "text": "hello"}
    await web.client.post(f"/api/chats/{cid}/turns", json=body)
    for _ in range(100):
        if web.harness.calls:
            break
        await asyncio.sleep(0.005)
    assert (await web.client.post(f"/api/chats/{cid}/turns", json=body)).status_code == 200
    assert (await web.client.post(f"/api/chats/{cid}/turns", json=body | {"turn_id": "b"})).status_code == 409
    assert (await web.client.get(f"/api/chats/{cid}/messages")).json()[-1]["output"] == "Ответ"
    assert web.harness.calls == 1
    await web.client.post(f"/api/chats/{cid}/stop")
    assert (await web.client.get(f"/api/chats/{cid}/messages")).json()[-1]["state"] == "cancelled"
    await web.client.post(f"/api/chats/{cid}/turns", json=body)
    assert web.harness.calls == 1
    response = await web.client.get(f"/api/chats/{cid}/turns/a/events")
    assert '"state": "cancelled"' in response.text
    await web.client.delete(f"/api/chats/{cid}")
    assert (await web.client.get(f"/api/chats/{cid}/messages")).status_code == 404
    assert await web.store.person(web.uid)


async def save_plan(web, recipe):
    draft = PlanDraft(meals=[dict(day=1, meal="Завтрак", recipe_id="r1", servings=1.0)], explanation="Меню")
    limits = PlanLimits(max_difficulty="easy", budget_rub=30.0)
    metrics, _ = evaluate(draft, {"r1": recipe}, limits)
    return await web.store.save_object(
        web.uid,
        "plan",
        draft.model_dump(mode="json")
        | metrics
        | {"days": 1, "limits": limits.model_dump(), "request": "test"},
        Result(),
        "seed",
    )


async def test_manual_plan_versions_budget_and_recipes(web, recipe):
    plan = await save_plan(web, recipe)
    pid = plan.data["plan_id"]
    invalid = await web.client.put(
        f"/api/plans/{pid}", json={"operation_id": "x", "meals": [{"index": 0, "servings": 0}]}
    )
    assert invalid.status_code == 422
    body = {"operation_id": "edit", "meals": [{"index": 0, "servings": 2.0}]}
    first = await web.client.put(f"/api/plans/{pid}", json=body)
    assert first.status_code == 200 and first.json()["status"] == "needs_revision"
    assert first.json()["data"]["totals"]["mean_rub"] == 40.0
    assert (await web.client.put(f"/api/plans/{pid}", json=body)).json() == first.json()
    assert (
        await web.client.put(f"/api/plans/{pid}", json=body | {"operation_id": "other"})
    ).status_code == 409
    assert (await web.store.get_object(web.uid, "plan", pid)) == plan
    assert len((await web.client.get("/api/plans")).json()) == 1
    recipes = (await web.client.get("/api/recipes")).json()
    assert [r["recipe_id"] for r in recipes] == ["r1"] and recipes[0]["ingredients"][0]["name"] == "Овсянка"
    assert web.harness.calls == 0


async def test_manual_cart_partial_link_failure_marks_and_empty(web):
    shopper = web.harness.shopper
    payload, issues, quantities = await shopper.assemble_skus(
        [SkuAmount(sku_id="sku", packages=1)], CartLimits(budget_rub=None)
    )
    payload["unresolved"] = [{"product_id": "oats", "recipe_ids": ["r1"], "reason": "Нет фасовки"}]
    cart = await shopper.save(
        web.uid,
        "seed-cart",
        SkusSource(type="skus", items=[SkuAmount(sku_id="sku", packages=1)]),
        CartLimits(budget_rub=None),
        payload,
        issues,
        quantities,
    )
    cid = cart.data["cart_id"]
    assert (
        await web.client.put(f"/api/carts/{cid}/purchased", json={"sku_id": "sku", "purchased": True})
    ).status_code == 200
    web.products.fail = True
    body = {"operation_id": "edit", "items": [{"sku_id": "sku", "packages": 1}], "unresolved_indices": [0]}
    response = await web.client.put(f"/api/carts/{cid}", json=body)
    data = response.json()["data"]
    assert data["cart_url"] is None and data["link_error"] and len(data["unresolved"]) == 1
    assert data["subtotal_rub"] is None
    assert (await web.client.get(f"/api/carts/{data['cart_id']}/purchased")).json()["purchased"] == ["sku"]
    calls = web.products.links
    assert (await web.client.put(f"/api/carts/{cid}", json=body)).json() == response.json()
    assert web.products.links == calls
    emptied = await web.client.put(
        f"/api/carts/{data['cart_id']}", json={"operation_id": "empty", "items": [], "unresolved_indices": []}
    )
    assert emptied.status_code == 200 and emptied.json()["data"]["items"] == []
    assert (await web.client.get(f"/api/carts/{emptied.json()['data']['cart_id']}/purchased")).json()[
        "purchased"
    ] == []


async def test_memory_conflict_and_restart(web):
    first = await web.client.put("/api/memory", json={"text": "Люблю овсянку", "revision": 0})
    assert first.status_code == 200
    assert (await web.client.put("/api/memory", json={"text": "wrong", "revision": 0})).status_code == 409
    assert (await web.client.put("/api/memory", json={"text": "", "revision": 1})).status_code == 200
    cid = await chat(web)
    async with web.store.connection() as db:
        await db.execute(
            "INSERT INTO web_runs(chat_id,turn_id,text,state,created_at) VALUES(?, 'crash', 'text', 'running', 1)",
            (cid,),
        )
        await db.commit()
    await web.store.initialize()
    rows = (await web.client.get(f"/api/chats/{cid}/messages")).json()
    assert rows[0]["state"] == "interrupted"
    assert not web.harness.calls


def test_projection_never_includes_reasoning_or_instructions():
    from pydantic_ai.messages import ThinkingPart

    raw = ModelMessagesTypeAdapter.dump_json(
        [
            ModelRequest(parts=[UserPromptPart("Hi")], instructions="secret"),
            ModelResponse(parts=[ThinkingPart("hidden"), TextPart("Hello")]),
        ]
    ).decode()
    view = project_messages(raw)
    assert view == {"text": "Hi", "output": "Hello", "artifacts": []}
    assert "secret" not in json.dumps(view) and "hidden" not in json.dumps(view)


async def test_recipe_results_have_cards_live_and_after_reload(catalog):
    for result in [await catalog.search("Овсянка"), await catalog.get(["r1"])]:
        recipes = result.data["recipes"]
        assert recipes
        expected = [r["recipe_id"] for r in recipes]
        assert [c["id"] for c in artifacts(result)] == expected
        raw = ModelMessagesTypeAdapter.dump_json(
            [ModelRequest(parts=[ToolReturnPart("search_recipes", result.model_dump(mode="json"), "c1")])]
        ).decode()
        cards = project_messages(raw)["artifacts"]
        assert [c["id"] for c in cards] == expected
        assert all(c["kind"] == "recipe" and c["title"] for c in cards)


def test_artifacts_do_not_parse_arbitrary_nested_data():
    assert artifacts(Result(data={"unrelated": [{"recipe_id": "wrong"}]})) == []
    assert artifacts("Мне нравится recipe_id: invented") == []


async def test_cancel_before_task_starts_is_terminal(web):
    cid = await chat(web)
    runs = web.app.state.runs
    await runs.start(web.uid, cid, "queued-stop", "Hello")
    await runs.cancel(cid)
    assert runs.live[cid]["state"] == "cancelled"
    assert (await web.store.history(cid))[2] == []
    assert (await runs.start(web.uid, cid, "queued-stop", "Hello"))["state"] == "cancelled"
    assert web.harness.calls == 0


async def test_current_version_follows_latest_branch(web):
    root = await web.store.save_object(web.uid, "plan", {}, Result(), "root")
    left = await web.store.save_object(web.uid, "plan", {}, Result(), "left", root.data["plan_id"])
    right = await web.store.save_object(web.uid, "plan", {}, Result(), "right", root.data["plan_id"])
    latest = await web.store.latest(web.uid, "plan")
    assert len(latest) == 1 and latest[0]["data"]["plan_id"] == right.data["plan_id"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await web.store.require_current(web.uid, "plan", left.data["plan_id"])
    assert error.value.status_code == 409
    rejected = await web.store.save_object(
        web.uid, "plan", {}, Result(), "web:plan:stale", left.data["plan_id"]
    )
    assert rejected.error.code == "VERSION_CONFLICT"


async def test_cancelled_run_keeps_compaction_metrics(web):
    async def stopped(*args, **kwargs):
        await args[4](
            {
                "type": "execution_report",
                "report": {"requests": 3, "cost_usd": 0.02, "compaction": {"requests": 1, "cost_usd": 0.01}},
            }
        )
        raise asyncio.CancelledError()

    web.harness.run = stopped
    cid = await chat(web)
    await web.app.state.runs.start(web.uid, cid, "cost", "Hello")
    await web.app.state.runs.tasks[cid]
    async with web.store.connection() as db:
        row = await (await db.execute("SELECT report,state FROM web_runs WHERE chat_id=?", (cid,))).fetchone()
    report = json.loads(row["report"])
    assert row["state"] == "cancelled" and report["requests"] == 3
    assert report["cost_usd"] == 0.02 and report["compaction"]["requests"] == 1
    assert report["guard"]["requests"] == 1
