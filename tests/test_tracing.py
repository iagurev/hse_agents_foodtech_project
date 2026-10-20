import asyncio
import json
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from app.assistant.agent import build_agent
from app.assistant.session import Session
from app.config import Config
from app.llm import ModelGateway
from app.tracing import Tracing, outcome, stage
from app.web.guard import InputGuard


@pytest.fixture
def traces(monkeypatch):
    monkeypatch.setattr("app.tracing.Langfuse", lambda **kwargs: SimpleNamespace(shutdown=lambda: None))
    value = Tracing(
        Config(_env_file=None, langfuse_enabled=True, langfuse_public_key="test", langfuse_secret_key="test")
    )
    exporter = InMemorySpanExporter()
    value.provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield value, exporter
    value.close()


async def test_sdk_tree_costs_and_concurrent_turns(traces, store, catalog, counter):
    tracing, exporter = traces

    def response(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            parts = [ToolCallPart("list_plans", {}, "list")]
        else:
            parts = [TextPart("Планов пока нет")]
        return ModelResponse(
            parts=parts,
            usage=RequestUsage(input_tokens=20, output_tokens=5, details={"reasoning_tokens": 3}),
            provider_details={"cost": 0.003},
        )

    session = Session(
        Config(_env_file=None),
        build_agent(FunctionModel(response), tracing.instrumentation),
        store,
        catalog,
        None,
        None,
        counter,
    )

    async def turn(uid, chat):
        with tracing.turn(uid, chat, "turn-" + chat, "Покажи планы") as span:
            result = await session.run(uid, chat, "Покажи планы", "turn-" + chat)
            outcome(span, {"state": "complete", "text": result["text"]})

    await asyncio.gather(turn("u", "c"), turn("other", "other-chat"))
    spans = exporter.get_finished_spans()
    assert all(s.attributes["langfuse.environment"] == "local" for s in spans)
    roots = [s for s in spans if s.name == "chat.turn"]
    assert len(roots) == 2 and len({s.context.trace_id for s in roots}) == 2
    for root in roots:
        children = [s for s in spans if s.context.trace_id == root.context.trace_id]
        generations = [
            s
            for s in children
            if "gen_ai.request.model" in s.attributes and "gen_ai.operation.name" in s.attributes
        ]
        assert len(generations) == 2
        assert (
            sum(json.loads(s.attributes["langfuse.observation.cost_details"])["total"] for s in generations)
            == 0.006
        )
        assert all("gen_ai.input.messages" in s.attributes for s in generations)
        assert all(
            json.loads(s.attributes["langfuse.observation.usage_details"])
            == {"input": 20, "output": 5, "total": 25}
            for s in generations
        )
        tools = [s for s in children if s.attributes.get("gen_ai.tool.name") == "list_plans"]
        assert len(tools) == 1 and tools[0].parent is not None
        assert tools[0].attributes["langfuse.observation.status_message"] == "ok"
        assert json.loads(tools[0].attributes["langfuse.observation.input"]) == {}
        assert "langfuse.observation.cost_details" not in root.attributes
    before = len(spans)
    assert (await session.run("u", "c", "Покажи планы", "turn-c"))["replayed"]
    assert len(exporter.get_finished_spans()) == before


async def test_nested_generation_and_failed_stages(traces):
    tracing, exporter = traces
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("Сводка")], provider_details={"cost": 0.001})
    )
    gateway = ModelGateway(model, 12, instrumentation=tracing.instrumentation)
    with tracing.turn("u", "c", "t", "Продолжим"):
        with stage("plan.operation"):
            assert await gateway.compact({"history": "abc"}, RunUsage()) == "Сводка"
        with pytest.raises(TimeoutError), stage("vkusvill_products_search", {"query": "яйца"}):
            raise TimeoutError("Connection timeout")
        with stage("cart.result") as span:
            outcome(span, {"status": "partial", "unresolved": ["яйца"]})
        with pytest.raises(asyncio.CancelledError), stage("cancelled"):
            raise asyncio.CancelledError()
    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    failed = by_name["vkusvill_products_search"]
    assert failed.status.status_code == StatusCode.ERROR
    assert by_name["cancelled"].status.status_code == StatusCode.ERROR
    assert by_name["cart.result"].attributes["langfuse.observation.level"] == "WARNING"
    internal = next(s for s in spans if s.attributes.get("gen_ai.agent.name") == "compaction")
    assert internal.parent.span_id == by_name["plan.operation"].context.span_id


async def test_guard_is_in_same_trace(traces):
    tracing, exporter = traces
    guard = InputGuard(
        FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[TextPart('{"allowed":true,"message":""}')], provider_details={"cost": 0.0001}
            )
        ),
        tracing.instrumentation,
    )
    with tracing.turn("u", "c", "t", "Привет"):
        assert (await guard.check("Привет", {}, [], RunUsage())).allowed
    spans = exporter.get_finished_spans()
    assert len({s.context.trace_id for s in spans}) == 1
    assert any(s.attributes.get("gen_ai.agent.name") == "input_guard" for s in spans)


def test_disabled_tracing_does_not_start_client(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("Disabled tracing must not initialize Langfuse")

    monkeypatch.setattr("app.tracing.Langfuse", unexpected)
    tracing = Tracing(Config(_env_file=None))
    with tracing.turn("u", "c", "t", "Привет") as span:
        assert not span.is_recording()
    tracing.close()
