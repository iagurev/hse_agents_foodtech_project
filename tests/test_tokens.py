import hashlib
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
from tokenizers import Tokenizer, models, pre_tokenizers

from app.assistant.agent import build_model
from app.assistant.tokens import REVISION, GLMTokenCounter, LunaTokenCounter, load_counter
from app.config import Config


@pytest.fixture
def tokenizer_assets(tmp_path):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    # Tiny controlled template: production asset revision/accuracy is checked separately live.
    (tmp_path / "chat_template.jinja").write_text("{{ tools | tojson }} {{ messages | tojson }}")
    (tmp_path / "tokenizer_config.json").write_text("{}")
    return tmp_path


@pytest.mark.parametrize("kind", ["glm", "luna"])
async def test_counter_matches_sdk_wire_with_transformed_schema(tokenizer_assets, monkeypatch, kind):
    captured = []

    def response(req):
        captured.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": "m",
                "object": "chat.completion",
                "created": 1,
                "model": "z-ai/glm-5.3-flash",
                "provider": "OpenInference",
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
        if kind == "luna":
            monkeypatch.setattr("app.assistant.tokens.tiktoken.get_encoding", lambda _: FakeEncoding())
            counter = LunaTokenCounter(model)
        else:
            counter = GLMTokenCounter(model, tokenizer_assets)
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


async def test_openrouter_reasoning_is_counted_and_input_unchanged(tokenizer_assets):
    async with httpx.AsyncClient() as client:
        counter = GLMTokenCounter(
            build_model(Config(_env_file=None, openrouter_api_key="test"), client), tokenizer_assets
        )
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
        with pytest.raises(ValueError, match="зашифрованный"):
            counter.count_wire(
                [
                    {
                        "role": "assistant",
                        "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
                    }
                ],
                [],
            )
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


async def test_verified_cache_download_and_offline_reuse(tokenizer_assets, tmp_path, monkeypatch):
    import app.assistant.tokens as module

    content = {name: (tokenizer_assets / name).read_bytes() for name in module.ASSETS}
    monkeypatch.setattr(
        module, "ASSETS", {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    )
    calls = []

    def response(req):
        name = req.url.path.split("/")[-1]
        calls.append(name)
        return httpx.Response(200, content=content[name])

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        first = await GLMTokenCounter.load(model, tmp_path / "cache", client)
        second = await GLMTokenCounter.load(model, tmp_path / "cache", client)
        assert await first.text("hello world") == await second.text("hello world") == 2
        assert len(calls) == 3
        (tmp_path / "cache" / REVISION / "tokenizer.json").write_text("corrupt")
        await GLMTokenCounter.load(model, tmp_path / "cache", client)
        assert len(calls) == 4


async def test_unavailable_tokenizer_fails_without_fallback(tmp_path):
    def unavailable(req):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        with pytest.raises(ValueError, match="токенизатор"):
            await GLMTokenCounter.load(model, tmp_path, client)


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "error"])
async def test_truncated_summary_is_charged_but_rejected(finish_reason):
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.usage import RunUsage

    from app.llm import ModelGateway

    def response(req):
        body = json.loads(req.content)
        assert body["max_completion_tokens"] == 16000 and "max_tokens" not in body and not body.get("tools")
        return httpx.Response(
            200,
            json={
                "id": "m",
                "object": "chat.completion",
                "created": 1,
                "model": "z-ai/glm-5.3-flash",
                "provider": "OpenInference",
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


class FakeEncoding:
    def encode(self, text, **kwargs):
        return text.encode()


async def test_luna_reasoning_and_counter_startup(monkeypatch, tmp_path):
    monkeypatch.setattr("app.assistant.tokens.tiktoken.get_encoding", lambda _: FakeEncoding())
    async with httpx.AsyncClient() as client:
        model = build_model(Config(_env_file=None, openrouter_api_key="test"), client)
        counter = await load_counter(model, tmp_path, client)
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

        monkeypatch.setattr("app.assistant.tokens.tiktoken.get_encoding", unavailable)
        with pytest.raises(ValueError, match="токенизатор Luna"):
            await load_counter(model, tmp_path, client)
        unsupported = build_model(Config(_env_file=None, openrouter_api_key="test", model="unknown"), client)
        with pytest.raises(ValueError, match="Не настроен"):
            await load_counter(unsupported, tmp_path, client)
