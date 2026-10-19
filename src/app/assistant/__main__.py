import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from loguru import logger

from app.assistant.agent import build_agent, build_model
from app.assistant.session import Session
from app.assistant.tokens import GLMTokenCounter
from app.catalog import Catalog
from app.config import get_config
from app.integrations import Vkusvill
from app.llm import ModelGateway
from app.network import system_tls_context
from app.retrieval.service import create_retriever
from app.storage import Store


async def execute(args: argparse.Namespace) -> None:
    """Собирает зависимости и запускает один/несколько ходов CLI.

    :param args: Аргументы CLI
    """
    config = get_config()
    logger.remove()
    logger.add(sys.stderr, level=config.log_level, serialize=True, diagnose=False)
    store = Store(config.database_path)
    await store.initialize()
    catalog = Catalog(
        config.recipes_database_path, config.products_database_path, config.sources_database_path
    )
    await catalog.validate()
    await store.ensure_chat(args.user, args.chat)
    if args.questionnaire:
        await store.set_questionnaire(
            args.user, json.loads(Path(args.questionnaire).read_text(encoding="utf-8"))
        )
    async with (
        httpx.AsyncClient(verify=system_tls_context()) as client,
        httpx.AsyncClient(
            timeout=config.integration_timeout_seconds, verify=system_tls_context()
        ) as product_client,
    ):
        model = build_model(config, client)
        retriever = await create_retriever(catalog, config)
        counter = await GLMTokenCounter.load(model, config.tokenizer_cache_path, client)
        session = Session(
            config,
            build_agent(model),
            store,
            catalog,
            Vkusvill(product_client, config.vkusvill_mcp_url, config.integration_timeout_seconds),
            ModelGateway(
                model,
                config.request_limit,
                planning_model=build_model(config, client, planning=True),
                planning_effort=config.planning_reasoning_effort,
            ),
            counter,
            retriever,
        )
        try:
            if args.message:
                print(
                    json.dumps(
                        await session.run(args.user, args.chat, args.message, args.turn_id),
                        ensure_ascii=False,
                    )
                )
                return
            while True:
                try:
                    text = await asyncio.to_thread(input, "Вы: ")
                except (EOFError, KeyboardInterrupt):
                    break
                if text.strip() in {"/exit", "/quit"}:
                    break
                if text.strip():
                    result = await session.run(args.user, args.chat, text)
                    print(result["text"])
        finally:
            if retriever:
                retriever.close()


def main() -> None:
    """Запускает локальный интерфейс; user/chat задаёт доверенное приложение."""
    parser = argparse.ArgumentParser(description="Ассистент по питанию")
    parser.add_argument("--user", default="local")
    parser.add_argument("--chat", default="main")
    parser.add_argument("--message")
    parser.add_argument("--turn-id")
    parser.add_argument("--questionnaire")
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except (ValueError, PermissionError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
