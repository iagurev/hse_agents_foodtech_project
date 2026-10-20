import json

from loguru import logger
from pydantic import BaseModel, Field
from pydantic_ai import Agent, InstrumentationSettings, PromptedOutput
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from app.assistant.prompts import load_prompt


class GuardDecision(BaseModel):
    allowed: bool = Field(description="Можно ли передать запрос разговорному агенту")
    message: str = Field(description="Понятный отказ, либо пустая строка для разрешённого запроса")


class InputGuard:
    """Проверка без tools; одна резервная попытка только при временном отказе провайдера."""

    def __init__(
        self,
        model: Model,
        instrumentation: InstrumentationSettings | bool = False,
        *,
        fallback_model: Model | None = None,
    ) -> None:
        self.agent = Agent(
            model,
            name="input_guard",
            instrument=instrumentation,
            instructions=load_prompt("input_guard"),
            output_type=PromptedOutput(GuardDecision),
            retries=0,
            output_retries=0,
            model_settings={"max_tokens": 2048},
        )
        self.fallback = (
            Agent(
                fallback_model,
                name="input_guard_fallback",
                instrument=instrumentation,
                instructions=load_prompt("input_guard"),
                output_type=PromptedOutput(GuardDecision),
                retries=0,
                output_retries=0,
                model_settings={"max_tokens": 2048, "openrouter_reasoning": {"effort": "low"}},
            )
            if fallback_model
            else None
        )

    async def check(self, text: str, profile: dict, recent: list[dict], usage: RunUsage) -> GuardDecision:
        """Оценивает только пользовательский запрос и краткий контекст.

        :param text: Новое сообщение
        :param profile: Анкета как данные
        :param recent: Два последних разрешённых обмена без tools
        :param usage: Общий счётчик задачи
        :return: Решение и пользовательское пояснение
        """
        payload = json.dumps({"message": text, "profile": profile, "recent": recent}, ensure_ascii=False)
        try:
            return await self._check(self.agent, payload, usage)
        except ModelHTTPError as exc:
            if self.fallback is None or exc.status_code not in {429, 500, 502, 503, 504}:
                raise
            logger.bind(status_code=exc.status_code).warning(
                "Input guard provider unavailable; using fallback"
            )
            return await self._check(self.fallback, payload, usage)

    async def _check(self, agent: Agent[None, GuardDecision], payload: str, usage: RunUsage) -> GuardDecision:
        """Исполняет ровно одну попытку и учитывает её usage даже при ошибке формата."""
        async with agent.iter(
            payload,
            usage=usage,
            usage_limits=UsageLimits(request_limit=usage.requests + 1),
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
            if run.result is None:
                raise ValueError("Проверка запроса не завершилась")
            return run.result.output
