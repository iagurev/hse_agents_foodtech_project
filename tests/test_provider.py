import json

import httpx
import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter

from app.assistant.agent import build_agent
from app.assistant.session import Session
from app.config import Config
from app.llm.gateway import ModelGateway
from app.llm.provider import build_model


@pytest.mark.parametrize("request_limit", [2, 3])
async def test_wire_schema_nested_budget_usage_and_private_catalog(
    store, catalog, request_limit, counter
) -> None:
    requests: list[dict] = []

    def response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["model"] == "openai/gpt-6-luna"
            assert payload["provider"]["only"] == ["openai"]
            assert payload["reasoning"]["effort"] == "medium"
            definition = next(
                t["function"] for t in payload["tools"] if t["function"]["name"] == "generate_plan"
            )
            assert "$ref" not in json.dumps(definition)
            cart = next(t for t in payload["tools"] if t["function"]["name"] == "create_cart")
            assert cart["function"]["strict"] is True
            assert "oneOf" not in json.dumps(cart)
            assert "anyOf" in cart["function"]["parameters"]["properties"]["source"]
            assert definition["parameters"]["properties"]["limits"]["type"] == "object"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_plan",
                        "type": "function",
                        "function": {
                            "name": "generate_plan",
                            "arguments": json.dumps(
                                {
                                    "request": "Овсянка",
                                    "days": 1,
                                    "limits": {"max_difficulty": "easy", "budget_rub": 100},
                                }
                            ),
                        },
                    }
                ],
            }
        elif len(requests) == 2:
            assert payload["response_format"]["type"] == "json_schema"
            assert "catalog" in payload["messages"][-1]["content"]
            content = payload["messages"][-1]["content"]
            inner = json.loads(content)
            assert content == json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
            assert inner["catalog"][0]["ingredients"] == ["Овсянка"]
            assert not payload.get("tools")
            assert payload["model"] == "openai/gpt-6-luna"
            assert payload["provider"]["only"] == ["openai"]
            assert payload["reasoning"]["effort"] == "medium"
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "meals": [{"day": 1, "meal": "breakfast", "recipe_id": "r1", "servings": 1}],
                        "explanation": "Готово",
                    }
                ),
            }
        else:
            assert payload["model"] == "openai/gpt-6-luna"
            assert '"catalog":' not in json.dumps(payload, ensure_ascii=False)
            message = {"role": "assistant", "content": "План сохранён."}
        return httpx.Response(
            200,
            json={
                "id": f"r{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "provider": "OpenInference",
                "choices": [
                    {
                        "index": 0,
                        "native_finish_reason": "stop",
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                        "message": message,
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cost": 0.001,
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            },
        )

    config = Config(_env_file=None, openrouter_api_key="test", request_limit=request_limit)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = build_model(config, client)
        session = Session(
            config,
            build_agent(model),
            store,
            catalog,
            None,
            ModelGateway(
                model,
                request_limit,
                planning_model=build_model(config, client, planning=True),
                planning_effort=config.planning_reasoning_effort,
            ),
            counter=counter,
        )
        result = await session.run("u", "c", "Составь план")
    if request_limit == 2:
        assert result["error"] == "UsageLimitExceeded"
        assert len(requests) == result["requests"] == 2
        assert len((await store.list_objects("u", "plan")).data) == 1
        return
    assert result["error"] is None
    assert len(requests) == result["requests"] == 3
    assert result["input_tokens"] == 300 and result["output_tokens"] == 60
    assert abs(result["cost_usd"] - 0.003) < 1e-9
    messages = ModelMessagesTypeAdapter.validate_json((await store.history("c"))[2][0]["messages"])
    assert '"catalog":' not in ModelMessagesTypeAdapter.dump_json(messages).decode()


async def test_invalid_native_output_is_charged_without_automatic_retry() -> None:
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.usage import RunUsage

    from app.models import PlanDraft

    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "invalid",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-6-luna",
                "provider": "OpenInference",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": "not JSON"},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.001},
            },
        )

    usage = RunUsage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        with pytest.raises(UnexpectedModelBehavior):
            await ModelGateway(model, 12).generate("meal_plan", PlanDraft, {}, usage, "high")
    assert len(calls) == usage.requests == 1
    assert usage.input_tokens == 100
    assert usage.details["cost_nano_usd"] == 1_000_000


