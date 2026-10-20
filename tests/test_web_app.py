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
from app.integrations.vkusvill import IntegrationError
from app.models import CartLimits, PlanDraft, PlanLimits, Result, SkuAmount, SkusSource
from app.planning.calculations import evaluate
from app.shopping.service import Shopper
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


async def test_recipe_results_have_cards_live_and_after_reload(catalog, no_recipe_limits):
    for result in [await catalog.search("Овсянка", no_recipe_limits), await catalog.get(["r1"])]:
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


async def test_progress_reconnect_total_time_and_parallel_tools(web):
    ready, release = asyncio.Event(), asyncio.Event()
    original = web.harness.run

    async def progressing(*args, **kwargs):
        emit = args[4]
        await emit({"type": "tool_start", "name": "create_cart", "call_id": "cart"})
        await emit(
            {
                "type": "progress",
                "name": "create_cart",
                "call_id": "cart",
                "stage": "cart_search",
                "completed": 2,
                "total": 3,
                "found": 1,
            }
        )
        await emit({"type": "tool_start", "name": "get_plan", "call_id": "plan"})
        await emit({"type": "tool_end", "name": "get_plan", "call_id": "plan", "content": {}})
        ready.set()
        await release.wait()
        await emit({"type": "tool_end", "name": "create_cart", "call_id": "cart", "content": {}})
        return await original(*args, **kwargs)

    web.harness.run = progressing
    cid = await chat(web)
    runs = web.app.state.runs
    await runs.start(web.uid, cid, "progress", "Корзина")
    await asyncio.wait_for(ready.wait(), 2)
    runs.started[cid] -= 61
    before = runs.started[cid]
    await runs.start(web.uid, cid, "progress", "Корзина")
    assert runs.started[cid] == before
    rows = (await web.client.get(f"/api/chats/{cid}/messages")).json()
    assert rows[-1]["progress"]["stage"] == "cart_search"
    assert rows[-1]["progress"]["found"] == 1
    assert rows[-1]["elapsed_seconds"] >= 61
    stream = runs.events(cid, "progress")
    snapshot = json.loads((await anext(stream)).removeprefix("data: "))
    await stream.aclose()
    assert snapshot["progress"] == rows[-1]["progress"]
    assert snapshot["elapsed_seconds"] >= rows[-1]["elapsed_seconds"]
    release.set()
    final = (await complete(web, cid))[-1]
    assert final["elapsed_seconds"] >= 61 and final["state"] == "complete"
    assert runs.live[cid]["progress"] is None
    assert cid not in runs.started
    runs.live.clear()
    restored = (await web.client.get(f"/api/chats/{cid}/messages")).json()[-1]
    assert restored["elapsed_seconds"] == final["elapsed_seconds"]
    assert "report" not in restored
    sse = await web.client.get(f"/api/chats/{cid}/turns/progress/events")
    assert json.loads(sse.text.removeprefix("data: "))["elapsed_seconds"] == final["elapsed_seconds"]


async def test_terminal_sse_waits_for_saved_status(web, monkeypatch):
    ready, release = asyncio.Event(), asyncio.Event()
    runs = web.app.state.runs
    original = runs._persist

    async def delayed(chat_id, report=None):
        if runs.live[chat_id]["state"] == "complete":
            ready.set()
            await release.wait()
        await original(chat_id, report)

    monkeypatch.setattr(runs, "_persist", delayed)
    cid = await chat(web)
    await runs.start(web.uid, cid, "terminal", "Hello")
    await asyncio.wait_for(ready.wait(), 2)
    stream = runs.events(cid, "terminal")
    event = asyncio.create_task(anext(stream))
    try:
        done, _ = await asyncio.wait({event}, timeout=0.05)
        assert not done
        release.set()
        snapshot = json.loads((await asyncio.wait_for(event, 2)).removeprefix("data: "))
        assert snapshot["state"] == "complete"
        assert not (await web.store.chats(web.uid))[0]["active"]
    finally:
        release.set()
        await runs.tasks[cid]
        await event
        await stream.aclose()


