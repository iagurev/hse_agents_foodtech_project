import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from app.catalog import Catalog
from app.config import get_config
from app.models import PersonContext
from app.retrieval.models import SearchConstraints
from app.retrieval.service import RecipeRetriever


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
            person = (
                PersonContext.model_validate_json(Path(args.person_file).read_text())
                if args.person_file
                else PersonContext()
            )
            constraints = (
                SearchConstraints.model_validate_json(args.constraints) if args.constraints else None
            )
            result = await retriever.search(args.query, person, constraints)
            print(result.model_dump_json(indent=2))
        else:
            queries = [
                "омлет",
                "сырники без творога",
                "ужин без духовки",
                "салат с курицей",
                "быстрый завтрак",
                "суп из чечевицы",
                "овощной ужин",
                "паста с грибами",
            ]
            times, cases = [], []
            for _ in range(args.repeats):
                for query in queries:
                    before = time.monotonic()
                    result = await retriever.search(query)
                    times.append((time.monotonic() - before) * 1000)
                    cases.append(
                        {
                            "query": query,
                            "ids": [r["recipe_id"] for r in result.data["recipes"]],
                            "diagnostics": result.data["diagnostics"],
                        }
                    )
            concurrent_started = time.monotonic()
            pair = await asyncio.gather(*(retriever.search(q) for q in [queries[0], queries[3]]))
            report = {
                "index_id": retriever.index_id,
                "dense": retriever.matrix is not None,
                "preparation_seconds": preparation,
                "requests": len(times),
                "p50_ms": round(statistics.median(times), 2),
                "p95_ms": round(sorted(times)[min(len(times) - 1, int(len(times) * 0.95))], 2),
                "max_ms": round(max(times), 2),
                "concurrent_pair_ms": round((time.monotonic() - concurrent_started) * 1000, 2),
                "concurrent_degraded": sum(
                    any(d["code"] == "RETRIEVAL_DEGRADED" for d in r.data["diagnostics"]) for r in pair
                ),
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
    search.add_argument("--constraints", help="SearchConstraints в JSON")
    search.add_argument("--person-file", help="JSON PersonContext")
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
