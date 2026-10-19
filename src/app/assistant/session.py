import asyncio
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from uuid import uuid4

from loguru import logger
from pydantic import JsonValue
from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from app.assistant.compaction import Compaction, CompactionError, dialogue_text
from app.assistant.context import Context
from app.assistant.tokens import RequestCounter
from app.catalog import Catalog
from app.config import Config
from app.integrations import Products
from app.llm import Generator
from app.planning import Planner
from app.shopping import Shopper
from app.storage import Store

EventSink = Callable[[dict[str, JsonValue]], Awaitable[None]]


class Session:
    """Исполняет ходы нескольких чатов, изолируя контекст и сохраняя результаты."""

    def __init__(
        self,
        config: Config,
        agent: Agent[Context, str],
        store: Store,
        catalog: Catalog,
        products: Products,
        generator: Generator,
        counter: RequestCounter,
    ) -> None:
        self.config, self.agent, self.store, self.catalog = config, agent, store, catalog
        self.products, self.generator, self.counter = products, generator, counter
        self.planner, self.shopper = (
            Planner(store, catalog, generator),
            Shopper(store, catalog, products, generator),
        )
        self.locks: dict[str, asyncio.Lock] = {}

    async def _close_interrupted(
        self, context: Context, messages: list[ModelMessage], response_text: str
    ) -> list[ModelMessage]:
        calls = {p.tool_call_id: p for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
        returned = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
        missing = []
        for call_id, call in calls.items():
            if call_id not in returned:
                saved = await self.store.replay(context.user_id, context.operation_id(call_id))
                content = (
                    saved.model_dump(mode="json")
                    if saved
                    else {
                        "status": "error",
                        "error": {
                            "code": "INTERRUPTED",
                            "message": "Исполнение прервано; успех не подтверждён",
                        },
                    }
                )
                missing.append(ToolReturnPart(call.tool_name, content, call_id))
        if missing:
            messages.append(ModelRequest(parts=missing))
        messages.append(ModelResponse(parts=[TextPart(response_text)]))
        return messages

    async def run(
        self,
        user_id: str,
        chat_id: str,
        text: str,
        turn_id: str | None = None,
        emit: EventSink | None = None,
        usage: RunUsage | None = None,
        task_seconds: float | None = None,
    ) -> dict[str, JsonValue]:
        """Выполняет один ход; повтор доставки возвращает сохранённый ответ.

        :param user_id: Авторизованный пользователь из приложения
        :param chat_id: Чат
        :param text: Текущая реплика
        :param turn_id: Стабильный ключ доставки; новый при отсутствии
        :param emit: Получатель событий инструментов и текста
        :return: Ответ, состояние исполнения и измерения
        """
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            await self.store.ensure_chat(user_id, chat_id)
            turn_id = turn_id or uuid4().hex
            async with self.store.connection() as db:
                existing = await (
                    await db.execute(
                        "SELECT messages FROM turns WHERE chat_id=? AND turn_id=?", (chat_id, turn_id)
                    )
                ).fetchone()
            if existing:
                previous = ModelMessagesTypeAdapter.validate_json(existing["messages"])
                texts = [p.content for m in previous for p in m.parts if isinstance(p, TextPart)]
                return {
                    "text": texts[-1] if texts else "Ход уже обработан",
                    "replayed": True,
                    "turn_id": turn_id,
                }
            summary, _, turns = await self.store.history(chat_id)
            compaction = Compaction(self.config, self.store, self.generator, self.counter, summary, turns)
            history = compaction.history()
            person = await self.store.person(user_id)
            person.dialogue = summary + "\n" + dialogue_text(history) + "\nПользователь: " + text
            context = Context(
                user_id,
                chat_id,
                turn_id,
                person,
                self.store,
                self.catalog,
                self.planner,
                self.shopper,
                self.products,
                summary,
                compaction,
            )
            usage = usage if usage is not None else RunUsage()
            started = time.monotonic()
            new_messages: list[ModelMessage] = []
            response_text = ""
            streamed_text: list[str] = []
            failure = None

            async def events(ctx: RunContext[Context], stream: AsyncIterable) -> None:
                async for event in stream:
                    if emit is None:
                        continue
                    if isinstance(event, FunctionToolCallEvent):
                        await emit({"type": "tool_start", "name": event.part.tool_name})
                    elif isinstance(event, FunctionToolResultEvent):
                        await emit(
                            {
                                "type": "tool_end",
                                "name": event.result.tool_name,
                                "content": event.result.content.model_dump(mode="json")
                                if hasattr(event.result.content, "model_dump")
                                else event.result.content,
                            }
                        )
                    elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                        streamed_text.append(event.delta.content_delta)
                        await emit({"type": "text_delta", "text": event.delta.content_delta})
                    elif isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                        streamed_text.append(event.part.content)
                        await emit({"type": "text_delta", "text": event.part.content})

            with capture_run_messages() as captured:
                try:
                    async with asyncio.timeout(
                        task_seconds if task_seconds is not None else self.config.turn_timeout_seconds
                    ) as timer:
                        compaction.timer = timer
                        result = await self.agent.run(
                            text,
                            deps=context,
                            message_history=history,
                            usage=usage,
                            usage_limits=UsageLimits(request_limit=self.config.request_limit),
                            event_stream_handler=events if emit else None,
                            model_settings={"max_tokens": self.config.response_reserve_tokens},
                        )
                        new_messages = result.new_messages()
                        response_text = result.output
                except (Exception, asyncio.CancelledError) as exc:
                    failure = type(exc).__name__
                    new_messages = (
                        compaction.current(captured)
                        if captured
                        else [ModelRequest(parts=[UserPromptPart(text)])]
                    )
                    if not new_messages:
                        new_messages = [ModelRequest(parts=[UserPromptPart(text)])]
                    # SDK может не записать незавершённый streaming-response при отмене.
                    visible = "".join(streamed_text)
                    saved_text = "".join(
                        p.content for m in new_messages for p in m.parts if isinstance(p, TextPart)
                    )
                    if visible.startswith(saved_text) and len(visible) > len(saved_text):
                        new_messages.append(ModelResponse(parts=[TextPart(visible[len(saved_text) :])]))
                    response_text = (
                        "Выполнение остановлено. Уже сохранённые результаты доступны."
                        if isinstance(exc, asyncio.CancelledError)
                        else "Не удалось завершить ход. Уже сохранённые планы и корзины остаются доступны."
                    )
                    if isinstance(exc, CompactionError):
                        response_text = str(exc)
                        logger.bind(chat_id=chat_id, error_type=failure, stop_reason=str(exc)).warning(
                            "Context preparation failed"
                        )
                    else:
                        logger.bind(chat_id=chat_id, error_type=failure).opt(exception=exc).warning(
                            "Conversation turn interrupted"
                        )
                    new_messages = await asyncio.shield(
                        self._close_interrupted(context, list(new_messages), response_text)
                    )
                finally:
                    if new_messages:
                        await asyncio.shield(
                            self.store.save_turn(
                                chat_id,
                                turn_id,
                                ModelMessagesTypeAdapter.dump_json(new_messages).decode(),
                                True,
                            )
                        )
            cost = sum(
                (m.provider_details or {}).get("cost", 0)
                for m in new_messages
                if isinstance(m, ModelResponse)
            )
            cost += usage.details.get("cost_nano_usd", 0) / 1e9
            maintenance = compaction.report()
            cost += maintenance["cost_usd"]
            report = {
                "text": response_text,
                "turn_id": turn_id,
                "error": failure,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "requests": usage.requests + maintenance["requests"],
                "input_tokens": usage.input_tokens + maintenance["input_tokens"],
                "output_tokens": usage.output_tokens + maintenance["output_tokens"],
                "cost_usd": cost,
                "compaction": maintenance,
            }
            logger.bind(chat_id=chat_id, **{k: v for k, v in report.items() if k != "text"}).info(
                "Conversation turn completed"
            )
            if failure == "CancelledError":
                if emit:
                    await asyncio.shield(emit({"type": "execution_report", "report": report}))
                raise asyncio.CancelledError
            return report
