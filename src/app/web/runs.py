import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import HTTPException
from loguru import logger
from pydantic import JsonValue
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from app.assistant.session import Session
from app.config import Config
from app.web.guard import InputGuard
from app.web.presentation import artifacts, transcript
from app.web.storage import WebStore

ACTIVE = {"queued", "guarding", "running"}


class Runs:
    """Владеет фоновыми задачами одного процесса; браузер только наблюдает за ними."""

    def __init__(self, config: Config, store: WebStore, session: Session, guard: InputGuard) -> None:
        self.config, self.store, self.session, self.guard = config, store, session, guard
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.live: dict[str, dict[str, JsonValue]] = {}

    async def start(self, user_id: str, chat_id: str, turn_id: str, text: str) -> dict[str, JsonValue]:
        await self.store.chat(user_id, chat_id)
        if not text.strip():
            raise HTTPException(422, "Введите сообщение")
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            async with self.store.connection() as db:
                saved = await (
                    await db.execute(
                        "SELECT * FROM web_runs WHERE chat_id=? AND turn_id=?", (chat_id, turn_id)
                    )
                ).fetchone()
                if saved:
                    return {"turn_id": turn_id, "state": saved["state"]}
                if chat_id in self.tasks and not self.tasks[chat_id].done():
                    raise HTTPException(409, "В этом чате уже выполняется запрос")
                await db.execute(
                    "INSERT INTO web_runs(chat_id,turn_id,text,state,created_at) VALUES(?,?,?,?,?)",
                    (chat_id, turn_id, text, "queued", time.time()),
                )
                count = await (
                    await db.execute("SELECT count(*) n FROM web_runs WHERE chat_id=?", (chat_id,))
                ).fetchone()
                await db.execute(
                    "UPDATE chat_meta SET title=CASE WHEN ?=1 AND title='Новый чат' THEN ? ELSE title END,updated_at=? WHERE chat_id=?",
                    (count["n"], " ".join(text.split())[:64], time.time(), chat_id),
                )
                await db.commit()
            self.live[chat_id] = {
                "turn_id": turn_id,
                "state": "queued",
                "output": "",
                "artifacts": [],
                "tool": None,
            }
            self.tasks[chat_id] = asyncio.create_task(self._execute(user_id, chat_id, turn_id, text))
        return {"turn_id": turn_id, "state": "queued"}

    async def _persist(self, chat_id: str, report: dict | None = None) -> None:
        state = self.live[chat_id]
        async with self.store.connection() as db:
            await db.execute(
                "UPDATE web_runs SET state=?,output=?,report=? WHERE chat_id=? AND turn_id=?",
                (state["state"], state["output"], json.dumps(report or {}), chat_id, state["turn_id"]),
            )
            await db.commit()

    async def _excluded(self, chat_id: str, turn_id: str, text: str, output: str) -> None:
        raw = ModelMessagesTypeAdapter.dump_json(
            [ModelRequest(parts=[UserPromptPart(text)]), ModelResponse(parts=[TextPart(output)])]
        ).decode()
        async with self.store.connection() as db:
            await db.execute(
                "INSERT OR IGNORE INTO turns(chat_id,turn_id,messages,complete,model_visible) VALUES(?,?,?,1,0)",
                (chat_id, turn_id, raw),
            )
            await db.commit()

    async def _execute(self, user_id: str, chat_id: str, turn_id: str, text: str) -> None:
        state = self.live[chat_id]
        usage = RunUsage()
        started = time.monotonic()
        report = {}
        last_write = 0.0
        try:
            state["state"] = "guarding"
            await self._persist(chat_id)
            person = await self.store.person(user_id)
            _, _, rows = await self.store.history(chat_id)
            from app.web.presentation import project_messages

            recent = [
                {k: v for k, v in project_messages(r["messages"]).items() if k != "artifacts"}
                for r in rows[-2:]
            ]
            async with asyncio.timeout(
                min(self.config.guard_timeout_seconds, self.config.turn_timeout_seconds)
            ):
                decision = await self.guard.check(text, person.questionnaire, recent, usage)
            guard_seconds = time.monotonic() - started
            guard_metrics = {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "elapsed_seconds": guard_seconds,
                "cost_usd": usage.details.get("cost_nano_usd", 0) / 1e9,
            }
            report["guard"] = guard_metrics
            if not decision.allowed:
                state.update(state="blocked", output=decision.message or "Не могу помочь с этим запросом.")
                await self._excluded(chat_id, turn_id, text, state["output"])
            else:
                remaining = self.config.turn_timeout_seconds - guard_seconds
                if remaining <= 0:
                    raise TimeoutError("Task budget exhausted")
                state["state"] = "running"
                await self._persist(chat_id)

                async def emit(event: dict[str, JsonValue]) -> None:
                    nonlocal last_write, report
                    if event["type"] == "execution_report":
                        report = event["report"] | {"guard": guard_metrics}
                        return
                    if event["type"] == "text_delta":
                        state["output"] += event["text"]
                    elif event["type"] == "tool_start":
                        state["tool"] = event["name"]
                    elif event["type"] == "tool_end":
                        state["tool"] = None
                        found = state["artifacts"] + artifacts(event.get("content"))
                        state["artifacts"] = list({f"{c['kind']}:{c['id']}": c for c in found}.values())
                    if time.monotonic() - last_write > 0.5:
                        await self._persist(chat_id)
                        last_write = time.monotonic()

                report = await self.session.run(
                    user_id, chat_id, text, turn_id, emit, usage=usage, task_seconds=remaining
                )
                state.update(
                    state="error" if report.get("error") else "complete", output=report["text"], tool=None
                )
            report["guard"] = guard_metrics
        except asyncio.CancelledError:
            state.update(
                state="cancelled", output="Остановлено. Уже сохранённые результаты доступны.", tool=None
            )
            await asyncio.shield(self._excluded(chat_id, turn_id, text, state["output"]))
        except Exception as exc:
            logger.bind(
                chat_id=chat_id,
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
                stage=state["state"],
            ).warning("Web turn failed")
            output = (
                "Не удалось проверить запрос. Попробуйте отправить его ещё раз."
                if state["state"] == "guarding"
                else "Не удалось завершить запрос. Сохранённые результаты доступны."
            )
            state.update(state="error", output=output, tool=None)
            await self._excluded(chat_id, turn_id, text, output)
        finally:
            if "guard" not in report:
                report["guard"] = {
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "cost_usd": usage.details.get("cost_nano_usd", 0) / 1e9,
                }
            if not report.get("requests"):
                report |= {
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": usage.details.get("cost_nano_usd", 0) / 1e9,
                }
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            await asyncio.shield(self._persist(chat_id, report))

    async def cancel(self, chat_id: str) -> None:
        task = self.tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            state = self.live.get(chat_id)
            if state and state["state"] in ACTIVE:
                state.update(state="cancelled", output="Выполнение остановлено.", tool=None)
                async with self.store.connection() as db:
                    row = await (
                        await db.execute(
                            "SELECT text FROM web_runs WHERE chat_id=? AND turn_id=?",
                            (chat_id, state["turn_id"]),
                        )
                    ).fetchone()
                if row:
                    await self._excluded(chat_id, state["turn_id"], row["text"], state["output"])
                await self._persist(chat_id)

    async def close(self) -> None:
        for chat_id in list(self.tasks):
            await self.cancel(chat_id)

    async def events(self, chat_id: str, turn_id: str) -> AsyncIterator[str]:
        """Отдаёт заменяемые снимки состояния; повтор соединения не дублирует текст."""
        previous = ""
        heartbeat = time.monotonic()
        while True:
            state = self.live.get(chat_id)
            if not state or state["turn_id"] != turn_id:
                async with self.store.connection() as db:
                    row = await (
                        await db.execute(
                            "SELECT state,output FROM web_runs WHERE chat_id=? AND turn_id=?",
                            (chat_id, turn_id),
                        )
                    ).fetchone()
                if not row:
                    return
                state = {**dict(row), "turn_id": turn_id, "artifacts": [], "tool": None}
            encoded = json.dumps(state, ensure_ascii=False)
            if encoded != previous:
                yield f"data: {encoded}\n\n"
                previous = encoded
            if state["state"] not in ACTIVE:
                return
            if time.monotonic() - heartbeat >= 10:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(0.15)

    async def messages(self, chat_id: str) -> list[dict[str, JsonValue]]:
        result = await transcript(self.store, chat_id)
        live = self.live.get(chat_id)
        if live:
            for row in result:
                if row["turn_id"] == live["turn_id"] and live["state"] in ACTIVE:
                    row.update(live)
        return result