async def test_planning_correction_uses_luna_but_editing_and_compaction_use_chat_model() -> None:
    from pydantic_ai.usage import RunUsage

    from app.models import PlanDraft

    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        content = (
            json.dumps({"meals": [], "explanation": "Проверка маршрута"})
            if body.get("response_format")
            else "Сводка"
        )
        return httpx.Response(
            200,
            json={
                "id": f"route-{len(requests)}",
                "model": body["model"],
                "object": "chat.completion",
                "provider": "OpenAI" if body["model"] == "openai/gpt-6-luna" else "OpenInference",
                "choices": [
                    {
                        "index": 0,
                        "native_finish_reason": "stop",
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001},
            },
        )

    config = Config(_env_file=None, openrouter_api_key="test")
    usage = RunUsage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        gateway = ModelGateway(
            build_model(config, client),
            4,
            planning_model=build_model(config, client, planning=True),
            planning_effort=config.planning_reasoning_effort,
        )
        for operation in ("generate", "repair"):
            await gateway.generate("meal_plan", PlanDraft, {"operation": operation}, usage)
        await gateway.generate("edit_plan", PlanDraft, {}, usage)
        await gateway.compact({}, usage, request_limit=4)
    assert [r["model"] for r in requests] == [config.planning_model] * 2 + [config.model] * 2
    assert [r["reasoning"]["effort"] for r in requests] == ["medium", "medium", "low", "medium"]
    assert [r["provider"]["only"] for r in requests] == [["openai"]] * 4
    assert all(not r.get("tools") for r in requests)
    assert usage.requests == 4
    assert usage.details["cost_nano_usd"] == 4_000_000


async def test_stream_preserves_separate_encrypted_reasoning_items_for_continuation() -> None:
    from pydantic_ai import Agent

    requests = []
    details = [
        {"type": "reasoning.summary", "summary": "Первая часть", "index": 0},
        {"type": "reasoning.encrypted", "id": "rs_first", "data": "cipher-first", "index": 1},
        {"type": "reasoning.summary", "summary": "Вторая часть", "index": 2},
        {"type": "reasoning.encrypted", "id": "rs_second", "data": "cipher-second", "index": 3},
    ]
    for detail in details:
        detail["format"] = "openai-responses-v1"
        detail.setdefault("id", None)

    def response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            previous = next(m for m in payload["messages"] if m["role"] == "assistant")
            assert previous["reasoning_details"] == details
        chunks = []
        for detail in details if len(requests) == 1 else []:
            chunks.append({"delta": {"reasoning_details": [detail]}, "finish_reason": None})
        chunks.append({"delta": {"content": "Готово"}, "finish_reason": "stop"})
        body = ""
        for choice in chunks:
            chunk = {
                "id": "gen-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": payload["model"],
                "provider": "OpenAI",
                "choices": [{"index": 0, **choice}],
            }
            body += "data: " + json.dumps(chunk) + "\n\n"
        return httpx.Response(
            200, text=body + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}
        )

    async def consume(_, events):
        async for _ in events:
            pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        agent = Agent(build_model(Config(_env_file=None, openrouter_api_key="test"), client))
        first = await agent.run("Первый", event_stream_handler=consume)
        await agent.run("Продолжи", message_history=first.new_messages(), event_stream_handler=consume)
    assert len(requests) == 2


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "error"])
async def test_truncated_summary_is_charged_but_rejected(finish_reason):
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.usage import RunUsage

    from app.llm.gateway import ModelGateway

    def response(req):
        body = json.loads(req.content)
        assert body["max_completion_tokens"] == 16000 and "max_tokens" not in body and not body.get("tools")
        return httpx.Response(
            200,
            json={
                "id": "m",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-6-luna",
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "native_finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": "Оборвано"},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.001},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        usage = RunUsage()
        gateway = ModelGateway(build_model(Config(_env_file=None, openrouter_api_key="test"), client), 12)
        with pytest.raises(UnexpectedModelBehavior):
            await gateway.compact({"history": []}, usage)
        assert usage.requests == 1 and usage.details["cost_nano_usd"] == 1000000