async def test_elapsed_time_stops_on_cancellation_and_guard_failure(web):
    cid = await chat(web)
    web.guard.wait = asyncio.Event()
    runs = web.app.state.runs
    await runs.start(web.uid, cid, "cancel-clock", "Hello")
    runs.started[cid] -= 7
    await runs.cancel(cid)
    first = (await web.client.get(f"/api/chats/{cid}/messages")).json()[-1]
    second = (await web.client.get(f"/api/chats/{cid}/messages")).json()[-1]
    assert first["state"] == "cancelled" and first["elapsed_seconds"] >= 7
    assert second["elapsed_seconds"] == first["elapsed_seconds"]
    web.guard.wait = None
    web.guard.fail = True
    await runs.start(web.uid, cid, "guard-error", "Hello")
    final = (await complete(web, cid))[-1]
    assert final["state"] == "error" and final["elapsed_seconds"] is not None
    assert cid not in runs.started


async def test_interrupted_timing_is_partial_and_old_unknown_time_is_absent(web):
    cid = await chat(web)
    async with web.store.connection() as db:
        await db.executemany(
            "INSERT INTO web_runs(chat_id,turn_id,text,state,created_at,report) VALUES(?,?,?, 'running',1,?)",
            [(cid, "checkpoint", "a", '{"elapsed_seconds": 12.5}'), (cid, "old", "b", "{}")],
        )
        await db.commit()
    await web.store.initialize()
    rows = {r["turn_id"]: r for r in (await web.client.get(f"/api/chats/{cid}/messages")).json()}
    assert rows["checkpoint"]["elapsed_seconds"] == 12.5
    assert rows["checkpoint"]["elapsed_is_partial"] is True
    assert rows["old"]["elapsed_seconds"] is None


async def test_manual_weighted_quantity_preserves_units_versions_and_replay(web):
    from app.models import Product

    await web.store.cache_skus(
        [
            Product(
                sku_id="weight",
                title="Индейка",
                price_rub=928,
                sale_unit="kg",
                package_amount=1000,
                package_unit="g",
            ),
            Product(sku_id="pack", title="Пачка", price_rub=100),
        ]
    )
    source = SkusSource(
        type="skus", items=[SkuAmount(sku_id="weight", packages=0.25), SkuAmount(sku_id="pack", packages=1)]
    )
    shopper = web.harness.shopper
    limits = CartLimits(budget_rub=None)
    payload, issues, quantities = await shopper.assemble_skus(source.items, limits)
    old = await shopper.save(web.uid, "weight-seed", source, limits, payload, issues, quantities)
    cid = old.data["cart_id"]
    body = {
        "operation_id": "weight-edit",
        "items": [{"sku_id": "weight", "packages": 0.29}, {"sku_id": "pack", "packages": 0.5}],
        "unresolved_indices": [],
    }
    assert (await web.client.put(f"/api/carts/{cid}", json=body)).status_code == 422
    body["items"][1]["packages"] = 1
    response = await web.client.put(f"/api/carts/{cid}", json=body)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"][0]["packages"] == 0.29
    assert data["items"][0]["product"]["sale_unit"] == "kg"
    assert data["subtotal_rub"] == 369.12
    assert (await web.client.put(f"/api/carts/{cid}", json=body)).json() == response.json()
    assert await web.store.get_object(web.uid, "cart", cid) == old


