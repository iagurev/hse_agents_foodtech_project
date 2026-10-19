import asyncio
import dataclasses
import json
import time
from typing import TYPE_CHECKING

from loguru import logger
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from app.assistant.prompts import load_prompt
from app.assistant.tokens import RequestCounter
from app.config import Config
from app.llm import Generator
from app.storage import Store

if TYPE_CHECKING:
    from app.assistant.context import Context


class CompactionError(RuntimeError):
    """Сжатие не завершилось; исходная история остаётся доступной."""


class ContextOverflowError(CompactionError):
    """Даже минимальный контекст превышает безопасный вход."""


def history_for_model(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Убирает reasoning только из завершённой истории.

    :param messages: Завершённые сообщения
    :return: Копии, исходный журнал не меняется
    """
    return [
        dataclasses.replace(m, parts=[p for p in m.parts if not isinstance(p, ThinkingPart)])
        if isinstance(m, ModelResponse)
        else m
        for m in messages
    ]


def dialogue_text(messages: list[ModelMessage]) -> str:
    """Выбирает реплики без результатов tools и reasoning.

    :param messages: История для модели
    :return: Контекст условий внутренних операций
    """
    return "\n".join(
        f"Пользователь: {p.content}" if isinstance(p, UserPromptPart) else f"Ассистент: {p.content}"
        for m in messages
        for p in m.parts
        if isinstance(p, (UserPromptPart, TextPart))
    )


class Compaction:
    """Состояние сжатия одного пользовательского хода, без собственного агентского цикла."""

    def __init__(
        self,
        config: Config,
        store: Store,
        generator: Generator,
        counter: RequestCounter,
        summary: str,
        turns: list[dict],
    ) -> None:
        self.config, self.store, self.generator, self.counter = config, store, generator, counter
        self.summary = summary
        self.turns = turns
        self.blocks = [
            [
                dataclasses.replace(m, run_id=f"history:{t['seq']}")
                for m in history_for_model(ModelMessagesTypeAdapter.validate_json(t["messages"]))
            ]
            for t in turns
        ]
        self.historical_ids = {f"history:{t['seq']}" for t in turns}
        self.usage = RunUsage()
        self.calls = 0
        self.elapsed = 0.0
        self.timer: asyncio.Timeout | None = None

    def history(self) -> list[ModelMessage]:
        """Возвращает несжатый хвост для SDK."""
        return [m for block in self.blocks for m in block]

    def current(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """Находит текущий ход независимо от изменения длины истории.

        :param messages: SDK-журнал, в том числе после сжатия или сбоя
        :return: Все сообщения текущего хода
        """
        return [m for m in messages if m.run_id not in self.historical_ids]

    def _project(
        self, deps: "Context", current: list[ModelMessage], blocks: list[list[ModelMessage]], summary: str
    ) -> list[ModelMessage]:
        messages = [m for block in blocks for m in block] + current
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], ModelRequest):
                messages[i] = dataclasses.replace(messages[i], instructions=deps.instructions(summary))
                break
        return messages

    async def process(
        self, ctx: RunContext["Context"], messages: list[ModelMessage], tools: list[ToolDefinition]
    ) -> list[ModelMessage]:
        """Проверяет каждый запрос, при необходимости сворачивает только завершённые ходы.

        :param ctx: Зависимости и идентификатор SDK-вызова
        :param messages: История перед запросом модели
        :param tools: Definitions текущего toolset
        :return: Проверенная проекция истории
        """
        current = self.current(messages)
        projected = self._project(ctx.deps, current, self.blocks, self.summary)
        before = await self.counter.count(projected, tools)
        if before < self.config.compaction_threshold_tokens:
            return projected
        # После сворачивания всей истории большому текущему циклу доступен остаток до hard limit.
        if not self.blocks and await self.counter.text(self.summary) <= self.config.summary_max_tokens:
            self._check_limit(before)
            return projected
        started = time.monotonic()
        deadline = self.timer.when() if self.timer else None
        if self.timer:
            self.timer.reschedule(None)
        try:
            remaining = self.config.compaction_timeout_seconds - self.elapsed
            if remaining <= 0:
                raise CompactionError("Исчерпано время сжатия истории. Выполненные действия сохранены.")
            async with asyncio.timeout(remaining):
                return await self._compress(ctx, current, tools, before)
        except (CompactionError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise CompactionError(
                "Не удалось сжать историю. Переписка и выполненные действия сохранены."
            ) from exc
        finally:
            spent = time.monotonic() - started
            self.elapsed += spent
            if self.timer and deadline is not None:
                self.timer.reschedule(deadline + spent)

    def _check_limit(self, tokens: int) -> None:
        if tokens > self.config.max_input_tokens:
            raise ContextOverflowError(
                "Текущая задача превышает доступный контекст. Уже выполненные действия сохранены; "
                "продолжите с более коротким запросом или в новом чате."
            )

    async def _compress(
        self,
        ctx: RunContext["Context"],
        current: list[ModelMessage],
        tools: list[ToolDefinition],
        before: int,
    ) -> list[ModelMessage]:
        keep = 0
        for candidate in (6, 3, 1, 0):
            if candidate >= len(self.blocks) and self.blocks:
                continue
            tail = self.blocks[-candidate:] if candidate else []
            size = await self.counter.count(self._project(ctx.deps, current, tail, ""), tools)
            if size + self.config.summary_max_tokens <= self.config.compaction_threshold_tokens:
                keep = candidate
                break
        tail = self.blocks[-keep:] if keep else []
        # Не обращаемся к суммаризатору, если один текущий цикл уже превышает hard limit.
        self._check_limit(await self.counter.count(self._project(ctx.deps, current, tail, ""), tools))
        removed_count = len(self.blocks) - keep
        if not all(t["complete"] for t in self.turns[:removed_count]):
            raise CompactionError("В старой истории есть незавершённый ход; сжатие остановлено.")
        pending = self.blocks[:removed_count]
        summary = self.summary
        # Даже прежняя сводка без новых ходов может требовать сокращения (например, после миграции).
        first = True
        while pending or first:
            first = False
            batch: list[list[ModelMessage]] = []
            # Размер монотонно растёт; двоичный поиск сохраняет целые ходы и не токенизирует O(n²).
            lo, hi = 0, len(pending)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                payload = self._payload(summary, pending[:mid])
                if await self._summary_input_size(payload) <= self.config.max_input_tokens:
                    lo = mid
                else:
                    hi = mid - 1
            if pending and lo == 0:
                raise CompactionError("Один из прошлых ходов слишком велик для безопасного сжатия.")
            batch = pending[:lo]
            summary = await self._summarize(self._payload(summary, batch))
            pending = pending[lo:]
            if await self.counter.text(summary) > self.config.summary_max_tokens:
                summary = await self._summarize(
                    {
                        "previous_summary": summary,
                        "history": [],
                        "max_summary_tokens": self.config.summary_max_tokens,
                        "instruction": "Сократи сводку до указанного предела, сохранив существенные факты и ID.",
                    }
                )
                if await self.counter.text(summary) > self.config.summary_max_tokens:
                    raise CompactionError("Сводка осталась слишком длинной. Исходная история сохранена.")
        projected = self._project(ctx.deps, current, tail, summary)
        after = await self.counter.count(projected, tools)
        self._check_limit(after)
        boundary = self.turns[removed_count - 1]["seq"] if removed_count else None
        if boundary is None:
            _, boundary, _ = await self.store.history(ctx.deps.chat_id)
        await self.store.save_summary(ctx.deps.chat_id, boundary, summary)
        self.summary, self.blocks = summary, tail
        self.turns = self.turns[removed_count:]
        ctx.deps.chat_summary = summary
        ctx.deps.person.dialogue = summary + "\n" + dialogue_text(projected)
        logger.bind(
            chat_id=ctx.deps.chat_id,
            input_before=before,
            input_after=after,
            kept_turns=len(tail),
            summary_tokens=await self.counter.text(summary),
        ).info("Conversation context compacted")
        return projected

    def _payload(self, summary: str, blocks: list[list[ModelMessage]]) -> dict:
        messages = [
            dataclasses.replace(m, instructions=None) if isinstance(m, ModelRequest) else m
            for block in blocks
            for m in block
        ]
        return {
            "previous_summary": summary,
            "history": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
            "max_summary_tokens": self.config.summary_max_tokens,
            "user_messages": [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)],
        }

    async def _summary_input_size(self, payload: dict) -> int:
        return await self.counter.count(
            [
                ModelRequest(
                    parts=[UserPromptPart(json.dumps(payload, ensure_ascii=False))],
                    instructions=load_prompt("compact"),
                )
            ],
            [],
        )

    async def _summarize(self, payload: dict) -> str:
        self._check_limit(await self._summary_input_size(payload))
        if self.calls >= self.config.compaction_request_limit:
            raise CompactionError("Исчерпан лимит вызовов сжатия. Исходная история сохранена.")
        self.calls += 1
        result = await self.generator.compact(
            payload,
            self.usage,
            max_output_tokens=self.config.response_reserve_tokens,
            request_limit=self.config.compaction_request_limit,
        )
        if not result.strip():
            raise CompactionError("Модель вернула пустую сводку. Исходная история сохранена.")
        return result

    def report(self) -> dict:
        """Возвращает метрики без текста истории и сводки."""
        return {
            "requests": self.calls,
            "elapsed_seconds": round(self.elapsed, 3),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": self.usage.details.get("cost_nano_usd", 0) / 1e9,
        }
