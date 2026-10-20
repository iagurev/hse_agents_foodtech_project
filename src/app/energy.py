import math

from pydantic import JsonValue

# NASEM 2023, таблицы 5-15/5-16: константа, возраст, рост в см, вес в кг.
# Порядок активности: inactive, low_active, active, very_active.
_EQUATIONS = {
    "adult": {
        "male": [
            (753.07, -10.83, 6.50, 14.10),
            (581.47, -10.83, 8.30, 14.94),
            (1004.82, -10.83, 6.52, 15.91),
            (-517.88, -10.83, 15.61, 19.11),
        ],
        "female": [
            (584.90, -7.01, 5.72, 11.71),
            (575.77, -7.01, 6.60, 12.14),
            (710.25, -7.01, 6.54, 12.34),
            (511.83, -7.01, 9.07, 12.56),
        ],
    },
    "adolescent": {
        "male": [
            (-447.51, 3.68, 13.01, 13.15),
            (19.12, 3.68, 8.62, 20.28),
            (-388.19, 3.68, 12.66, 20.46),
            (-671.75, 3.68, 15.38, 23.25),
        ],
        "female": [
            (55.59, -22.25, 8.43, 17.07),
            (-297.54, -22.25, 12.77, 14.73),
            (-189.55, -22.25, 11.74, 18.34),
            (-709.59, -22.25, 18.22, 14.25),
        ],
    },
}
_ACTIVITIES = ("inactive", "low_active", "active", "very_active")


def energy_reference(profile: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """Рассчитывает ориентир поддержания; не назначает дефицит или целевые БЖУ.

    Диапазон ±10% и округление до 100 ккал — правило представления приложения,
    не доверительный интервал формулы. При неизвестной активности охватывает все
    четыре сценария. Для 14–18 лет учитывает рост, согласно источнику.
    :param profile: Анкета, в том числе неполная анкета CLI
    :return: Оценка и её основания либо None при недостатке основных данных
    """
    values = [profile.get(key) for key in ("age", "height_cm", "weight_kg")]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return None
    age, height, weight = values
    sex = profile.get("sex")
    if sex not in ("male", "female") or not (
        14 <= age <= 120 and 150 <= height <= 230 and 50 <= weight <= 200
    ):
        return None
    group = "adult" if age >= 19 else "adolescent"
    activity = profile.get("activity")
    rows = _EQUATIONS[group][sex]
    if activity in _ACTIVITIES:
        rows = [rows[_ACTIVITIES.index(activity)]]
    else:
        activity = None
    estimates = [base + a * age + h * height + w * weight + (20 if age < 19 else 0) for base, a, h, w in rows]
    return {
        "maintenance_kcal_per_day": {
            "min": math.floor(min(estimates) * 0.9 / 100) * 100,
            "max": math.ceil(max(estimates) * 1.1 / 100) * 100,
        },
        "activity": activity,
        "age_group": group,
        "basis": "NASEM 2023 EER; ±10% и округление — ориентир приложения, не медицинская норма",
        "source": "https://www.nationalacademies.org/read/26818/chapter/7",
    }
