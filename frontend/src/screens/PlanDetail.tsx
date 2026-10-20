import { useEffect, useState } from 'react';
import { MessageCircle, Pencil, ShoppingBag, Trash2 } from 'lucide-react';
import {
  api,
  ApiError,
  difficulties,
  errorText,
  go,
  detailPath,
  openDetail,
  mealName,
  money,
  number,
} from '../api';
import type { Plan, Recipe, Result, Discuss } from '../api';
import { BackHeader, ErrorNote, FoodImage, Loading, ObjectActions, useRestoreScroll } from '../components/ui';
import { positiveNumber, mealTotals, sumTotals, cookingTime, dailyAverage } from '../preview';
import { Nutrition } from '../components/ui';

export function PlanDetail({ id, back, discuss }: { id: string; back: string; discuss: Discuss }) {
  const [result, setResult] = useState<Result<Plan>>();
  const [recipes, setRecipes] = useState<Record<string, Recipe>>({});
  const [recipesLoading, setRecipesLoading] = useState(true);
  const [day, setDay] = useState(Number(sessionStorage.getItem(`plan-day:${id}`) || 1));
  const [editing, setEditing] = useState(false);
  const [rows, setRows] = useState<{ index: number; servings: string }[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [operation, setOperation] = useState(crypto.randomUUID());
  useEffect(() => {
    let alive = true;
    setResult(undefined);
    api<Result<Plan>>(`/plans/${id}`)
      .then(async (r) => {
        if (!alive) return;
        setResult(r);
        setRows(r.data.meals.map((m, index) => ({ index, servings: String(m.servings) })));
        const ids = [...new Set(r.data.meals.map((m) => m.recipe_id))];
        const found = await Promise.all(
          ids.map(async (rid) => {
            try {
              return await api<Recipe>(`/recipes/${rid}`);
            } catch {
              return null;
            }
          }),
        );
        if (alive) {
          setRecipes(
            Object.fromEntries(found.filter((r): r is Recipe => r !== null).map((r) => [r.recipe_id, r])),
          );
          setRecipesLoading(false);
        }
      })
      .catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [id]);
  async function save() {
    if (rows.some((r) => positiveNumber(r.servings) === null)) {
      setError('Количество порций должно быть положительным числом.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const updated = await api<Result<Plan>>(`/plans/${id}`, 'PUT', {
        operation_id: operation,
        meals: rows.map((r) => ({ index: r.index, servings: Number(r.servings.replace(',', '.')) })),
      });
      setEditing(false);
      go(detailPath(`/plan/${updated.data.plan_id}`, back));
    } catch (e) {
      setError(errorText(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  const plan = result?.data;
  useRestoreScroll(Boolean(plan) && !recipesLoading);
  const preview = rows.map((row) => mealTotals(recipes[plan!.meals[row.index].recipe_id], row.servings));
  const total = editing ? sumTotals(preview) : (plan?.totals ?? null);
  const today = editing
    ? sumTotals(preview.filter((_, index) => plan!.meals[rows[index].index].day === day))
    : (plan?.daily[String(day)] ?? null);
  const datedTitle = plan?.title.match(/^(Меню на \d+ (?:день|дня|дней)) · (\d{2}\.\d{2}\.\d{4})$/);
  const invalid = rows.some((r) => positiveNumber(r.servings) === null);
  const dirty = Boolean(
    plan &&
    (rows.length !== plan.meals.length ||
      rows.some((r) => positiveNumber(r.servings) !== plan.meals[r.index].servings)),
  );

  return (
    <>
      <BackHeader title="План питания" back={back}>
        {plan && result && !editing && (
          <ObjectActions
            kind="plans"
            id={id}
            title={plan.title}
            hidden={plan.hidden}
            renamed={(title) => setResult({ ...result, data: { ...plan, title } })}
          />
        )}
      </BackHeader>
      <div className="page-content">
        <ErrorNote text={error} />
        {conflict && (
          <button className="text-button" onClick={() => go('/plans')}>
            Открыть актуальные планы
          </button>
        )}
        {!plan ? (
          !error && <Loading />
        ) : (
          <>
            <div className="section-intro">
              <h1>{datedTitle?.[1] || plan.title}</h1>
              {datedTitle && <span className="object-date">{datedTitle[2]}</span>}
              <p>
                {plan.meals.length
                  ? `≈ ${total ? money(total.mean_rub) : '—'} на весь план`
                  : 'Не удалось подобрать меню по заданным условиям.'}
              </p>
            </div>
            {editing && (
              <p className="draft-note" role="status">
                {dirty ? 'Есть несохранённые изменения. ' : ''}Предварительный расчёт; проверки выполнятся при
                сохранении.
              </p>
            )}
            {!editing && result.issues.length > 0 && (
              <div className="notice" role="status">
                {result.issues.map((i, n) => (
                  <p key={n}>{i.message}</p>
                ))}
              </div>
            )}
            <div className="days" aria-label="Дни плана">
              {Array.from({ length: Math.max(plan.days, 1) }, (_, i) => (
                <button
                  key={i}
                  aria-pressed={day === i + 1}
                  className={day === i + 1 ? 'selected' : ''}
                  onClick={() => {
                    setDay(i + 1);
                    sessionStorage.setItem(`plan-day:${id}`, String(i + 1));
                  }}
                >
                  День {i + 1}
                </button>
              ))}
            </div>
            <Nutrition value={today} />
            <p className="muted">За день: ≈ {today ? money(today.mean_rub) : '—'}</p>
            <div className="meals">
              {[
                ...new Set(
                  rows
                    .filter((r) => plan.meals[r.index].day === day)
                    .map((r) => mealName(plan.meals[r.index].meal)),
                ),
              ].map((name) => (
                <section className="meal-group" key={name}>
                  <h2>{name}</h2>
                  {rows
                    .filter(
                      (r) => plan.meals[r.index].day === day && mealName(plan.meals[r.index].meal) === name,
                    )
                    .map((row) => {
                      const meal = plan.meals[row.index];
                      const recipe = recipes[meal.recipe_id];
                      const metrics = mealTotals(recipe, row.servings);
                      return (
                        <article className="meal" key={row.index}>
                          <div className="inline">
                            {editing && (
                              <button
                                className="icon-button danger"
                                aria-label={`Удалить ${recipe?.title || meal.recipe_id}`}
                                onClick={() => {
                                  setRows(rows.filter((r) => r.index !== row.index));
                                  setOperation(crypto.randomUUID());
                                }}
                              >
                                <Trash2 size={18} />
                              </button>
                            )}
                          </div>
                          <button
                            className="meal-recipe"
                            onClick={() =>
                              openDetail(
                                `/recipe/${meal.recipe_id}?servings=${encodeURIComponent(row.servings)}`,
                              )
                            }
                          >
                            <FoodImage src={recipe?.image_url} alt="" />
                            <span>
                              <strong>
                                {recipe?.title ||
                                  (recipesLoading ? 'Загружаем рецепт…' : 'Рецепт недоступен')}
                              </strong>
                              <small>
                                {recipe
                                  ? `${cookingTime(recipe.total_minutes)} · ${difficulties[recipe.difficulty]}`
                                  : recipesLoading
                                    ? ''
                                    : meal.recipe_id}
                              </small>
                              {!editing && <small>Порций: {number(meal.servings, 3)}</small>}
                            </span>
                          </button>
                          <div className="meal-metrics">
                            <span>
                              {metrics
                                ? `${number(metrics.kcal)} ккал · Б ${number(metrics.protein_g)} · Ж ${number(metrics.fat_g)} · У ${number(metrics.carbs_g)} г`
                                : 'КБЖУ недоступны'}
                            </span>
                            <strong>{metrics ? `≈ ${money(metrics.mean_rub)}` : '—'}</strong>
                          </div>
                          {editing && positiveNumber(row.servings) === null && (
                            <p className="field-error" id={`portion-error-${row.index}`}>
                              Введите число порций больше нуля.
                            </p>
                          )}
                          {editing && (
                            <label className="quantity-field">
                              Порции
                              <input
                                aria-label={`Порции: ${recipe?.title || meal.recipe_id}`}
                                inputMode="decimal"
                                value={row.servings}
                                aria-invalid={positiveNumber(row.servings) === null}
                                aria-describedby={
                                  positiveNumber(row.servings) === null
                                    ? `portion-error-${row.index}`
                                    : undefined
                                }
                                onChange={(e) => {
                                  setRows(
                                    rows.map((r) =>
                                      r.index === row.index ? { ...r, servings: e.target.value } : r,
                                    ),
                                  );
                                  setOperation(crypto.randomUUID());
                                }}
                              />
                            </label>
                          )}
                        </article>
                      );
                    })}
                </section>
              ))}
            </div>
            <details className="plan-explanation">
              <summary>В среднем за день</summary>
              <Nutrition value={dailyAverage(total, plan.days)} />
            </details>
            {plan.explanation && (
              <details className="plan-explanation">
                <summary>Почему такой план</summary>
                <p>{plan.explanation}</p>
              </details>
            )}
            {editing ? (
              <div className="action-stack">
                <button className="primary full" disabled={busy || invalid} onClick={() => void save()}>
                  {busy ? 'Сохраняем…' : 'Сохранить изменения'}
                </button>
                <button
                  className="secondary full"
                  disabled={busy}
                  onClick={() => {
                    setEditing(false);
                    setRows(plan.meals.map((m, index) => ({ index, servings: String(m.servings) })));
                    setError('');
                    setConflict(false);
                  }}
                >
                  Отмена
                </button>
              </div>
            ) : (
              <div className="action-stack">
                <button
                  className="primary full"
                  onClick={() =>
                    discuss('Собери покупки по этому меню.', [{ kind: 'plan', id, title: plan.title }])
                  }
                >
                  <ShoppingBag size={18} />
                  Собрать покупки
                </button>
                <button
                  className="secondary full"
                  onClick={() => {
                    setEditing(true);
                    setOperation(crypto.randomUUID());
                  }}
                >
                  <Pencil size={17} />
                  Порции и состав
                </button>
                <button
                  className="text-button"
                  onClick={() =>
                    discuss('Хочу изменить это меню.', [{ kind: 'plan', id, title: plan.title }])
                  }
                >
                  <MessageCircle size={17} />
                  Изменить через ассистента
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
