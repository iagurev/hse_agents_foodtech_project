import asyncio
import json

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

from app.assistant.agent import build_agent
from app.assistant.session import Session
from app.config import Config


class Summaries:
    def __init__(self, replies=None, delay=0):
        self.payloads = []
        self.replies = replies or ["Активен plan_1; бюджет 5000; рыба исключена."]
        self.delay = delay

    async def compact(self, payload, usage, **kwargs):
        self.payloads.append(payload)
        usage.requests += 1
        usage.input_tokens += 100
        usage.output_tokens += 20
        usage.details["cost_nano_usd"] = usage.details.get("cost_nano_usd", 0) + 1000000
        await asyncio.sleep(self.delay)
        reply = self.replies[min(len(self.payloads) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


def settings(threshold=1000, hard=100000, **kwargs):
    return Config(
        _env_file=None,
        context_window_tokens=hard + 120,
        response_reserve_tokens=100,
        token_count_margin=20,
        compaction_threshold_tokens=threshold,
        summary_max_tokens=80,
        **kwargs,
    )


async def seed(store, count=10, size=100):
    for i in range(count):
        messages = [
            ModelRequest(parts=[UserPromptPart(str(i).zfill(size))]),
            ModelResponse(parts=[ThinkingPart("old hidden"), ToolCallPart("list_plans", {}, f"old{i}")]),
            ModelRequest(parts=[ToolReturnPart("list_plans", [], f"old{i}")]),
            ModelResponse(parts=[TextPart("x" * 20)]),
        ]
        await store.save_turn("c", f"old{i}", ModelMessagesTypeAdapter.dump_json(messages).decode(), True)


def session(config, store, catalog, counter, generator, respond=None):
    return Session(
        config,
        build_agent(FunctionModel(respond or (lambda m, i: ModelResponse(parts=[TextPart("Да")])))),
        store,
        catalog,
        None,
        generator,
        counter,
    )


@pytest.mark.parametrize("threshold,keep", [(1000, 6), (600, 3), (300, 1), (150, 0)])
async def test_compaction_selects_whole_tail_and_retains_raw(store, catalog, counter, threshold, keep):
    await seed(store)
    await store.save_summary("c", 0, "Ранее выбран plan_1.")
    generator = Summaries()
    seen = []

    def respond(messages, info):
        seen.extend(messages)
        return ModelResponse(parts=[TextPart("Да")])

    result = await session(settings(threshold), store, catalog, counter, generator, respond).run(
        "u", "c", "?"
    )
    assert result["error"] is None
    summary, boundary, turns = await store.history("c")
    assert boundary == 10 - keep and len(turns) == keep + 1
    assert (
        summary == generator.replies[0]
        and generator.payloads[0]["previous_summary"] == "Ранее выбран plan_1."
    )
    assert len(generator.payloads) == 1
    compacted = ModelMessagesTypeAdapter.validate_python(generator.payloads[0]["history"])
    assert len(compacted) == (10 - keep) * 4
    assert not any(isinstance(p, ThinkingPart) for m in seen for p in m.parts)
    calls = [p.tool_call_id for m in compacted for p in m.parts if isinstance(p, ToolCallPart)]
    returns = [p.tool_call_id for m in compacted for p in m.parts if isinstance(p, ToolReturnPart)]
    assert calls == returns
    assert summary in next(m.instructions for m in reversed(seen) if isinstance(m, ModelRequest))
    async with store.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM turns")).fetchone())[0] == 11
    assert result["compaction"]["requests"] == 1 and result["compaction"]["input_tokens"] == 100
    assert result["requests"] == 2 and result["cost_usd"] == 0.001


async def test_many_short_turns_do_not_trigger(store, catalog, counter):
    await seed(store, 40, 1)
    generator = Summaries()
    result = await session(settings(2000), store, catalog, counter, generator).run("u", "c", "?")
    assert result["error"] is None and not generator.payloads
    assert (await store.history("c"))[1] == 0


async def test_compacts_after_tool_result_preserving_active_reasoning(store, catalog, counter):
    await seed(store, 4)
    seen = []

    def respond(messages, info):
        seen.append(list(messages))
        if len(seen) == 1:
            return ModelResponse(
                parts=[
                    ThinkingPart("active reasoning"),
                    ToolCallPart("get_recipes", {"recipe_ids": ["r1"]}, "lookup"),
                ]
            )
        return ModelResponse(parts=[TextPart("Готово")])

    generator = Summaries()
    result = await session(settings(900), store, catalog, counter, generator, respond).run(
        "u", "c", "Покажи рецепт"
    )
    assert result["error"] is None and len(generator.payloads) == 1 and len(seen) == 2
    assert any(
        isinstance(p, ThinkingPart) and p.content == "active reasoning" for m in seen[-1] for p in m.parts
    )
    raw = (await store.history("c"))[2][-1]["messages"]
    messages = ModelMessagesTypeAdapter.validate_json(raw)
    assert any(isinstance(p, ToolCallPart) and p.tool_call_id == "lookup" for m in messages for p in m.parts)
    assert any(
        isinstance(p, ToolReturnPart) and p.tool_call_id == "lookup" for m in messages for p in m.parts
    )
    assert all(m.run_id is None or not m.run_id.startswith("history:") for m in messages)


@pytest.mark.parametrize("reply", [RuntimeError("provider failed"), "", " "])
async def test_failure_stops_without_saving_summary(store, catalog, counter, reply):
    await seed(store)
    seen = []
    result = await session(
        settings(), store, catalog, counter, Summaries([reply]), lambda m, i: seen.append(m)
    ).run("u", "c", "?")
    assert result["error"] == "CompactionError" and not seen
    summary, boundary, turns = await store.history("c")
    assert summary == "" and boundary == 0 and len(turns) == 11


@pytest.mark.parametrize("replies,success", [(["x" * 81, "коротко"], True), (["x" * 81, "y" * 81], False)])
async def test_long_summary_one_correction(store, catalog, counter, replies, success):
    await seed(store)
    generator = Summaries(replies)
    result = await session(settings(), store, catalog, counter, generator).run("u", "c", "?")
    assert len(generator.payloads) == 2 and result["compaction"]["requests"] == 2
    assert (result["error"] is None) == success
    assert ((await store.history("c"))[1] > 0) == success


@pytest.mark.parametrize("length,allowed", [(964, True), (965, False)])
async def test_current_only_hard_boundary(store, catalog, counter, length, allowed):
    # ControlledCounter: 10 + 13*2 + input length; no completed history can be compressed.
    generator = Summaries()
    result = await session(settings(200, 1000), store, catalog, counter, generator).run(
        "u", "c", "x" * length
    )
    assert (result["error"] is None) == allowed
    assert not generator.payloads
    if not allowed:
        assert result["error"] == "ContextOverflowError"


async def test_compaction_clock_is_separate_and_timeout_is_atomic(store, catalog, counter):
    await seed(store)
    generator = Summaries(delay=0.08)
    result = await session(
        settings(turn_timeout_seconds=0.05, compaction_timeout_seconds=0.5),
        store,
        catalog,
        counter,
        generator,
    ).run("u", "c", "?")
    assert result["error"] is None and result["compaction"]["elapsed_seconds"] >= 0.08
    await store.ensure_chat("u", "other")
    # A new attempt in the same chat has enough history to trigger again under a lower threshold.
    result = await session(
        settings(150, compaction_timeout_seconds=0.01), store, catalog, counter, Summaries(delay=0.1)
    ).run("u", "c", "?")
    assert result["error"] == "CompactionError"
    assert (await store.history("other")) == ("", 0, [])


async def test_cancel_after_compaction_and_write_keeps_current_turn(store, catalog, counter):
    await seed(store)
    written = asyncio.Event()
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("update_memory", {"text": "Не ест кинзу"}, "save")])
        written.set()
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("unused")])

    runtime = session(settings(), store, catalog, counter, Summaries(), respond)
    task = asyncio.create_task(runtime.run("u", "c", "Запомни", "delivery"))
    await asyncio.wait_for(written.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    messages = ModelMessagesTypeAdapter.validate_json((await store.history("c"))[2][-1]["messages"])
    assert [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)] == ["save"]
    assert [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)] == ["save"]
    assert (await store.person("u")).memory == "Не ест кинзу"
    assert (await runtime.run("u", "c", "Запомни", "delivery"))["replayed"]


