import json

from pydantic import BaseModel, Field
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from app.assistant.prompts import load_prompt


class GuardDecision(BaseModel):
    allowed: bool = Field(description="Можно ли передать запрос разговорному агенту")
    message: str = Field(description="Понятный отказ, либо пустая строка для разрешённого запроса")


class InputGuard:
    """Один структурированный запрос без tools и автоматической коррекции."""

    def __init__(self, model: Model) -> None:
        self.agent = Agent(
            model,
            instructions=load_prompt("input_guard"),
            output_type=PromptedOutput(GuardDecision),
            retries=0,
            output_retries=0,
            model_settings={"max_tokens": 2048},
        )

    async def check(self, text: str, profile: dict, recent: list[dict], usage: RunUsage) -> GuardDecision:
        """Оценивает только пользовательский запрос и краткий контекст.

        :param text: Новое сообщение
        :param profile: Анкета как данные
        :param recent: Два последних разрешённых обмена без tools
        :param usage: Общий счётчик задачи
        :return: Решение и пользовательское пояснение
        """
        async with self.agent.iter(
            json.dumps({"message": text, "profile": profile, "recent": recent}, ensure_ascii=False),
            usage=usage,
            usage_limits=UsageLimits(request_limit=1),
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
