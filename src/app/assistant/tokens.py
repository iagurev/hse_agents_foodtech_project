import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
import tiktoken
from jinja2.sandbox import ImmutableSandboxedEnvironment
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.tools import ToolDefinition
from tokenizers import Tokenizer

REVISION = "46a050bec13002ad29ee795c16e61cf4fa3c4a9c"
ASSETS = {
    "tokenizer.json": "19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d",
    "tokenizer_config.json": "98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc",
    "chat_template.jinja": "2614a3c896d53e265f564c23701173c0049f985dacfbae79fc4bcd9f5ba083f4",
}


class RequestCounter(Protocol):
    """Граница локального подсчёта и контролируемой замены в тестах."""

    async def count(self, messages: list[ModelMessage], tools: list[ToolDefinition]) -> int: ...
    async def text(self, value: str) -> int: ...


def _verified(path: Path, expected: str) -> bool:
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected


class SDKTokenCounter:
    """Переиспользует фактическую сериализацию сообщений и схем инструментов SDK."""

    model: OpenRouterModel

    def count_wire(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        raise NotImplementedError

    async def count(self, messages: list[ModelMessage], tools: list[ToolDefinition]) -> int:
        """Считает полный запрос с тем же профилем схем, что использует SDK.

        :param messages: История с текущими instructions
        :param tools: Зарегистрированные definitions, не копии контрактов
        :return: Локальное число входных токенов
        """
        _, parameters = self.model.prepare_request(
            None, ModelRequestParameters(function_tools=copy.deepcopy(tools))
        )
        wire = await self.model._map_messages(messages, parameters)
        definitions = self.model._get_tools(parameters)
        return await asyncio.to_thread(self.count_wire, wire, definitions)


class GLMTokenCounter(SDKTokenCounter):
    """Считает официальный шаблон GLM; формат провайдера может отличаться.

    SDK-сериализация из закреплённого Pydantic AI 1.68.0 намеренно переиспользуется;
    соответствие настоящему wire-запросу проверяется тестом.
    """

    def __init__(self, model: OpenRouterModel, directory: Path) -> None:
        self.model = model
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        env = ImmutableSandboxedEnvironment(
            trim_blocks=True, lstrip_blocks=True, extensions=["jinja2.ext.loopcontrols"]
        )
        env.filters["tojson"] = lambda value, **kwargs: json.dumps(value, **kwargs)
        self.template = env.from_string((directory / "chat_template.jinja").read_text())

    @classmethod
    async def load(cls, model: OpenRouterModel, cache: Path, client: httpx.AsyncClient) -> "GLMTokenCounter":
        """Загружает проверенные файлы один раз при сборке приложения.

        :param model: Реальный адаптер сериализации SDK
        :param cache: Каталог локального кэша, без весов модели
        :param client: HTTP-клиент для первого запуска
        :return: Готовый счётчик; ошибка загрузки не заменяется оценкой по символам
        """
        directory = cache / REVISION
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for name, expected in ASSETS.items():
                path = directory / name
                if await asyncio.to_thread(_verified, path, expected):
                    continue
                response = await client.get(
                    f"https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/{REVISION}/{name}",
                    follow_redirects=True,
                    timeout=60,
                )
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != expected:
                    raise ValueError("Контрольная сумма токенизатора не совпадает")
                temporary = directory / f".{name}.{uuid4().hex}"
                try:
                    await asyncio.to_thread(temporary.write_bytes, response.content)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            return await asyncio.to_thread(cls, model, directory)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ValueError(
                "Не удалось загрузить проверенный токенизатор GLM; проверьте сеть и кэш"
            ) from exc

    def count_wire(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """Считает сериализованные сообщения и tools на CPU.

        Any отражает неоднородные объекты протокола OpenRouter.
        :param messages: Сообщения из SDK-сериализатора
        :param tools: Определения из SDK после преобразования JSON Schema
        :return: Число токенов шаблона, включая текущий reasoning
        """
        messages = copy.deepcopy(messages)
        for message in messages:
            for call in message.get("tool_calls") or []:
                args = call["function"]["arguments"]
                if isinstance(args, str):
                    call["function"]["arguments"] = json.loads(args)
            reasoning = []
            for detail in message.get("reasoning_details") or []:
                if detail.get("type") == "reasoning.encrypted":
                    raise ValueError("Нельзя локально подсчитать зашифрованный reasoning")
                reasoning.append(detail.get("text") or detail.get("summary") or "")
            if reasoning:
                message["reasoning_content"] = "\n".join(reasoning)
            elif message.get("reasoning"):
                message["reasoning_content"] = message["reasoning"]
        rendered = self.template.render(
            messages=messages,
            tools=tools,
            add_generation_prompt=True,
            reasoning_effort="low",
            clear_thinking=False,
        )
        return len(self.tokenizer.encode(rendered, add_special_tokens=False).ids)

    async def text(self, value: str) -> int:
        """Считает текст сводки без служебного оформления.

        :param value: Текст
        :return: Число токенов
        """
        return await asyncio.to_thread(
            lambda: len(self.tokenizer.encode(value, add_special_tokens=False).ids)
        )


class LunaTokenCounter(SDKTokenCounter):
    """Оценивает wire-запрос Luna через o200k_base с запасом на оформление.

    Публичного chat template Luna нет. Это проверяемая по usage оценка,
    а не обещание точного совпадения с сервером; общий запас сохраняется.
    Зашифрованный reasoning учитывается как непрозрачный текст целиком.
    """

    def __init__(self, model: OpenRouterModel) -> None:
        self.model = model
        self.encoding = tiktoken.get_encoding("o200k_base")

    def count_wire(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """Учитывает все поля SDK, включая tools, instructions и текущий reasoning."""
        wire = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
        return len(self.encoding.encode(wire, disallowed_special=())) + 16 * len(messages) + 32 * len(tools)

    async def text(self, value: str) -> int:
        """Считает сводку без обрамления запроса."""
        return await asyncio.to_thread(lambda: len(self.encoding.encode(value, disallowed_special=())))


async def load_counter(model: OpenRouterModel, cache: Path, client: httpx.AsyncClient) -> RequestCounter:
    """Выбирает счётчик для настроенной модели; неизвестную модель не угадывает.

    :param model: Модель чатового агента
    :param cache: Кэш файлов GLM при явном выборе этой модели
    :param client: Клиент первоначальной загрузки
    :return: Счётчик, загруженный один раз при запуске
    """
    if model.model_name == "z-ai/glm-5.3-flash":
        return await GLMTokenCounter.load(model, cache, client)
    if model.model_name == "openai/gpt-6-luna":
        try:
            return await asyncio.to_thread(LunaTokenCounter, model)
        except Exception as exc:
            raise ValueError(
                "Не удалось загрузить токенизатор Luna; проверьте сеть и локальный кэш tiktoken"
            ) from exc
    raise ValueError(f"Не настроен подсчёт контекста для {model.model_name}")
