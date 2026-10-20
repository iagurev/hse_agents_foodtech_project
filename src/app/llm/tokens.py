import asyncio
import copy
import json
from typing import Any, Protocol

import tiktoken
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.tools import ToolDefinition


class RequestCounter(Protocol):
    """Граница локального подсчёта и контролируемой замены в тестах."""

    async def count(self, messages: list[ModelMessage], tools: list[ToolDefinition]) -> int: ...
    async def text(self, value: str) -> int: ...


class LunaTokenCounter:
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


async def load_counter(model: OpenRouterModel) -> RequestCounter:
    """Загружает счётчик Luna один раз при запуске, вне основного event loop.

    :param model: Адаптер реальной сериализации запросов SDK
    :return: Готовый счётчик; неизвестная модель или ошибка загрузки останавливают запуск
    """
    if model.model_name != "openai/gpt-6-luna":
        raise ValueError(f"Не настроен подсчёт контекста для {model.model_name}")
    try:
        return await asyncio.to_thread(LunaTokenCounter, model)
    except Exception as exc:
        raise ValueError(
            "Не удалось загрузить токенизатор Luna; проверьте сеть и локальный кэш tiktoken"
        ) from exc
