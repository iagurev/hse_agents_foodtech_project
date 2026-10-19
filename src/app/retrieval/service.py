import asyncio
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from loguru import logger

from app.catalog import Catalog
from app.config import Config
from app.models import PersonContext, PlanLimits, Recipe, Result
from app.retrieval.encoder import Encoder, SentenceEncoder
from app.retrieval.features import DEVICES, STOP, Conditions, Language
from app.retrieval.index import prepare_index
from app.retrieval.models import SearchConstraints, SearchDiagnostic, Selection

RANKS = {"easy": 0, "intermediate": 1, "advanced": 2}


class RecipeRetriever:
    """Гибридный поиск и широкий отбор для меню с изолированным CPU worker."""

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
        self.conditions = await asyncio.to_thread(Conditions, self.language, self.products, aliases)
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
        self.features = {d["recipe_id"]: d["features"] for d in self.documents}
        self.titles = {d["recipe_id"]: set(d["title_terms"]) for d in self.documents}
        self.ids = [r.recipe_id for r in recipes]
        self.source_stats = {p: (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) for p in paths}
        logger.bind(index_id=self.index_id, recipes=len(recipes), dense=self.matrix is not None).info(
            "Recipe retrieval ready"
        )

    def close(self) -> None:
        """Закрывает executor без преждевременного освобождения занятого слота."""
        self.worker.shutdown(wait=False, cancel_futures=True)

    def _eligible(self, constraints: SearchConstraints, diagnostics: list[SearchDiagnostic]) -> set[str]:
        """Проверяет только подтверждённые условия до top-k.

        :param constraints: Поддерживаемые ограничения
        :param diagnostics: Список для сообщений о неизвестности
        :return: Допустимые ID
        """
        if constraints.allergens:
            diagnostics.append(
                SearchDiagnostic(
                    code="INSUFFICIENT_EVIDENCE",
                    message="Каталог не подтверждает отсутствие аллергенов и следовых количеств; безопасные варианты не установлены.",
                )
            )
            return set()
        excluded = set()
        for name in constraints.exclude_products:
            ids = self.conditions.product_ids(name)
            if not ids:
                diagnostics.append(
                    SearchDiagnostic(
                        code="UNRESOLVED_CONSTRAINT",
                        message=f"Исключение продукта «{name[:100]}» не удалось проверить.",
                    )
                )
                return set()
            excluded.update(ids)
        if constraints.diet:
            for name in ["мясо", "рыба", "креветки", "желатин"] + (
                ["молочное", "яйцо", "мед"] if constraints.diet == "vegan" else []
            ):
                excluded.update(self.conditions.product_ids(name))
            diagnostics.append(
                SearchDiagnostic(
                    code="INSUFFICIENT_EVIDENCE",
                    message="Исключены известные животные ингредиенты; состав сложных товаров и следовые количества не сертифицированы.",
                )
            )
        forbidden = set()
        for name in constraints.forbidden_equipment:
            matched = {d for d, pattern in DEVICES.items() if re.search(pattern, name.lower())}
            if not matched:
                diagnostics.append(
                    SearchDiagnostic(
                        code="UNRESOLVED_CONSTRAINT", message=f"Неизвестное оборудование «{name[:100]}»."
                    )
                )
                return set()
            forbidden.update(matched)
        result, uncertain = set(), 0
        for rid, recipe in self.recipes.items():
            if excluded & {i.product_id for i in recipe.ingredients}:
                continue
            if constraints.max_difficulty and RANKS[recipe.difficulty] > RANKS[constraints.max_difficulty]:
                continue
            if (
                constraints.max_total_minutes is not None
                and recipe.total_minutes > constraints.max_total_minutes
            ):
                continue
            if constraints.meal_roles and not set(constraints.meal_roles) & set(recipe.meal_roles):
                continue
            features = self.features[rid]
            if forbidden and features["unknown_heat"]:
                uncertain += 1
                continue
            if any(
                all(
                    device in forbidden
                    or (device == "микроволновая печь с грилем" and "микроволновая печь" in forbidden)
                    for device in group
                )
                for group in features["groups"]
            ):
                continue
            result.add(rid)
        if uncertain:
            diagnostics.append(
                SearchDiagnostic(
                    code="INSUFFICIENT_EVIDENCE",
                    message="Часть рецептов исключена: в шагах есть нагрев, но устройство не установлено.",
                )
            )
        return result

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
        self, query: str, allowed: set[str], diagnostics: list[SearchDiagnostic], deadline: float
    ) -> list[str]:
        """Объединяет лексический и векторный ранги, ограничивая CPU inference.

        :param query: Положительный запрос без исключённых продуктов
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
                diagnostics.append(
                    SearchDiagnostic(
                        code="RETRIEVAL_DEGRADED",
                        message="Семантический поиск временно недоступен; показаны лексические совпадения.",
                    )
                )
        elif self.encoder is None or self.matrix is None:
            diagnostics.append(
                SearchDiagnostic(
                    code="RETRIEVAL_DEGRADED",
                    message="Использован лексический поиск: локальная семантическая модель недоступна или отключена.",
                )
            )
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

    async def select(
        self,
        query: str,
        person: PersonContext | None = None,
        constraints: SearchConstraints | None = None,
        *,
        for_plan: bool = False,
        days: int = 1,
        limits: PlanLimits | None = None,
    ) -> Selection:
        """Возвращает точечную выдачу либо широкий разнообразный пул меню.

        :param query: Самодостаточное поручение
        :param person: Текущие персональные данные
        :param constraints: Явные условия
        :param for_plan: Выбирать широкий пул
        :param days: Число дней для оценки покрытия
        :param limits: Предел сложности генератора
        :return: ID и честная диагностика
        """
        started = time.monotonic()
        if any(
            (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) != stat
            for p, stat in self.source_stats.items()
        ):
            return Selection(
                index_id=self.index_id,
                diagnostics=[
                    SearchDiagnostic(
                        code="RETRIEVAL_DEGRADED",
                        message="Каталог изменился после запуска. Для согласованного поиска требуется перезапуск приложения.",
                    )
                ],
            )
        positive, conditions, diagnostics = self.conditions.resolve(
            query, person or PersonContext(), constraints
        )
        if limits is not None:
            # При создании меню limits — явный контракт; анкета не восстанавливает снятый предел.
            explicit_difficulty = constraints.max_difficulty if constraints is not None else None
            candidates = list(filter(None, [explicit_difficulty, limits.max_difficulty]))
            conditions.max_difficulty = min(candidates, key=RANKS.get) if candidates else None
        allowed = self._eligible(conditions, diagnostics)
        anchors = set(
            self.language.words(
                "омлет яичница сырники блины оладьи суп салат паста ризотто каша котлеты запеканка вафли пицца смузи рагу борщ"
            )
        )
        requested_dishes = set(self.language.words(positive)) & anchors
        if requested_dishes and not any(word in positive.lower() for word in ["похож", "аналог", "вместо"]):
            allowed = {
                rid
                for rid in allowed
                if set(
                    self.language.words(
                        re.split(r"\s+(?:к|для)\s+", self.recipes[rid].title, maxsplit=1, flags=re.I)[0]
                    )
                )
                & requested_dishes
            }
        if any(d.code == "UNRESOLVED_CONSTRAINT" for d in diagnostics):
            allowed = set()
        preferences = self.conditions.preferences(person or PersonContext())
        ranking_query = positive + (
            " " + " ".join(self.products[pid].name for pid in sorted(preferences)) if preferences else ""
        )
        ranked = (
            await self._rank(
                ranking_query, allowed, diagnostics, started + self.config.retrieval_timeout_seconds
            )
            if allowed
            else []
        )
        if preferences:
            positions = {rid: i for i, rid in enumerate(ranked)}
            ranked.sort(
                key=lambda rid: (
                    positions[rid]
                    - 8 * len(preferences & {i.product_id for i in self.recipes[rid].ingredients}),
                    rid,
                )
            )
        if (
            not ranked
            and allowed
            and (conditions.meal_roles or not (set(self.language.words(positive)) - STOP))
        ):
            ranked = sorted(allowed, key=lambda rid: (self.recipes[rid].total_minutes, rid))
        if for_plan and allowed:
            target = min(len(allowed), max(self.config.planning_candidate_count, days * 10))
            roles = conditions.meal_roles or sorted(
                {role for rid in allowed for role in self.recipes[rid].meal_roles}
            )
            pool: list[str] = []
            # Покрытие: релевантные, дешёвые и быстрые варианты каждой роли.
            for role in roles:
                role_ids = [rid for rid in allowed if role in self.recipes[rid].meal_roles]
                relevant = [rid for rid in ranked if rid in set(role_ids)]
                cheap = sorted(role_ids, key=lambda rid: (self.recipes[rid].cost.mean_rub, rid))
                fast = sorted(role_ids, key=lambda rid: (self.recipes[rid].total_minutes, rid))
                for branch in [relevant[:12], cheap[:6], fast[:6]]:
                    pool.extend(branch)
            pool = list(dict.fromkeys(pool))
            # Круговой обход составов добавляет разнообразие без назначения скрытых приёмов пищи.
            remaining = ranked + sorted(allowed, key=lambda rid: (self.recipes[rid].title, rid))
            groups: dict[str, list[str]] = {}
            for rid in dict.fromkeys(remaining):
                product_ids = self.recipes[rid].ingredients
                key = max(product_ids, key=lambda i: i.amount).product_id if product_ids else rid
                groups.setdefault(key, []).append(rid)
            while len(pool) < target and groups:
                for key in list(groups):
                    rid = groups[key].pop(0)
                    if rid not in pool:
                        pool.append(rid)
                    if not groups[key]:
                        del groups[key]
                    if len(pool) >= target:
                        break
            if any(
                sum(role in self.recipes[r].meal_roles for r in pool) < min(days * 2, 10) for role in roles
            ):
                pool = sorted(allowed)
                diagnostics.append(
                    SearchDiagnostic(
                        code="CANDIDATE_POOL_EXPANDED",
                        message="Для покрытия приёмов пищи передан весь допустимый каталог.",
                    )
                )
            ranked = pool
        elif not for_plan:
            ranked = ranked[:10]
        if not ranked:
            diagnostics.append(
                SearchDiagnostic(
                    code="NO_RELEVANT_MATCH",
                    message="В базе не найдено подтверждённых вариантов по текущим условиям.",
                )
            )
        logger.bind(
            index_id=self.index_id,
            purpose="plan" if for_plan else "search",
            candidates=len(ranked),
            allowed=len(allowed),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        ).info("Recipe retrieval completed")
        return Selection(
            recipe_ids=ranked,
            diagnostics=list({(d.code, d.message): d for d in diagnostics}.values()),
            index_id=self.index_id,
        )

    async def search(
        self, query: str, person: PersonContext | None = None, constraints: SearchConstraints | None = None
    ) -> Result:
        """Загружает карточки выбранных существующих рецептов одним запросом.

        :param query: Поисковый текст
        :param person: Актуальный контекст
        :param constraints: Поддерживаемые условия
        :return: Публичные карточки и диагностика
        """
        selected = await self.select(query, person, constraints)
        recipes = await self.catalog.recipes_by_ids(selected.recipe_ids)
        return Result(
            data={
                "recipes": [
                    recipes[rid].card(self.products) for rid in selected.recipe_ids if rid in recipes
                ],
                "diagnostics": [d.model_dump() for d in selected.diagnostics],
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
