from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

EventSink = Callable[[dict[str, JsonValue]], Awaitable[None]]


@dataclass
class Progress:
    """Сообщает реальные этапы одной операции; не добавляет данные в контекст модели."""

    emit: EventSink | None = None
    name: str = ""
    call_id: str | None = None
    counters: dict[str, JsonValue] = field(default_factory=dict)

    async def report(
        self, stage: str, *, completed: int | None = None, total: int | None = None, found: int | None = None
    ) -> None:
        """Передаёт этап и сохраняет счётчики поиска до конца этой операции.

        :param stage: Код этапа, заданный приложением
        :param completed: Число завершённых поисков, включая пустые и неуспешные
        :param total: Общее число уникальных продуктов
        :param found: Число продуктов с непустым набором кандидатов
        """
        if self.emit is None:
            return
        self.counters.update(
            {
                k: v
                for k, v in {"completed": completed, "total": total, "found": found}.items()
                if v is not None
            }
        )
        await self.emit(
            {"type": "progress", "name": self.name, "call_id": self.call_id, "stage": stage, **self.counters}
        )
