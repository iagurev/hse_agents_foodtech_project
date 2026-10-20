import json
import time
from typing import Literal, Protocol, TypeVar

from loguru import logger
from pydantic import BaseModel, JsonValue
from pydantic_ai import Agent, InstrumentationSettings, NativeOutput
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from app.assistant.prompts import PromptName, load_prompt

Output = TypeVar("Output", bound=BaseModel)
Response = TypeVar("Response")


class Generator(Protocol):
    """Граница ограниченных вызовов для подмены модели в детерминированных тестах."""

    async def generate(
        self,
        prompt: PromptName,
        schema: type[Output],
        payload: dict[str, JsonValue],
        usage: RunUsage,
        effort: Literal["low", "medium", "high"] = "low",
    ) -> Output: ...

    async def compact(
        self,
        payload: dict[str, JsonValue],
        usage: RunUsage,
        *,
        max_output_tokens: int = 16000,
        request_limit: int = 2,
    ) -> str: ...


class ModelGateway:
    """Исполняет один SDK-вызов без скрытых исправлений результата."""

    def __init__(
        self,
        model: Model,
        request_limit: int,
        *,
        planning_model: Model | None = None,
        planning_effort: Literal["low", "medium", "high"] = "medium",
        instrumentation: InstrumentationSettings | bool = False,
    ) -> None:
        self.instrumentation = instrumentation
        self.model = model
        self.planning_model = planning_model
        self.planning_effort = planning_effort
        self.limits = UsageLimits(request_limit=request_limit)
        self.prompts = {
            name: load_prompt(name)
            for name in (
                "meal_plan",
                "edit_plan",
                "edit_cart",
                "product_selection",
                "compact",
            )
        }

    async def _run(
        self,
        agent: Agent[None, Response],
        operation: str,
        payload: dict[str, JsonValue],
        usage: RunUsage,
        limits: UsageLimits | None = None,
    ) -> Response:
        """Учитывает также оплаченный ответ, не прошедший проверку формата.

        :param agent: Одноразовый SDK-вызов без инструментов
        :param operation: Имя этапа для метрик
        :param payload: Контекст операции
        :param usage: Общие счётчики хода
        :return: Типизированный ответ
        """
        started = time.monotonic()
        before = (usage.requests, usage.input_tokens, usage.output_tokens)
        async with agent.iter(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            usage=usage,
            usage_limits=limits or self.limits,
        ) as run:
            try:
                async for _ in run:
                    pass
            finally:
                cost = sum(
                    (m.provider_details or {}).get("cost", 0)
                    for m in run.all_messages()
                    if isinstance(m, ModelResponse)
                )
                usage.details["cost_nano_usd"] = usage.details.get("cost_nano_usd", 0) + round(cost * 1e9)
                logger.bind(
                    operation=operation,
                    model=agent.model.model_name,
                    elapsed_seconds=time.monotonic() - started,
                    requests=usage.requests - before[0],
                    input_tokens=usage.input_tokens - before[1],
                    output_tokens=usage.output_tokens - before[2],
                    cost_usd=cost,
                    completed=run.result is not None,
                ).info("Model operation completed")
            if run.result is None:
                raise UnexpectedModelBehavior("Модель не завершила операцию")
            if operation == "compact" and any(
                isinstance(m, ModelResponse) and m.finish_reason not in (None, "stop")
                for m in run.all_messages()
            ):
                raise UnexpectedModelBehavior("Сводка оборвана или заблокирована")
            return run.result.output

    async def generate(
        self,
        prompt: PromptName,
        schema: type[Output],
        payload: dict[str, JsonValue],
        usage: RunUsage,
        effort: Literal["low", "medium", "high"] = "low",
    ) -> Output:
        """Получает структурированный результат ровно одним модельным запросом.

        :param prompt: Имя инструкций
        :param schema: Тип результата
        :param payload: Только контекст операции
        :param usage: Общий счётчик хода, включая вложенные вызовы
        :param effort: Уровень reasoning; отдельный генератор меню использует planning_effort
        :return: Результат по схеме
        """
        model = self.model
        if prompt == "meal_plan" and self.planning_model is not None:
            model, effort = self.planning_model, self.planning_effort
        agent = Agent(
            model,
            name=f"{prompt}.{'repair' if payload.get('feedback') else 'generate'}",
            instrument=self.instrumentation,
            instructions=self.prompts[prompt],
            output_type=NativeOutput(schema, strict=True),
            retries=0,
            output_retries=0,
            model_settings={"openrouter_reasoning": {"effort": effort}},
        )
        return await self._run(agent, prompt, payload, usage)

    async def compact(
        self,
        payload: dict[str, JsonValue],
        usage: RunUsage,
        *,
        max_output_tokens: int = 16000,
        request_limit: int = 2,
    ) -> str:
        """Сжимает только завершённую историю, без tools.

        :param payload: Сводка и завершённые ходы
        :param usage: Общий счётчик
        :return: Новая сводка
        """
        agent = Agent(
            self.model,
            name="compaction",
            instrument=self.instrumentation,
            instructions=self.prompts["compact"],
            retries=0,
            output_retries=0,
            model_settings={"openrouter_reasoning": {"effort": "medium"}, "max_tokens": max_output_tokens},
        )
        result = await self._run(agent, "compact", payload, usage, UsageLimits(request_limit=request_limit))
        if not result.strip():
            raise UnexpectedModelBehavior("Пустая сводка")
        return result
