import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from app.catalog.search.service import RecipeRetriever
from app.catalog.store import Catalog
from app.config import get_config
from app.models import RecipeSearchLimits


async def execute(args: argparse.Namespace) -> None:
    """Готовит индекс или ищет рецепты без вызовов LLM.

    :param args: Параметры CLI
    """
    config = get_config()
    if args.lexical:
        config.retrieval_dense_enabled = False
    catalog = Catalog(
        config.recipes_database_path, config.products_database_path, config.sources_database_path
    )
    retriever = RecipeRetriever(catalog, config)
    started = time.monotonic()
    try:
        await retriever.prepare()
        preparation = round(time.monotonic() - started, 3)
        if args.command == "prepare":
            print(
                json.dumps(
                    {
                        "index_id": retriever.index_id,
                        "recipes": len(retriever.recipes),
                        "dense": retriever.matrix is not None,
                        "preparation_seconds": preparation,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "search":
            limits = RecipeSearchLimits.model_validate_json(args.limits)
            result = await retriever.search(args.query, limits)
            print(result.model_dump_json(indent=2))
        else:
            unlimited = RecipeSearchLimits(**{field: None for field in RecipeSearchLimits.model_fields})
            queries = [
                "омлет",
                "сырники",
                "омлет",
                "салат с курицей",
                "быстрый завтрак",
                "суп из чечевицы",
                "овощной ужин",
                "паста с грибами",
            ]
            limits = [unlimited for _ in queries]
            limits[1] = unlimited.model_copy(update={"exclude_products": ["творог"]})
            limits[2] = unlimited.model_copy(update={"forbidden_equipment": ["духовка"]})
            times, cases = [], []
            for _ in range(args.repeats):
                for query, conditions in zip(queries, limits, strict=True):
                    before = time.monotonic()
                    result = await retriever.select(query, conditions)
                    times.append((time.monotonic() - before) * 1000)
                    cases.append(
                        {
                            "query": query,
                            "ids": result.recipe_ids,
                            "limits": conditions.model_dump(),
                            "diagnostics": result.diagnostics,
                        }
                    )
            concurrent_started = time.monotonic()
            pair = await asyncio.gather(*(retriever.select(q, unlimited) for q in [queries[0], queries[3]]))
            report = {
                "index_id": retriever.index_id,
                "dense": retriever.matrix is not None,
                "preparation_seconds": preparation,
                "requests": len(times),
                "p50_ms": round(statistics.median(times), 2),
                "p95_ms": round(sorted(times)[min(len(times) - 1, int(len(times) * 0.95))], 2),
                "max_ms": round(max(times), 2),
                "concurrent_pair_ms": round((time.monotonic() - concurrent_started) * 1000, 2),
                "concurrent_degraded": sum("RETRIEVAL_DEGRADED" in r.diagnostics for r in pair),
                "cases": cases[: len(queries)],
            }
            value = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(value + "\n")
            print(value)
    finally:
        retriever.close()


def main() -> None:
    """Запускает подготовку, поиск или измерение локального RAG."""
    parser = argparse.ArgumentParser(description="Локальный RAG рецептов без LLM-ключа")
    parser.add_argument("--lexical", action="store_true", help="Явно отключить dense-ветку")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument(
        "--limits",
        default=json.dumps({field: None for field in RecipeSearchLimits.model_fields}),
        help="JSON со всеми полями RecipeSearchLimits; без флага CLI явно передаёт все null",
    )
    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("--repeats", type=int, choices=range(1, 21), default=3)
    benchmark.add_argument("--output")
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Ошибка RAG: {exc}\n")


if __name__ == "__main__":
    main()
