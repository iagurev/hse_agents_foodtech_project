import asyncio

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from app.assistant.agent import build_agent
from app.assistant.compaction import history_for_model
from app.assistant.session import Session
from app.config import Config
from app.models import Result


class NoProducts:
    async def search(self, query: str, limit: int) -> list:
        raise AssertionError("Unexpected product search")


class SummaryGenerator:
    def __init__(self) -> None:
        self.inputs = []

    async def compact(self, payload: dict, usage: RunUsage, **kwargs) -> str:
        self.inputs.append(payload)
        return "Активен plan_1. Только на этот план бюджет 5500; обычный — 5000."


async def test_memory_conflict_and_idempotence(store) -> None:
    first = await store.update_memory("u", 0, "Не ем рыбу", "memory-op")
    assert first.status == "ok"
    assert (await store.update_memory("u", 0, "Не ем рыбу", "memory-op")) == first
    conflict = await store.update_memory("u", 0, "Есть духовка")
    assert conflict.error.code == "MEMORY_CONFLICT"
    assert conflict.data["memory"] == "Не ем рыбу"
    merged = await store.update_memory("u", 1, "Не ем рыбу. Есть духовка.")
    assert merged.data["memory_revision"] == 2
    await store.ensure_chat("u", "other")
    assert (await store.person("u")).memory == merged.data["memory"]
    await store.update_memory("u", 2, "Есть духовка.")
    assert (await store.person("u")).memory == "Есть духовка."
    with pytest.raises(PermissionError):
        await store.ensure_chat("another_user", "c")


async def test_atomic_duplicate_and_owner_access(store) -> None:
    results = await asyncio.gather(
        *(store.save_object("u", "plan", {"meals": []}, Result(), "same") for _ in range(2))
    )
    assert results[0] == results[1]
    assert len((await store.list_objects("u", "plan")).data) == 1
    denied = await store.get_object("other", "plan", results[0].data["plan_id"])
    assert denied.error.code == "NOT_FOUND"


def test_reasoning_removed_from_completed_history_only() -> None:
    raw = [
        ModelRequest(parts=[UserPromptPart("Список")]),
        ModelResponse(parts=[ThinkingPart("opaque reasoning"), ToolCallPart("list_plans", {}, "call")]),
        ModelRequest(parts=[ToolReturnPart("list_plans", [], "call")]),
        ModelResponse(parts=[TextPart("Планов нет")]),
    ]
    copy = history_for_model(raw)
    assert isinstance(raw[1].parts[0], ThinkingPart)
    assert not any(isinstance(p, ThinkingPart) for m in copy for p in m.parts)
    assert copy[1].parts[0].tool_call_id == copy[2].parts[0].tool_call_id
    assert ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(copy)) == copy


async def test_real_sdk_loop_and_chat_isolation(store, catalog, tmp_path, counter) -> None:
    seen = []

    def respond(messages, info):
        seen.append(messages)
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(
                parts=[ThinkingPart("service data"), ToolCallPart("list_plans", {}, "call_1")]
            )
        return ModelResponse(parts=[TextPart("Планов пока нет.")])

    generator = SummaryGenerator()
    session = Session(
        Config(_env_file=None, database_path=tmp_path / "unused"),
        build_agent(FunctionModel(respond)),
        store,
        catalog,
        NoProducts(),
        generator,
        counter=counter,
    )
    result = await session.run("u", "c", "Покажи планы", "delivery")
    assert result["text"] == "Планов пока нет."
    assert result["requests"] == 2
    assert any(isinstance(p, ThinkingPart) for m in seen[1] for p in m.parts)
    replay = await session.run("u", "c", "Покажи планы", "delivery")
    assert replay["replayed"]
    assert len(seen) == 2
    await session.run("u", "c", "А теперь?", "next")
    assert not any(isinstance(p, ThinkingPart) for m in seen[2] for p in m.parts)
    await session.run("u", "new", "Покажи планы", "new")
    users = [p.content for m in seen[-2] for p in m.parts if isinstance(p, UserPromptPart)]
    assert "А теперь?" not in users


async def test_timeout_does_not_claim_success(store, catalog, counter) -> None:
    async def slow(messages, info):
        await asyncio.sleep(1)
        return ModelResponse(parts=[TextPart("done")])

    session = Session(
        Config(_env_file=None, turn_timeout_seconds=0.01),
        build_agent(FunctionModel(slow)),
        store,
        catalog,
        NoProducts(),
        SummaryGenerator(),
        counter=counter,
    )
    result = await session.run("u", "c", "Запрос")
    assert result["error"] == "TimeoutError"
    assert "Не удалось" in result["text"]
    assert len((await store.history("c"))[2]) == 1


async def test_questionnaire_before_first_chat_is_retained(store) -> None:
    await store.set_questionnaire("new_user", {"goal": "устойчивая цель"})
    await store.ensure_chat("new_user", "fresh")
    assert (await store.person("new_user")).questionnaire == {"goal": "устойчивая цель"}


