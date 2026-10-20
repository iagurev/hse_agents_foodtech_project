import json

import httpx
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from app.config import Config
from app.llm.provider import build_model
from app.llm.tokens import LunaTokenCounter, load_counter


async def test_counter_matches_sdk_wire_with_transformed_schema(monkeypatch):
    captured = []

    def response(req):
        captured.append(json.loads(req.content))
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
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        monkeypatch.setattr("app.llm.tokens.tiktoken.get_encoding", lambda _: FakeEncoding())
        counter = LunaTokenCounter(model)
        tool = ToolDefinition(
            name="lookup",
            description="Описание",
            parameters_json_schema={
                "type": "object",
                "properties": {"limits": {"$ref": "#/$defs/Limits"}},
                "$defs": {
                    "Limits": {
                        "type": "object",
                        "properties": {"budget": {"type": "number", "description": "Бюджет"}},
                    }
                },
            },
        )
        messages = [
            ModelRequest(
                parts=[UserPromptPart("hello world")], instructions="Инструкции и персональные данные"
            )
        ]
        estimated = await counter.count(messages, [tool])
        await model.request(messages, None, ModelRequestParameters(function_tools=[tool]))
        wire = captured[0]
        assert "$ref" not in json.dumps(wire["tools"])
        assert estimated == counter.count_wire(wire["messages"], wire["tools"])
        assert estimated > await counter.count(messages, [])
        assert estimated > await counter.count([ModelRequest(parts=[UserPromptPart("hello")])], [tool])


async def test_openrouter_reasoning_is_counted_and_input_unchanged(monkeypatch):
    monkeypatch.setattr("app.llm.tokens.tiktoken.get_encoding", lambda _: FakeEncoding())
    async with httpx.AsyncClient() as client:
        counter = LunaTokenCounter(build_model(Config(_env_file=None, openrouter_api_key="test"), client))
        wire = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": [{"type": "reasoning.text", "text": "hello world " * 20}],
            }
        ]
        copy = json.dumps(wire)
        assert counter.count_wire(wire, []) > counter.count_wire([{"role": "assistant", "content": ""}], [])
        assert json.dumps(wire) == copy
        messages = [
            ModelResponse(
                parts=[
                    ThinkingPart(
                        "hello world " * 20,
                        provider_name="openrouter",
                        provider_details={"type": "reasoning.text"},
                    )
                ]
            )
        ]
        assert await counter.count(messages, []) > await counter.count([], [])


class FakeEncoding:
    def encode(self, text, **kwargs):
        return text.encode()


async def test_luna_reasoning_and_counter_startup(monkeypatch):
    monkeypatch.setattr("app.llm.tokens.tiktoken.get_encoding", lambda _: FakeEncoding())
    async with httpx.AsyncClient() as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        counter = await load_counter(model)
        assert isinstance(counter, LunaTokenCounter)
        messages = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque" * 100}],
            }
        ]
        original = json.dumps(messages)
        assert counter.count_wire(messages, []) > counter.count_wire([], []) + 600
        assert json.dumps(messages) == original
        assert await counter.text("<|endoftext|>") > 0

        def unavailable(_):
            raise OSError("offline")

        monkeypatch.setattr("app.llm.tokens.tiktoken.get_encoding", unavailable)
        with pytest.raises(ValueError, match="токенизатор Luna"):
            await load_counter(model)
        unsupported = build_model(Config(_env_file=None, openrouter_api_key="test", model="unknown"), client)
        with pytest.raises(ValueError, match="Не настроен"):
            await load_counter(unsupported)
