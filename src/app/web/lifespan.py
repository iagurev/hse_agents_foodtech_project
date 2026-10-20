import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from openai import AsyncOpenAI
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from app.assistant.agent import build_agent
from app.assistant.session import Session
from app.catalog.search.service import create_retriever
from app.catalog.store import Catalog
from app.config import Config
from app.integrations.tls import system_tls_context
from app.integrations.vkusvill import Vkusvill
from app.llm.gateway import ModelGateway
from app.llm.provider import build_model
from app.llm.tokens import load_counter
from app.shopping.service import Shopper
from app.tracing import Tracing
from app.web.guard import InputGuard
from app.web.objects import ObjectEditor
from app.web.runs import Runs
from app.web.storage import WebStore


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    config: Config,
    store: WebStore,
    catalog: Catalog,
    session: Session | None,
    guard: InputGuard | None,
) -> AsyncIterator[None]:
    """Открывает зависимости на время работы сервера и закрывает их при остановке."""
    await catalog.validate()
    await store.initialize()
    tracing = Tracing(config)
    async with (
        httpx.AsyncClient(verify=system_tls_context()) as client,
        httpx.AsyncClient(
            timeout=config.integration_timeout_seconds, verify=system_tls_context()
        ) as product_client,
        httpx.AsyncClient(timeout=config.image_timeout_seconds, verify=system_tls_context()) as media_client,
    ):
        active_session, active_guard = session, guard
        retriever = getattr(active_session, "retriever", None)
        try:
            if active_session is None:
                retriever = await create_retriever(catalog, config)
                if config.openrouter_api_key and config.openrouter_api_key.get_secret_value():
                    model = build_model(config, client)
                    counter = await load_counter(model)
                    active_session = Session(
                        config,
                        build_agent(model, tracing.instrumentation),
                        store,
                        catalog,
                        Vkusvill(product_client, config.vkusvill_mcp_url, config.integration_timeout_seconds),
                        ModelGateway(
                            model,
                            config.request_limit,
                            planning_model=build_model(config, client, planning=True),
                            planning_effort=config.planning_reasoning_effort,
                            instrumentation=tracing.instrumentation,
                        ),
                        counter,
                        retriever,
                    )
            if (
                active_guard is None
                and config.openrouter_api_key
                and config.openrouter_api_key.get_secret_value()
            ):
                guard_client = AsyncOpenAI(
                    api_key=config.openrouter_api_key.get_secret_value(),
                    base_url=config.openrouter_base_url,
                    http_client=client,
                    max_retries=0,
                    timeout=config.guard_timeout_seconds,
                )
                guard_model = OpenRouterModel(
                    config.guard_model,
                    provider=OpenRouterProvider(openai_client=guard_client),
                    settings={"openrouter_usage": {"include": True}},
                )
                active_guard = InputGuard(guard_model, tracing.instrumentation)
            app.state.runs = Runs(config, store, active_session, active_guard, catalog, tracing)
            app.state.media_client = media_client
            shopper = (
                active_session.shopper
                if active_session
                else Shopper(
                    store,
                    catalog,
                    Vkusvill(product_client, config.vkusvill_mcp_url, config.integration_timeout_seconds),
                    None,
                )
            )
            app.state.editor = ObjectEditor(store, catalog, shopper)
            app.state.retriever = retriever
            app.state.chat_enabled = active_session is not None and active_guard is not None
            yield
        finally:
            if hasattr(app.state, "runs"):
                await app.state.runs.close()
            if session is None and retriever:
                retriever.close()
            await asyncio.to_thread(tracing.close)
