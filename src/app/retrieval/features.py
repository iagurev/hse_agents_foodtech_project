import re
from functools import lru_cache

import pymorphy3

from app.models import NormalizedProduct, PersonContext, Recipe
from app.retrieval.models import SearchConstraints, SearchDiagnostic

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


class Conditions:
    """Разрешает только поддерживаемые условия; свободный текст не сертифицирует состав."""

    def __init__(
        self, language: Language, products: dict[str, NormalizedProduct], aliases: dict[str, str]
    ) -> None:
        self.language, self.products = language, products
        self.product_words = {pid: set(language.words(p.name)) for pid, p in products.items()}
        self.aliases: dict[str, set[str]] = {}
        for alias, pid in [*aliases.items(), *[(p.name, pid) for pid, p in products.items()]]:
            key = " ".join(language.words(alias))
            self.aliases.setdefault(key, set()).add(pid)

    def product_ids(self, name: str) -> set[str]:
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

    def parse(self, query: str) -> tuple[str, SearchConstraints, list[SearchDiagnostic]]:
        """Отделяет отрицания и простые ограничения от положительного блюда.

        :param query: Текущий самодостаточный запрос
        :return: Положительный текст, условия и непонятые фрагменты
        """
        result = SearchConstraints()
        diagnostics = []
        pattern = r"\b(?:без|исключая|не ем|не употребляю|исключить)\s+([^.!;\n]+)"

        def negative(match: re.Match) -> str:
            """Извлекает отдельное отрицание из совпадения.

            :param match: Фрагмент после без/исключая
            :return: Пустая строка вместо отрицательного терма
            """
            phrase = re.split(r"\s+(?:за|до|на|для|но|кроме)\s+", match.group(1), maxsplit=1)[0]
            positive_parts = []
            for part in re.split(r",|\s+и\s+|\s+или\s+", phrase):
                part = re.sub(r"^(?:без|исключая|не ем|не употребляю)\s+", "", part.strip(), flags=re.I)
                if re.match(r"(?:хочу|покажи|приготов|нужен|завтрак|обед|ужин|перекус|десерт)\b", part, re.I):
                    positive_parts.append(part)
                    continue
                if set(self.language.words(part)) & {"глютен", "лактоза", "аллерген"}:
                    result.allergens.append(part)
                    continue
                devices = [d for d, p in DEVICES.items() if re.search(p, part.lower())]
                if devices:
                    result.forbidden_equipment.extend(devices)
                elif self.product_ids(part):
                    result.exclude_products.append(part)
                else:
                    diagnostics.append(
                        SearchDiagnostic(
                            code="UNRESOLVED_CONSTRAINT",
                            message=f"Не удалось однозначно проверить условие «без {part[:100]}».",
                        )
                    )
            return " " + " ".join(positive_parts) + " "

        positive = re.sub(pattern, negative, query, flags=re.I)
        for device, pattern_device in DEVICES.items():
            if re.search(r"(?:нет|не использую)\s+(?:" + pattern_device + ")", query, flags=re.I):
                result.forbidden_equipment.append(device)
        time = re.search(r"(?:до|за|не более)\s*(\d+)\s*(?:мин|минут)", query, re.I)
        if time:
            result.max_total_minutes = float(time.group(1)) or None
        for word, role in {
            "завтрак": "breakfast",
            "обед": "lunch",
            "ужин": "dinner",
            "перекус": "snack",
            "десерт": "dessert",
        }.items():
            if word in self.language.words(query):
                result.meal_roles.append(role)
        if re.search(r"\bвеган", query, re.I):
            result.diet = "vegan"
        elif re.search(r"\bвегетариан", query, re.I):
            result.diet = "vegetarian"
        allergy_facts = re.sub(
            r"\bнет\s+(?:аллерги\w*|непереносим\w*)|\b(?:аллерги\w*|непереносим\w*)\s+нет\b",
            "",
            query,
            flags=re.I,
        )
        if re.search(r"аллерги|непереносим", allergy_facts, re.I):
            result.allergens.append("указанная аллергия/непереносимость")
        return positive.strip(), result, diagnostics

    def resolve(
        self, query: str, person: PersonContext, explicit: SearchConstraints | None
    ) -> tuple[str, SearchConstraints, list[SearchDiagnostic]]:
        """Учитывает актуальную память/анкету и текущий запрос без старого dialogue.

        :param query: Текущий запрос
        :param person: Актуальный контекст пользователя
        :param explicit: Явные поддерживаемые условия
        :return: Положительная часть, объединённые условия и диагностика
        """
        profile = str(person.questionnaire.get("restrictions", "")) + "\n" + person.memory
        _, baseline, diagnostics = self.parse(profile)
        positive, current, notes = self.parse(query)
        diagnostics.extend(notes)
        data = baseline.model_dump()
        # При явной структуре она полностью описывает действующие условия, включая снятые запреты.
        if explicit is not None:
            data.update(explicit.model_dump(exclude_unset=True))
        for key in ["exclude_products", "forbidden_equipment", "allergens", "meal_roles"]:
            data[key] = list(dict.fromkeys(data[key] + getattr(current, key)))
        for key in ["max_total_minutes", "diet", "max_difficulty"]:
            if getattr(current, key) is not None:
                data[key] = getattr(current, key)
        if data["max_difficulty"] is None and not (
            explicit is not None and "max_difficulty" in explicit.model_fields_set
        ):
            data["max_difficulty"] = person.questionnaire.get("cooking_difficulty")
        return positive, SearchConstraints.model_validate(data), diagnostics

    def preferences(self, person: PersonContext) -> set[str]:
        """Извлекает известные любимые продукты для мягкого ранжирования.

        :param person: Актуальная память/анкета без исторического dialogue
        :return: ID без назначения новых запретов
        """
        result = set()
        text = person.memory + "\n" + str(person.questionnaire.get("preferences", ""))
        for match in re.finditer(r"(?<!не )\b(?:люблю|предпочитаю|нравится)\s+([^.!;\n]+)", text, re.I):
            for name in re.split(r",|\s+и\s+", match.group(1)):
                result.update(self.product_ids(name.strip()))
        return result