async def test_titles_hidden_chains_and_ownership(web, recipe):
    plan = await save_plan(web, recipe)
    pid = plan.data["plan_id"]
    async with web.store.connection() as db:
        # 19 Oct 21:30 UTC is 20 Oct in Moscow.
        await db.execute("UPDATE object_meta SET created_at=? WHERE root_id=?", (1792445400, pid))
        await db.commit()
    view = (await web.client.get(f"/api/plans/{pid}")).json()["data"]
    assert view["title"] == "Меню на 1 день · 20.10.2026"
    for bad in ["  ", "x" * 81]:
        assert (await web.client.patch(f"/api/plans/{pid}/title", json={"title": bad})).status_code == 422
    assert (
        await web.client.patch(f"/api/plans/{pid}/title", json={"title": "  Моё   меню\nна неделю  "})
    ).json() == {"title": "Моё меню на неделю"}
    for _ in range(2):
        assert (await web.client.post(f"/api/plans/{pid}/hide")).status_code == 200
    assert (await web.client.get("/api/plans")).json() == []
    assert (await web.client.get("/api/recipes")).json() == []
    assert (await web.store.list_objects(web.uid, "plan")).data == []
    body = {"operation_id": "hidden-edit", "meals": [{"index": 0, "servings": 1.5}]}
    edited = (await web.client.put(f"/api/plans/{pid}", json=body)).json()
    assert edited["data"]["hidden"] and edited["data"]["title"] == "Моё меню на неделю"
    assert edited["data"]["totals"]["mean_rub"] == 30
    assert (await web.client.put(f"/api/plans/{pid}", json=body)).json() == edited
    assert (await web.store.get_object(web.uid, "plan", pid)) == plan
    assert (await web.store.replay(web.uid, "seed")) == plan
    assert (await web.client.get("/api/plans")).json() == []
    # A separate user's versions cannot be renamed, hidden or opened.
    foreign = await web.store.save_object("other", "plan", {}, Result(), "foreign")
    fid = foreign.data["plan_id"]
    assert (await web.client.patch(f"/api/plans/{fid}/title", json={"title": "no"})).status_code == 404
    assert (await web.client.post(f"/api/plans/{fid}/hide")).status_code == 404
    assert (await web.client.get(f"/api/plans/{fid}")).status_code == 404
    async with web.store.connection() as db:
        await db.execute("UPDATE object_meta SET created_at=NULL,title=NULL WHERE root_id=?", (pid,))
        await db.commit()
    await web.store.initialize()
    assert (await web.client.get(f"/api/plans/{pid}")).json()["data"]["title"] == "Меню на 1 день"


async def test_cart_title_survives_independent_manual_edit_and_plan_hide(web, recipe):
    from app.models import PlanSource

    plan = await save_plan(web, recipe)
    pid = plan.data["plan_id"]
    await web.client.patch(f"/api/plans/{pid}/title", json={"title": "Завтраки"})
    shopper = web.harness.shopper
    payload, issues, quantities = await shopper.assemble_skus(
        [SkuAmount(sku_id="sku", packages=1)], CartLimits(budget_rub=None)
    )
    cart = await shopper.save(
        web.uid,
        "cart-title",
        PlanSource(type="plan", plan_id=pid),
        CartLimits(budget_rub=None),
        payload,
        issues,
        quantities,
    )
    cid = cart.data["cart_id"]
    await web.client.post(f"/api/plans/{pid}/hide")
    assert (await web.client.get("/api/carts")).json()[0]["data"]["title"] == "Завтраки"
    body = {
        "operation_id": "title-edit",
        "items": [{"sku_id": "sku", "packages": 2}],
        "unresolved_indices": [],
    }
    edited = (await web.client.put(f"/api/carts/{cid}", json=body)).json()["data"]
    assert edited["source"]["type"] == "skus" and edited["title"] == "Завтраки"
    await web.client.patch(f"/api/carts/{edited['cart_id']}/title", json={"title": "Моя покупка"})
    assert (await web.client.get(f"/api/carts/{cid}")).json()["data"]["title"] == "Моя покупка"
    await web.client.post(f"/api/carts/{cid}/hide")
    assert (await web.client.get("/api/carts")).json() == []
    assert (await web.store.list_objects(web.uid, "cart")).data == []


async def test_card_projection_resolves_missing_ancestor_and_preserves_old_turn(web, recipe):
    from app.web.presentation import project_cards

    root = await save_plan(web, recipe)
    middle = await web.store.save_object(web.uid, "plan", root.data, Result(), "v2", root.data["plan_id"])
    last = await web.store.save_object(web.uid, "plan", middle.data, Result(), "v3", middle.data["plan_id"])
    independent = await web.store.save_object(web.uid, "plan", root.data, Result(), "independent")
    cid = await chat(web)

    def raw(results):
        return ModelMessagesTypeAdapter.dump_json(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart("test"),
                        *[
                            ToolReturnPart("get_plan", r.model_dump(mode="json"), str(i))
                            for i, r in enumerate(results)
                        ],
                    ]
                )
            ]
        ).decode()

    await web.store.save_turn(cid, "old", raw([root]), True)
    await web.store.save_turn(cid, "new", raw([root, last, independent]), True)
    expected = await project_cards(
        web.store, web.catalog, web.uid, artifacts(root) + artifacts(last) + artifacts(independent)
    )
    assert [c["id"] for c in expected] == [last.data["plan_id"], independent.data["plan_id"]]
    backwards = await project_cards(web.store, web.catalog, web.uid, artifacts(last) + artifacts(root))
    assert backwards[0]["id"] == last.data["plan_id"]

    rows = (await web.client.get(f"/api/chats/{cid}/messages")).json()
    assert rows[0]["artifacts"][0]["id"] == root.data["plan_id"]
    assert rows[1]["artifacts"] == expected
    async with web.store.connection() as db:
        await db.execute(
            "INSERT INTO web_runs(chat_id,turn_id,text,state,created_at) VALUES(?, 'new', 'test', 'complete', 1)",
            (cid,),
        )
        await db.commit()
    event = await web.client.get(f"/api/chats/{cid}/turns/new/events")
    snapshot = json.loads(next(line[6:] for line in event.text.splitlines() if line.startswith("data: ")))
    assert snapshot["artifacts"] == expected


