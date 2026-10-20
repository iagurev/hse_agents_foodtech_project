import asyncio
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from loguru import logger

from app.catalog import Catalog
from app.config import Config
from app.models import Recipe, RecipeSearchLimits, Result
from app.retrieval.encoder import Encoder, SentenceEncoder
from app.retrieval.features import STOP, Language, RecipeFilter
from app.retrieval.index import prepare_index


@dataclass
class SearchSelection:
    """Внутренний результат: существующие ID и технические коды для логов/CLI."""

    recipe_ids: list[str]
    diagnostics: list[str]


class RecipeRetriever:
    """Гибридный поиск рецептов с изолированным CPU worker."""

    def __init__(self, catalog: Catalog, config: Config, encoder: Encoder | None = None) -> None:
        self.catalog, self.config, self.encoder = catalog, config, encoder
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recipe-encoder")
        self.busy: asyncio.Future | None = None
        self.waiting = False
        self.recipes: dict[str, Recipe] = {}
        self.index_id = ""
        self.matrix: np.ndarray | None = None

    async def prepare(self, *, load_model: bool = True) -> None:
        """Готовит модель и индекс до обработки пользовательских запросов.

        :param load_model: Загружать production encoder, если не внедрён тестовый
        """
        await self.catalog.validate()
        recipes, self.products = await asyncio.gather(self.catalog.all(), self.catalog.products())
        self.recipes = {r.recipe_id: r for r in recipes}
        self.language = await asyncio.to_thread(Language)
        async with self.catalog.connection() as db:
            aliases = dict(
                await (await db.execute("SELECT alias,product_id FROM product_aliases")).fetchall()
            )
        if self.encoder is None and load_model and self.config.retrieval_dense_enabled:
            try:
                self.encoder = await asyncio.to_thread(
                    SentenceEncoder, self.config.retrieval_model_cache_path, self.config.retrieval_threads
                )
            except Exception as exc:
                logger.bind(error_type=type(exc).__name__).warning(
                    "Semantic encoder unavailable; lexical retrieval remains active"
                )
        paths = [self.catalog.recipes_path, self.catalog.products_path, self.catalog.sources_path]
        try:
            self.index_id, self.path, self.documents, self.matrix = await asyncio.to_thread(
                prepare_index,
                paths,
                self.config.retrieval_cache_path,
                recipes,
                self.products,
                self.language,
                self.encoder,
            )
        except Exception as exc:
            if self.encoder is None:
                raise
            logger.bind(error_type=type(exc).__name__).warning(
                "Dense index build failed; rebuilding lexical index"
            )
            self.encoder = None
            self.index_id, self.path, self.documents, self.matrix = await asyncio.to_thread(
                prepare_index,
                paths,
                self.config.retrieval_cache_path,
                recipes,
                self.products,
                self.language,
                None,
            )
        self.filters = await asyncio.to_thread(
            RecipeFilter,
            self.recipes,
            self.products,
            aliases,
            self.language,
            {d["recipe_id"]: d["features"] for d in self.documents},
        )
        self.titles = {d["recipe_id"]: set(d["title_terms"]) for d in self.documents}
        self.ids = [r.recipe_id for r in recipes]
        self.source_stats = {p: (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) for p in paths}
        logger.bind(index_id=self.index_id, recipes=len(recipes), dense=self.matrix is not None).info(
            "Recipe retrieval ready"
        )

    def close(self) -> None:
        """Закрывает executor без преждевременного освобождения занятого слота."""
        self.worker.shutdown(wait=False, cancel_futures=True)

    def _lexical(self, words: list[str], allowed: set[str]) -> list[str]:
        """Находит лемматизированные BM25-кандидаты с параметризованным MATCH.

        :param words: Положительные леммы
        :param allowed: Допустимые рецепты
        :return: Ранжированные ID после фильтрации
        """
        if not words:
            return []
        expression = " OR ".join(f'"{word}"' for word in dict.fromkeys(words))
        with sqlite3.connect((self.path / "lexical.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT recipe_id FROM search WHERE search MATCH ? ORDER BY bm25(search,0,5,2,0.3),recipe_id",
                (expression,),
            )
            return [rid for (rid,) in rows if rid in allowed][:60]

    async def _rank(
        self, query: str, allowed: set[str], diagnostics: list[str], deadline: float
    ) -> list[str]:
        """Объединяет лексический и векторный ранги, ограничивая CPU inference.

        :param query: Название блюда или ингредиентов без извлечения условий из текста
        :param allowed: Допустимые ID
        :param diagnostics: Диагностика деградации
        :param deadline: Общий срок окончания поиска
        :return: Объединённый порядок кандидатов
        """
        words = [w for w in self.language.words(query) if w not in STOP]
        lexical = await asyncio.to_thread(self._lexical, words, allowed)
        semantic: list[str] = []
        if self.encoder is not None and self.matrix is not None and words:
            try:
                if self.busy is not None and not self.busy.done():
                    if self.waiting:
                        raise TimeoutError("Encoder queue full")
                    self.waiting = True
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self.busy), timeout=max(0.001, deadline - time.monotonic())
                        )
                    finally:
                        self.waiting = False
                self.busy = asyncio.get_running_loop().run_in_executor(
                    self.worker, self.encoder.encode, [query[:2000]]
                )
                self.busy.add_done_callback(
                    lambda future: future.exception() if not future.cancelled() else None
                )
                vector = await asyncio.wait_for(
                    asyncio.shield(self.busy), timeout=max(0.001, deadline - time.monotonic())
                )
                scores = self.matrix @ vector[0]
                semantic = [
                    self.ids[int(i)]
                    for i in np.argsort(-scores, kind="stable")
                    if self.ids[int(i)] in allowed and scores[int(i)] >= 0.45
                ][:60]
            except (TimeoutError, ValueError, RuntimeError, OSError) as exc:
                logger.bind(error_type=type(exc).__name__, index_id=self.index_id).warning(
                    "Dense retrieval degraded"
                )
                diagnostics.append("RETRIEVAL_DEGRADED")
        elif self.encoder is None or self.matrix is None:
            diagnostics.append("RETRIEVAL_DEGRADED")
        scores = {}
        for branch in [lexical, semantic]:
            for rank, rid in enumerate(branch, 1):
                scores[rid] = scores.get(rid, 0) + 1 / (60 + rank)
        terms = set(words)
        for rid in scores:
            title = self.titles[rid]
            scores[rid] += 0.015 * len(terms & title) / max(1, len(terms))
            if terms and terms <= title:
                scores[rid] += 0.04
        return sorted(scores, key=lambda rid: (-scores[rid], rid))

    async def select(self, query: str, limits: RecipeSearchLimits) -> SearchSelection:
        """Фильтрует по limits и ранжирует по неизменённому query до десяти ID.

        :param query: Блюдо или ингредиенты для поиска по словам и смыслу
        :param limits: Явные фильтры; профиль и память не подставляются
        :return: Существующие ID и внутренняя диагностика
        """
        started = time.monotonic()
        if any(
            (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) != stat
            for p, stat in self.source_stats.items()
        ):
            logger.bind(index_id=self.index_id).warning("Recipe catalog changed; restart required")
            return SearchSelection([], ["RETRIEVAL_DEGRADED"])
        allowed, diagnostics = self.filters.apply(limits)
        # Совпадение названного блюда — часть релевантности, не извлечение ограничений.
        dishes = set(
            self.language.words(
                "омлет яичница сырники блины оладьи суп салат паста ризотто каша котлеты запеканка вафли пицца смузи рагу борщ"
            )
        )
        requested = set(self.language.words(query)) & dishes
        if requested and not any(word in query.lower() for word in ["похож", "аналог", "вместо"]):
            allowed = {
                rid
                for rid in allowed
                if requested
                & set(
                    self.language.words(
                        re.split(r"\s+(?:к|для)\s+", self.recipes[rid].title, maxsplit=1, flags=re.I)[0]
                    )
                )
            }
        if not (set(self.language.words(query)) - STOP):
            ranked = sorted(allowed, key=lambda rid: (self.recipes[rid].total_minutes, rid))
        elif allowed:
            ranked = await self._rank(
                query, allowed, diagnostics, started + self.config.retrieval_timeout_seconds
            )
        else:
            ranked = []
        ranked = ranked[:10]
        if not ranked:
            diagnostics.append("NO_RELEVANT_MATCH")
        logger.bind(
            index_id=self.index_id,
            diagnostics=diagnostics,
            candidates=len(ranked),
            allowed=len(allowed),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        ).info("Recipe retrieval completed")
        return SearchSelection(ranked, diagnostics)

    async def search(self, query: str, limits: RecipeSearchLimits) -> Result:
        """Возвращает карточки без внутренней диагностики в публичном контракте.

        :param query: Один поисковый запрос
        :param limits: Явные фильтры, применяемые до top-k
        :return: До десяти коротких карточек
        """
        selected = await self.select(query, limits)
        recipes = await self.catalog.recipes_by_ids(selected.recipe_ids)
        return Result(
            data={
                "recipes": [recipes[rid].card(self.products) for rid in selected.recipe_ids if rid in recipes]
            }
        )


async def create_retriever(catalog: Catalog, config: Config) -> RecipeRetriever | None:
    """Создаёт общий модуль поиска при запуске web/CLI.

    :param catalog: Read-only каталог
    :param config: Централизованные настройки
    :return: Готовый модуль либо явно отключённый режим
    """
    if not config.retrieval_enabled:
        return None
    retriever = RecipeRetriever(catalog, config)
    try:
        await retriever.prepare()
    except BaseException:
        retriever.close()
        raise
    return retriever