async def test_stream_continues_through_tool_and_emits_first_text(store, catalog, counter) -> None:
    from pydantic_ai.models.function import DeltaToolCall

    async def stream(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Проверю. "
            yield {0: DeltaToolCall(name="list_plans", json_args="{}", tool_call_id="stream_call")}
        else:
            yield "Планов "
            yield "нет."

    events = []

    async def emit(event):
        events.append(event)

    session = Session(
        Config(_env_file=None),
        build_agent(FunctionModel(stream_function=stream)),
        store,
        catalog,
        NoProducts(),
        SummaryGenerator(),
        counter=counter,
    )
    result = await session.run("u", "c", "Покажи планы", emit=emit)
    assert result["error"] is None
    assert result["text"] == "Планов нет."
    assert [e["type"] for e in events].count("tool_end") == 1
    assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "Проверю. Планов нет."


async def test_cancel_after_successful_write_preserves_result(store, catalog, counter) -> None:
    written = asyncio.Event()

    async def respond(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("update_memory", {"text": "Не люблю кинзу"}, "saved")])
        written.set()
        await asyncio.sleep(10)
        return ModelResponse(parts=[TextPart("unused")])

    session = Session(
        Config(_env_file=None),
        build_agent(FunctionModel(respond)),
        store,
        catalog,
        NoProducts(),
        SummaryGenerator(),
        counter=counter,
    )
    task = asyncio.create_task(session.run("u", "c", "Запомни", "cancelled"))
    await asyncio.wait_for(written.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.person("u")).memory == "Не люблю кинзу"
    turns = (await store.history("c"))[2]
    messages = ModelMessagesTypeAdapter.validate_json(turns[0]["messages"])
    calls = [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)]
    returns = [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    assert calls == returns == ["saved"]
    assert (await session.run("u", "c", "Запомни", "cancelled"))["replayed"]


async def test_cancel_preserves_visible_stream_without_duplicate_tool_text(store, catalog, counter) -> None:
    from pydantic_ai.models.function import DeltaToolCall

    visible = asyncio.Event()

    async def stream(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Проверю. "
            yield {0: DeltaToolCall(name="list_plans", json_args="{}", tool_call_id="read")}
        else:
            yield "Планов нет. "
            yield "Продолжение"
            await asyncio.Event().wait()

    async def emit(event):
        if event["type"] == "text_delta" and "Продолжение" in event["text"]:
            visible.set()

    session = Session(
        Config(_env_file=None),
        build_agent(FunctionModel(stream_function=stream)),
        store,
        catalog,
        NoProducts(),
        SummaryGenerator(),
        counter=counter,
    )
    task = asyncio.create_task(session.run("u", "c", "Планы", "partial", emit=emit))
    await asyncio.wait_for(visible.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    messages = ModelMessagesTypeAdapter.validate_json((await store.history("c"))[2][0]["messages"])
    text = "".join(p.content for m in messages for p in m.parts if isinstance(p, TextPart))
    assert text.count("Проверю.") == 1
    assert text.count("Планов нет.") == 1
    assert text.count("Продолжение") == 1
    assert "Выполнение остановлено" in text
    assert "Не удалось" not in text
    calls = [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)]
    returns = [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    assert calls == returns == ["read"]
    assert (await session.run("u", "c", "Планы", "partial"))["replayed"]


async def test_plan_progress_uses_tool_call_id_without_entering_model_history(store, catalog, counter):
    from pydantic_ai.models.function import DeltaToolCall

    from app.models import Meal, PlanDraft

    async def stream(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield {
                0: DeltaToolCall(
                    name="generate_plan",
                    json_args='{"request":"Меню", "days":1, "limits":{"budget_rub":null,"max_difficulty":null}}',
                    tool_call_id="menu-progress",
                )
            }
        else:
            yield "Готово."

    class Generator(SummaryGenerator):
        async def generate(self, *args, **kwargs):
            return PlanDraft(
                meals=[Meal(day=1, meal="breakfast", recipe_id="r1", servings=1)], explanation=""
            )

    events = []

    async def emit(event):
        events.append(event)

    session = Session(
        Config(_env_file=None),
        build_agent(FunctionModel(stream_function=stream)),
        store,
        catalog,
        NoProducts(),
        Generator(),
        counter=counter,
    )
    result = await session.run("u", "c", "Меню", emit=emit)
    assert result["error"] is None
    stages = [e for e in events if e["type"] == "progress"]
    assert [e["stage"] for e in stages] == ["plan_prepare", "plan_generate", "plan_check", "plan_save"]
    assert all(e["call_id"] == "menu-progress" and e["name"] == "generate_plan" for e in stages)
    _, _, turns = await store.history("c")
    assert "plan_generate" not in str(turns)