async def test_two_blocks_and_budget_exhaustion_never_commit_partial(store, catalog, counter):
    await seed(store, 4)

    class BlockCounter:
        async def text(self, value):
            return len(value)

        async def count(self, messages, tools):
            if not tools:  # Bound the summarizer to two old turns per request.
                payload = json.loads(messages[0].parts[0].content)
                return 50 + len(payload["history"]) * 100
            return await counter.count(messages, tools)

    generator = Summaries()
    result = await session(settings(150, 1000), store, catalog, BlockCounter(), generator).run("u", "c", "?")
    assert result["error"] is None and len(generator.payloads) == 2
    assert (await store.history("c"))[1] == 4
    # Separate chat seeded with more blocks: budget must not save the first two summaries.
    async with store.connection() as db:
        await db.execute("UPDATE chats SET summary='',through_turn=0 WHERE chat_id='c'")
        await db.commit()
    generator = Summaries()
    result = await session(settings(150, 500), store, catalog, BlockCounter(), generator).run("u", "c", "?")
    assert result["error"] == "CompactionError" and len(generator.payloads) == 2
    assert (await store.history("c"))[0:2] == ("", 0)


async def test_budget_shared_across_multiple_tool_continuations(store, catalog, counter):
    await seed(store)
    generator = Summaries()
    observed = []

    def respond(messages, info):
        observed.append(messages)
        # Each large active response forces a shorter historical tail.
        return ModelResponse(parts=[TextPart("x" * 350), ToolCallPart("list_plans", {}, str(len(observed)))])

    runtime = session(settings(), store, catalog, counter, generator, respond)
    result = await runtime.run("u", "c", "?", "shared-budget")
    assert result["error"] == "CompactionError"
    assert len(generator.payloads) == 2 and len(observed) == 2
    assert result["compaction"]["requests"] == 2
    assert result["requests"] == 4
    assert (await store.history("c"))[1] == 7  # last successful compaction, not a partial third
    replay = await runtime.run("u", "c", "?", "shared-budget")
    assert replay["replayed"] and replay["text"] == result["text"]


async def test_cancel_during_summary_does_not_commit(store, catalog, counter):
    await seed(store)
    started = asyncio.Event()

    class WaitingSummary(Summaries):
        async def compact(self, payload, usage, **kwargs):
            started.set()
            await asyncio.sleep(5)
            return "unused"

    runtime = session(settings(), store, catalog, counter, WaitingSummary())
    task = asyncio.create_task(runtime.run("u", "c", "?"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    summary, boundary, turns = await store.history("c")
    assert summary == "" and boundary == 0 and len(turns) == 11
