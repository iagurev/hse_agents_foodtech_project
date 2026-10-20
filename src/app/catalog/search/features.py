import re
from functools import lru_cache

import pymorphy3

from app.models import NormalizedProduct, Recipe, RecipeSearchLimits

STOP = frozenset(
    "и или в на с со из к по для это рецепт блюдо приготовить сделать покажи найти хочу мне пожалуйста что как можно без не до за минут минута еда меню день неделя".split()
)
DEVICES = {
    "духовка": r"духов\w*|духовочная?\s+печ\w*",
    "аэрогриль": r"аэрогрил\w*",
    "гриль": r"(?<!аэро)\bгрил\w*",
    "тостер": r"тостер\w*",
    "вафельница": r"вафельниц\w*",
    "блендер": r"блендер\w*",
    "кухонный комбайн": r"комбайн\w*",
    "миксер": r"миксер\w*",
    "микроволновая печь": r"микроволн\w*|\bсвч\b",
    "мультиварка": r"мультиварк\w*",
    "мясорубка": r"мясоруб\w*",
    "сковорода": r"сковород\w*",
    "кастрюля": r"кастрюл\w*|сотейн\w*",
    "морозильник": r"морозил\w*",
    "холодильник": r"холодильник\w*",
}
FAMILIES = {
    "молочный": "молоко кефир творог йогурт сметана сыр сливки сливочный ряженка простокваша",
    "молочное": "молоко кефир творог йогурт сметана сыр сливки сливочный ряженка простокваша",
    "курица": "курица куриный цыпленок",
    "рыба": "рыба рыбный лосось форель горбуша треска скумбрия судак тунец сельдь минтай семга",
    "мясо": "мясо говядина свинина курица куриный индейка цыпленок бекон ветчина колбаса сосиска баранина",
    "сахар": "сахар сахарный",
}


class Language:
    """Нормализует русские словоформы и продуктовые имена без модельных запросов."""

    def __init__(self) -> None:
        self.morph = pymorphy3.MorphAnalyzer()

    @lru_cache(maxsize=10000)
    def lemma(self, word: str) -> str:
        """Возвращает нормальную форму слова.

        :param word: Одно слово
        :return: Лемма с нормализованной ё
        """
        return self.morph.parse(word)[0].normal_form.replace("ё", "е")

    def words(self, text: str) -> list[str]:
        """Разбирает короткий текст на леммы.

        :param text: Текст запроса либо названия
        :return: Слова с сохранением порядка
        """
        return [self.lemma(w) for w in re.findall(r"[a-zа-яё]+", text.lower())]


def equipment_features(recipe: Recipe) -> dict:
    """Извлекает OR-группы техники из шагов, сохраняя неизвестность.

    :param recipe: Полный опубликованный рецепт
    :return: Обязательные группы, цитаты и флаг неизвестного нагрева
    """
    groups, evidence = [], []
    for number, step in enumerate(recipe.steps):
        text = step.lower().replace("ё", "е")
        if re.search(r"хран\w*|впрок|при желании|можно заморозить", text):
            continue
        devices = [name for name, pattern in DEVICES.items() if re.search(pattern, text)]
        if "гриль" in devices and re.search(r"куриц\w* гриль|кабач\w* гриль", text):
            devices.remove("гриль")
        if "гриль" in devices and "микроволновая печь" in devices:
            devices.remove("гриль")
            devices.append("микроволновая печь с грилем")
        if not devices:
            continue
        # Явные альтернативы объединяются, а не превращаются в AND.
        parts = [devices] if re.search(r"\bили\b|либо|можно также", text) else [[d] for d in devices]
        for group in parts:
            if group not in groups:
                groups.append(group)
                evidence.append({"step": number, "text": step, "any_of": group})
    unknown_heat = any(re.search(r"выпека|запека|\d+\s*°", s.lower()) for s in recipe.steps) and not any(
        set(g)
        & {
            "духовка",
            "аэрогриль",
            "микроволновая печь",
            "микроволновая печь с грилем",
            "мультиварка",
            "вафельница",
        }
        for g in groups
    )
    return {"groups": groups, "evidence": evidence, "unknown_heat": unknown_heat}