async def test_recipe_image_delivery_limits_cache_and_sources(web, recipe, monkeypatch):
    from app.web.presentation import image_source, image_url

    source = "https://vkusvill.ru/upload/recipe.jpg"
    monkeypatch.setattr(web.catalog, "recipes_by_ids", lambda ids: image_recipes())

    async def image_recipes():
        return {"r1": recipe.model_copy(update={"image_url": source})}

    requested = []
    response_kind = "ok"

    async def serve(request):
        requested.append(str(request.url))
        if response_kind == "timeout":
            raise httpx.ReadTimeout("timeout")
        if response_kind == "type":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        if response_kind == "large":
            return httpx.Response(
                200, content=b"a" * (8 * 1024 * 1024 + 1), headers={"content-type": "image/webp"}
            )
        return httpx.Response(200, content=b"photo", headers={"content-type": "image/webp"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        web.app.state.media_client = client
        result = await web.client.get("/api/recipes/r1/image")
        assert result.status_code == 200 and result.content == b"photo"
        assert result.headers["cache-control"] == "private, max-age=86400"
        assert requested[0].startswith("https://wsrv.nl/?") and "w=960" in requested[0]
        for response_kind in ["timeout", "type", "large"]:
            result = await web.client.get("/api/recipes/r1/image")
            assert result.status_code == 502 and result.headers["cache-control"] == "no-store"
        source = "https://images.gastronom.ru/a.jpg"
        response_kind = "ok"
        await web.client.get("/api/recipes/r1/image")
        assert requested[-1] == source
    assert image_url("r1", source) != image_url("r1", source + "?changed")
    assert image_source("https://example.org/a.jpg") is None
    assert image_source("http://images.gastronom.ru/a.jpg") is None
    await web.client.post("/api/auth/logout")
    assert (await web.client.get("/api/recipes/r1/image")).status_code == 401


async def test_opened_recipes_are_final_selection_for_live_and_reload(web):
    from app.web.presentation import project_cards

    search = Result(
        data={
            "recipes": [
                {"recipe_id": "r1", "title": "Первое"},
                {"recipe_id": "r2", "title": "Второе"},
            ]
        }
    )
    chosen = Result(data={"recipes": [{"recipe_id": "r2", "title": "Второе"}]})
    calls = [("search_recipes", search), ("get_recipes", chosen), ("search_recipes", search)]
    live = [card for tool, result in calls for card in artifacts(result, tool)]
    raw = ModelMessagesTypeAdapter.dump_json(
        [
            ModelRequest(parts=[ToolReturnPart(tool, result.model_dump(mode="json"), f"c{i}")])
            for i, (tool, result) in enumerate(calls)
        ]
    ).decode()
    expected = await project_cards(web.store, web.catalog, web.uid, live)
    assert [c["id"] for c in expected] == ["r2"]
    assert expected[0]["selectable"] and "_opened" not in expected[0]
    assert (
        await project_cards(web.store, web.catalog, web.uid, project_messages(raw)["artifacts"]) == expected
    )
    assert len(await project_cards(web.store, web.catalog, web.uid, artifacts(search, "search_recipes"))) == 2


@pytest.mark.parametrize("activity", [None, "inactive", "low_active", "active", "very_active"])
async def test_optional_activity_roundtrip(web, activity):
    profile = PROFILE | {"activity": activity}
    response = await web.client.put("/api/profile", json=profile)
    assert response.status_code == 200
    assert response.json()["profile"]["activity"] == activity
    assert (await web.store.person(web.uid)).questionnaire["activity"] == activity
    for required in ("goal", "sex", "age", "height_cm", "weight_kg"):
        assert (
            await web.client.put("/api/profile", json={k: v for k, v in profile.items() if k != required})
        ).status_code == 422


async def test_references_exact_hidden_version_history_and_replay(web, recipe):
    plan = await save_plan(web, recipe)
    pid = plan.data["plan_id"]
    await web.client.patch(f"/api/plans/{pid}/title", json={"title": "Наше меню"})
    await web.client.put(
        f"/api/plans/{pid}", json={"operation_id": "later", "meals": [{"index": 0, "servings": 1.5}]}
    )
    await web.client.post(f"/api/plans/{pid}/hide")
    cid = await chat(web)
    body = {
        "turn_id": "reference",
        "text": "Собери покупки по этому меню.",
        "references": [{"kind": "plan", "id": pid}, {"kind": "recipe", "id": "r1", "servings": 1.5}],
    }
    assert (await web.client.post(f"/api/chats/{cid}/turns", json=body)).status_code == 200
    row = (await complete(web, cid))[-1]
    assert row["text"] == body["text"] and pid not in row["text"]
    assert row["references"][0] == {"kind": "plan", "id": pid, "title": "Наше меню"}
    assert row["references"][1]["servings"] == 1.5
    _, _, history = await web.store.history(cid)
    assert pid in history[-1]["messages"] and "Объекты поручения" in history[-1]["messages"]
    assert web.guard.calls[-1][0] == body["text"]
    assert (await web.client.post(f"/api/chats/{cid}/turns", json=body)).status_code == 200
    assert web.harness.calls == 1
    # Проверяем сохранённый SSE-снимок после удаления in-process состояния.
    web.app.state.runs.live.clear()
    events = await web.client.get(f"/api/chats/{cid}/turns/reference/events")
    snapshot = json.loads(events.text.removeprefix("data: ").strip())
    assert snapshot["references"] == row["references"]


async def test_reference_access_and_invalid_values_before_execution(web, recipe):
    plan = await save_plan(web, recipe)
    pid = plan.data["plan_id"]
    await web.client.post(
        "/api/auth/register", json={"login": "other-ref", "password": "x", "profile": PROFILE}
    )
    cid = await chat(web)
    for ref, status in [
        ({"kind": "plan", "id": pid}, 404),
        ({"kind": "recipe", "id": "missing"}, 404),
        ({"kind": "recipe", "id": "r1", "servings": 0}, 422),
        ({"kind": "recipe", "id": "r1", "servings": True}, 422),
        ({"kind": "recipe", "id": "r1", "title": "Подменённое название"}, 422),
    ]:
        assert (
            await web.client.post(
                f"/api/chats/{cid}/turns", json={"turn_id": "invalid", "text": "Обсудим", "references": [ref]}
            )
        ).status_code == status
    assert web.harness.calls == 0 and not web.guard.calls


async def test_sse_delivers_terminal_snapshot_when_turn_finishes_during_card_projection(web, monkeypatch):
    release = asyncio.Event()
    web.harness.wait = release
    cid = await chat(web)
    runs = web.app.state.runs
    await runs.start(web.uid, cid, "projection-race", "Hello")
    async with asyncio.timeout(2):
        while not web.harness.calls:
            await asyncio.sleep(0)

    async def finish_during_projection(store, catalog, user_id, cards):
        if asyncio.current_task() is not runs.tasks[cid]:
            release.set()
            await runs.tasks[cid]
        return cards

    monkeypatch.setattr("app.web.runs.project_cards", finish_during_projection)
    stream = runs.events(cid, "projection-race")
    try:
        first = json.loads((await anext(stream)).removeprefix("data: "))
        assert first["state"] == "running" and runs.live[cid]["state"] == "complete"
        final = json.loads((await asyncio.wait_for(anext(stream), 2)).removeprefix("data: "))
        assert final["state"] == "complete"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        release.set()
        await runs.tasks[cid]
        await stream.aclose()
