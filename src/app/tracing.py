import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from langfuse import Langfuse, propagate_attributes
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import Span, Status, StatusCode
from pydantic_ai import InstrumentationSettings
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters

from app.config import Config

_tracer: ContextVar[trace.Tracer | None] = ContextVar("app_tracer", default=None)


class SpanDefaults(SpanProcessor):
    """Передаёт окружение также нативным SDK-spans, созданным без Langfuse API."""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        span.set_attribute("langfuse.environment", self.environment)


class ModelInstrumentation(InstrumentationSettings):
    """Дополняет нативные spans фактической ценой OpenRouter вместо тарифной оценки."""

    def handle_messages(
        self,
        input_messages: list[ModelMessage],
        response: ModelResponse,
        system: str,
        span: Span,
        parameters: ModelRequestParameters | None = None,
    ) -> None:
        """Записывает нативный протокол и итоги без повторного сложения reasoning."""
        super().handle_messages(input_messages, response, system, span, parameters)
        usage = response.usage
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps(
                {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "total": usage.input_tokens + usage.output_tokens,
                }
            ),
        )
        span.set_attribute("langfuse.observation.metadata.usage_breakdown", json.dumps(usage.details))
        cost = (response.provider_details or {}).get("cost")
        if isinstance(cost, (int, float)):
            span.set_attribute("langfuse.observation.cost_details", json.dumps({"total": cost}))
            span.set_attribute("langfuse.observation.metadata.cost_source", "openrouter")


class Tracing:
    """Изолированный exporter: без глобальной инструментации HTTP, SQL и чужих агентов."""

    def __init__(self, config: Config) -> None:
        self.provider = (
            TracerProvider(resource=Resource.create({"service.name": "k-stolu"}))
            if config.langfuse_enabled
            else None
        )
        if self.provider:
            self.provider.add_span_processor(SpanDefaults(config.langfuse_environment))
        self.client = (
            Langfuse(
                public_key=config.langfuse_public_key,
                secret_key=config.langfuse_secret_key.get_secret_value(),
                base_url=config.langfuse_base_url,
                environment=config.langfuse_environment,
                tracer_provider=self.provider,
                timeout=5,
            )
            if self.provider
            else None
        )
        self.instrumentation = (
            ModelInstrumentation(
                tracer_provider=self.provider,
                include_binary_content=False,
                version=3,
                use_aggregated_usage_attribute_names=True,
            )
            if self.provider
            else False
        )

    @contextmanager
    def turn(self, user_id: str, chat_id: str, turn_id: str, text: str) -> Iterator[Span]:
        """Группирует новый пользовательский ход; личность задаёт runtime, не модель."""
        if self.provider is None:
            yield trace.INVALID_SPAN
            return
        token = _tracer.set(self.provider.get_tracer("app"))
        try:
            with propagate_attributes(
                user_id=user_id,
                session_id=chat_id,
                trace_name="chat.turn",
                tags=["k-stolu"],
                metadata={"turn_id": turn_id},
            ):
                with stage("chat.turn", text) as span:
                    span.set_attribute("langfuse.trace.name", "chat.turn")
                    span.set_attribute("langfuse.trace.input", text)
                    yield span
        finally:
            _tracer.reset(token)

    def close(self) -> None:
        """Дожидается очереди при завершении процесса; вызывается вне event loop."""
        if self.client:
            self.client.shutdown()
        if self.provider:
            self.provider.shutdown()


@contextmanager
def stage(name: str, input_data: object = None) -> Iterator[Span]:
    """Измеряет значимый этап в текущем ходе, не собирая аргументы функций автоматически."""
    tracer = _tracer.get()
    if tracer is None:
        yield trace.INVALID_SPAN
        return
    with tracer.start_as_current_span(name) as span:
        if input_data is not None:
            span.set_attribute("langfuse.observation.input", json.dumps(input_data, ensure_ascii=False))
        try:
            yield span
        except BaseException as exc:
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def outcome(span: Span, result: dict) -> None:
    """Пишет итог и статус, не добавляя суммарные расходы второй раз как generation."""
    span.set_attribute("langfuse.observation.output", json.dumps(result, ensure_ascii=False, default=str))
    state = result.get("state") or result.get("status")
    if state in {"error", "cancelled", "interrupted"}:
        span.set_attribute("langfuse.observation.level", "ERROR")
        span.set_status(Status(StatusCode.ERROR, str(state)))
    elif state in {"blocked", "partial", "needs_revision"}:
        span.set_attribute("langfuse.observation.level", "WARNING")
    if state:
        span.set_attribute("langfuse.observation.status_message", str(state))