class RecipeFilter:
    """Фильтрует каталог по явным limits, не анализируя текст поискового запроса."""

    def __init__(
        self,
        recipes: dict[str, Recipe],
        products: dict[str, NormalizedProduct],
        aliases: dict[str, str],
        language: Language | None = None,
        features: dict[str, dict] | None = None,
    ) -> None:
        self.recipes, self.products = recipes, products
        self.language = language or Language()
        language = self.language
        self.features = (
            features
            if features is not None
            else {rid: equipment_features(recipe) for rid, recipe in recipes.items()}
        )
        self.product_words = {pid: set(language.words(p.name)) for pid, p in products.items()}
        self.aliases: dict[str, set[str]] = {}
        for alias, pid in [*aliases.items(), *[(p.name, pid) for pid, p in products.items()]]:
            key = " ".join(language.words(alias))
            self.aliases.setdefault(key, set()).add(pid)

    def _product_ids(self, name: str) -> set[str]:
        """Разрешает имя ингредиента или группы в существующие ID.

        :param name: Название или product_id
        :return: Известные подходящие продукты
        """
        if name in self.products:
            return {name}
        terms = set(self.language.words(name)) - {"продукт"}
        key = " ".join(self.language.words(name))
        if terms & {"молочный", "молочное"}:
            terms = {"молочный"}
        expanded = set()
        for term in terms:
            expanded.update(self.language.words(FAMILIES.get(term, term)))
        matches = set(self.aliases.get(key, set()))
        matches.update(
            pid
            for pid, words in self.product_words.items()
            if terms and (terms <= words or (len(terms) == 1 and expanded & words))
        )
        return matches

    def apply(self, limits: RecipeSearchLimits) -> tuple[set[str], list[str]]:
        """Отбирает допустимые рецепты до ранжирования и ограничения выдачи.

        :param limits: Явные условия без подстановки персонального контекста
        :return: Допустимые ID и коды диагностики без пользовательского текста
        """
        excluded, forbidden = set(), set()
        for name in limits.exclude_products or []:
            ids = self._product_ids(name)
            if not ids:
                return set(), ["UNRESOLVED_CONSTRAINT"]
            excluded.update(ids)
        if limits.diet:
            groups = ["мясо", "рыба", "креветки", "желатин"]
            if limits.diet == "vegan":
                groups += ["молочное", "яйцо", "мед"]
            for name in groups:
                excluded.update(self._product_ids(name))
        for name in limits.forbidden_equipment or []:
            matched = {d for d, pattern in DEVICES.items() if re.search(pattern, name.lower())}
            if not matched:
                return set(), ["UNRESOLVED_CONSTRAINT"]
            forbidden.update(matched)
        ranks = {"easy": 0, "intermediate": 1, "advanced": 2}
        allowed, uncertain = set(), False
        for rid, recipe in self.recipes.items():
            if excluded & {i.product_id for i in recipe.ingredients}:
                continue
            if limits.max_difficulty and ranks[recipe.difficulty] > ranks[limits.max_difficulty]:
                continue
            if limits.max_total_minutes is not None and recipe.total_minutes > limits.max_total_minutes:
                continue
            if limits.budget_rub is not None and recipe.cost.mean_rub > limits.budget_rub * 1.05:
                continue
            if limits.meal_roles and not set(limits.meal_roles) & set(recipe.meal_roles):
                continue
            features = self.features[rid]
            if forbidden and features["unknown_heat"]:
                uncertain = True
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
            allowed.add(rid)
        return allowed, ["INSUFFICIENT_EVIDENCE"] if uncertain else []
