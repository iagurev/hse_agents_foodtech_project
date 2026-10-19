import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.profiles import InlineDefsJsonSchemaTransformer
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openrouter import OpenRouterProvider

from app.assistant.context import Context
from app.assistant.tools import build_toolset
from app.config import Config


def build_model(config: Config, http_client: httpx.AsyncClient, *, planning: bool = False) -> OpenRouterModel:
    """Создаёт модель с отключёнными транспортными повторами.

    :param config: Проверенные настройки
    :param http_client: Управляемый приложением HTTP-клиент
    :param planning: Использовать отдельную модель генерации меню
    :return: OpenRouter-модель
    """
    if not config.openrouter_api_key or not config.openrouter_api_key.get_secret_value():
        raise ValueError("Задайте OPENROUTER_API_KEY для запуска модели")
    client = AsyncOpenAI(
        api_key=config.openrouter_api_key.get_secret_value(),
        base_url=config.openrouter_base_url,
        http_client=http_client,
        max_retries=0,
        timeout=config.model_timeout_seconds,
    )
    return OpenRouterModel(
        config.planning_model if planning else config.model,
        provider=OpenRouterProvider(openai_client=client),
        profile=OpenAIModelProfile(
            supports_json_schema_output=True, json_schema_transformer=InlineDefsJsonSchemaTransformer
        ),
        settings={
            "openrouter_provider": {
                "require_parameters": True,
                "only": [config.planning_provider if planning else config.openrouter_provider],
                "allow_fallbacks": False,
            },
            "openrouter_reasoning": {"effort": "low"},
            "openrouter_usage": {"include": True},
        },
    )


def personal_instructions(ctx: RunContext[Context]) -> str:
    """Передаёт актуальный снимок как явно обозначенные данные.

    :param ctx: Зависимости текущего хода
    :return: Отдельный контекст без каталога
    """
    return ctx.deps.instructions(ctx.deps.chat_summary)


def build_agent(model: Model) -> Agent[Context, str]:
    """Подключает общие инструкции и отдельные Tool-объекты.

    :param model: Модель либо контролируемая замена для теста
    :return: Разговорный агент со штатным SDK-циклом
    """
    toolset = build_toolset()

    async def prepare_history(ctx: RunContext[Context], messages: list[ModelMessage]) -> list[ModelMessage]:
        if ctx.deps.compaction is None:
            raise RuntimeError("Session должна передать контроллер контекста")
        tools = await toolset.get_tools(ctx)
        return await ctx.deps.compaction.process(ctx, messages, [t.tool_def for t in tools.values()])

    return Agent(
        model,
        deps_type=Context,
        instructions=[personal_instructions],
        toolsets=[toolset],
        history_processors=[prepare_history],
        retries=0,
        output_retries=0,
        end_strategy="exhaustive",
    )
