from pydantic_ai import Agent, InstrumentationSettings, RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from app.assistant.context import Context
from app.assistant.tools import build_toolset


def personal_instructions(ctx: RunContext[Context]) -> str:
    """Передаёт актуальный снимок как явно обозначенные данные.

    :param ctx: Зависимости текущего хода
    :return: Отдельный контекст без каталога
    """
    return ctx.deps.instructions(ctx.deps.chat_summary)


def build_agent(model: Model, instrumentation: InstrumentationSettings | bool = False) -> Agent[Context, str]:
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
        name="assistant",
        instrument=instrumentation,
        deps_type=Context,
        instructions=[personal_instructions],
        toolsets=[toolset],
        history_processors=[prepare_history],
        retries=0,
        output_retries=0,
        end_strategy="exhaustive",
    )
