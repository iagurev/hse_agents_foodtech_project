import { useEffect, useState } from 'react';
import { Check, Clock3, MessageCircle, Pencil, ShoppingBag, Trash2 } from 'lucide-react';
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
} from './api';
import type { Cart, Plan, Recipe, Result, Totals, Discuss } from './api';
import {
  BackHeader,
  ErrorNote,
  ExternalLink,
  FoodImage,
  Loading,
  ObjectActions,
  useRestoreScroll,
} from './ui';
import {
  positiveNumber,
  packageNumber,
  mealTotals,
  sumTotals,
  itemPrice,
  knownSubtotal,
  ingredientAmount,
  cookingTime,
  servingDescription,
  dailyAverage,
} from './preview';

export function Nutrition({ value }: { value: Omit<Totals, 'mean_rub'> | null }) {
  return (
    <div className="nutrition">
      {[
        ['ккал', value?.kcal],
        ['белки, г', value?.protein_g],
        ['жиры, г', value?.fat_g],
        ['углев., г', value?.carbs_g],
      ].map(([label, v]) => (
        <div key={label}>
          <strong>{number(v as number)}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}
export function RecipeDetail({
  id,
  back,
  discuss,
  servings = 1,
}: {
  id: string;
  back: string;
  discuss: Discuss;
  servings?: number;
}) {
  const [recipe, setRecipe] = useState<Recipe>();
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    api<Recipe>(`/recipes/${id}`)
      .then((v) => alive && setRecipe(v))
      .catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [id]);
  useRestoreScroll(Boolean(recipe));
  return (
    <>
      <BackHeader title="Рецепт" back={back} />
      <ErrorNote text={error} />
      {!recipe ? (
        !error && <Loading />
      ) : (
        <>
          <FoodImage className="recipe-hero" src={recipe.image_url} alt={recipe.title} />
          <div className="page-content recipe-detail">
            <h1>{recipe.title}</h1>
            <div className="recipe-meta">
              <span>
                <Clock3 size={16} />
                {cookingTime(recipe.total_minutes)}
              </span>
              <span>{difficulties[recipe.difficulty]}</span>
              <span>≈ {money(recipe.cost.mean_rub * servings)}</span>
            </div>
            <p className="base-serving">Порций: {number(servings, 3)}</p>
            <Nutrition value={mealTotals(recipe, String(servings))} />
            <section>
              <h2>Ингредиенты</h2>
              <ul className="ingredients">
                {recipe.ingredients.map((i, index) => (
                  <li key={`${i.product_id}:${index}`}>
                    <span>{i.name}</span>
                    <strong>
                      {ingredientAmount(i.amount == null ? null : i.amount * servings)}{' '}
                      {{ g: 'г', ml: 'мл', pcs: 'шт.' }[i.unit] || i.unit}
                    </strong>
                  </li>
                ))}
              </ul>
            </section>
            <section>
              <h2>Как приготовить</h2>
              <ol className="steps">
                {recipe.steps.map((step, i) => (
                  <li key={i}>
                    <span>{i + 1}</span>
                    <p>{step}</p>
                  </li>
                ))}
              </ol>
            </section>
            <section>
              <h2>Полезно знать</h2>
              <p>Одна порция: {servingDescription(recipe.base_serving)}</p>
              <p>{recipe.restrictions || 'Особенности состава не указаны.'}</p>
              {recipe.equipment.length > 0 && (
                <p>
                  <strong>Понадобится:</strong> {recipe.equipment.join(', ')}.
                </p>
              )}
              <p>Активное время: {recipe.active_minutes} мин.</p>
              <ExternalLink href={recipe.source_url}>Первоисточник рецепта</ExternalLink>
            </section>
            <button
              className="primary full"
              onClick={() =>
                discuss('Хочу обсудить этот рецепт.', [{ kind: 'recipe', id, title: recipe.title, servings }])
              }
            >
              <MessageCircle size={18} />
              Обсудить рецепт
            </button>
          </div>
        </>
      )}
    </>
  );
}
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
export function CartDetail({ id, back, discuss }: { id: string; back: string; discuss: Discuss }) {
  const [result, setResult] = useState<Result<Cart>>();
  const [marks, setMarks] = useState<string[]>([]);
  const [items, setItems] = useState<{ sku_id: string; packages: string }[]>([]);
  const [unresolved, setUnresolved] = useState<number[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [operation, setOperation] = useState(crypto.randomUUID());
  const [conflict, setConflict] = useState(false);
  useEffect(() => {
    let alive = true;
    setResult(undefined);
    Promise.all([api<Result<Cart>>(`/carts/${id}`), api<{ purchased: string[] }>(`/carts/${id}/purchased`)])
      .then(([r, m]) => {
        if (!alive) return;
        setResult(r);
        setMarks(m.purchased);
        setItems(r.data.items.map((i) => ({ sku_id: i.sku_id, packages: String(i.packages) })));
        setUnresolved(r.data.unresolved.map((_, i) => i));
      })
      .catch((e) => alive && setError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [id]);
  async function save() {
    if (invalid) {
      setError('Укажите положительное количество: целые упаковки или кг/л с шагом 0,01.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const next = await api<Result<Cart>>(`/carts/${id}`, 'PUT', {
        operation_id: operation,
        items: items.map((i) => ({ ...i, packages: Number(i.packages.replace(',', '.')) })),
        unresolved_indices: unresolved,
      });
      setEditing(false);
      go(detailPath(`/cart/${next.data.cart_id}`, back));
    } catch (e) {
      setError(errorText(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  const cart = result?.data;
  useRestoreScroll(Boolean(cart));
  const invalid = items.some(
    (row) =>
      packageNumber(
        row.packages,
        cart?.items.find((i) => i.sku_id === row.sku_id),
      ) === null,
  );
  const prices = items.map((row) =>
    itemPrice(
      cart?.items.find((i) => i.sku_id === row.sku_id),
      row.packages,
    ),
  );
  const previewTotal = invalid ? null : knownSubtotal(prices);
  const previewIncomplete = unresolved.length > 0 || prices.some((p) => p === null) || !items.length;
  const dirty = Boolean(
    cart &&
    (items.length !== cart.items.length ||
      unresolved.length !== cart.unresolved.length ||
      items.some(
        (row) => positiveNumber(row.packages) !== cart.items.find((i) => i.sku_id === row.sku_id)?.packages,
      )),
  );

  return (
    <>
      <BackHeader title="Список покупок" back={back}>
        {cart && result && !editing && (
          <ObjectActions
            kind="carts"
            id={id}
            title={cart.title}
            hidden={cart.hidden}
            renamed={(title) => setResult({ ...result, data: { ...cart, title } })}
          />
        )}
      </BackHeader>
      <div className="page-content">
        <ErrorNote text={error} />
        {conflict && (
          <button className="text-button" onClick={() => go('/carts')}>
            Открыть актуальные покупки
          </button>
        )}
        {!cart ? (
          !error && <Loading />
        ) : (
          <>
            <div className="section-intro">
              <h1>{cart.title}</h1>
              <p>
                Позиций: {items.length}
                {!editing && ` · куплено: ${marks.length}`}
              </p>
            </div>

            {editing && (
              <p className="draft-note" role="status">
                {dirty ? 'Есть несохранённые изменения. ' : ''}Предварительная стоимость. Ссылка магазина
                обновится при сохранении.
              </p>
            )}
            <div className="cart-items">
              {items.map((row) => {
                const item = cart.items.find((i) => i.sku_id === row.sku_id)!;
                return (
                  <div
                    className={`cart-item ${marks.includes(row.sku_id) && !editing ? 'purchased' : ''}`}
                    key={row.sku_id}
                  >
                    {!editing && (
                      <button
                        className="check-box"
                        aria-label={`Куплено: ${item.product?.title || row.sku_id}`}
                        aria-pressed={marks.includes(row.sku_id)}
                        onClick={async () => {
                          try {
                            const m = await api<{ purchased: string[] }>(`/carts/${id}/purchased`, 'PUT', {
                              sku_id: row.sku_id,
                              purchased: !marks.includes(row.sku_id),
                            });
                            setMarks(m.purchased);
                          } catch (e) {
                            setError(errorText(e));
                          }
                        }}
                      >
                        {marks.includes(row.sku_id) && <Check size={16} />}
                      </button>
                    )}
                    <div className="cart-item-main">
                      <strong>{item.product?.title || `Товар ${item.sku_id}`}</strong>
                      {editing && packageNumber(row.packages, item) === null && (
                        <p className="field-error" id={`package-error-${row.sku_id}`}>
                          {item.product?.sale_unit
                            ? 'Введите количество больше нуля с шагом 0,01.'
                            : 'Введите целое число упаковок больше нуля.'}
                        </p>
                      )}
                      {editing ? (
                        <label className="quantity-field">
                          {item.product?.sale_unit === 'kg'
                            ? 'Килограммы'
                            : item.product?.sale_unit === 'l'
                              ? 'Литры'
                              : 'Упаковки'}
                          <input
                            aria-label={`${item.product?.sale_unit === 'kg' ? 'Килограммы' : item.product?.sale_unit === 'l' ? 'Литры' : 'Упаковки'}: ${item.product?.title || row.sku_id}`}
                            inputMode={item.product?.sale_unit ? 'decimal' : 'numeric'}
                            value={row.packages}
                            aria-invalid={packageNumber(row.packages, item) === null}
                            aria-describedby={
                              packageNumber(row.packages, item) === null
                                ? `package-error-${row.sku_id}`
                                : undefined
                            }
                            onChange={(e) => {
                              setItems(
                                items.map((i) =>
                                  i.sku_id === row.sku_id ? { ...i, packages: e.target.value } : i,
                                ),
                              );
                              setOperation(crypto.randomUUID());
                            }}
                          />
                        </label>
                      ) : (
                        <small>
                          {item.packages.toLocaleString('ru-RU')}{' '}
                          {item.product?.sale_unit === 'kg'
                            ? 'кг'
                            : item.product?.sale_unit === 'l'
                              ? 'л'
                              : 'уп.'}{' '}
                          · {money(itemPrice(item, row.packages))}
                        </small>
                      )}
                      {editing && (
                        <small>
                          {packageNumber(row.packages, item) === null
                            ? 'Стоимость: —'
                            : money(itemPrice(item, row.packages))}
                        </small>
                      )}
                    </div>
                    {editing && (
                      <button
                        className="icon-button danger"
                        aria-label={`Удалить ${item.product?.title || row.sku_id}`}
                        onClick={() => {
                          setItems(items.filter((i) => i.sku_id !== row.sku_id));
                          setOperation(crypto.randomUUID());
                        }}
                      >
                        <Trash2 size={18} />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
            {unresolved.length > 0 && (
              <section>
                <h2>Ещё нужно подобрать</h2>
                {unresolved.map((i) => (
                  <div className="unresolved" key={i}>
                    <div>
                      <strong>{cart.unresolved[i].product_name || 'Не подобран продукт'}</strong>
                      <p>{cart.unresolved[i].reason}</p>
                    </div>
                    {editing && (
                      <button
                        className="icon-button danger"
                        aria-label="Удалить нерешённую позицию"
                        onClick={() => {
                          setUnresolved(unresolved.filter((n) => n !== i));
                          setOperation(crypto.randomUUID());
                        }}
                      >
                        <Trash2 size={18} />
                      </button>
                    )}
                  </div>
                ))}
              </section>
            )}
            <div className="cart-total">
              <span>
                {(editing ? !previewIncomplete && previewTotal !== null : cart.total_is_complete)
                  ? 'Итого'
                  : 'Стоимость найденной части'}
              </span>
              <strong>{editing && invalid ? '—' : money(editing ? previewTotal : cart.subtotal_rub)}</strong>
            </div>
            {(editing ? previewIncomplete || previewTotal === null : !cart.total_is_complete) && (
              <p className="muted">Расчёт неполный: для части покупки ещё нет данных.</p>
            )}
            {!editing &&
              result.issues.map((issue, i) => (
                <p className="notice" key={i}>
                  {issue.message}
                </p>
              ))}
            {!editing && cart.link_error && (
              <p className="notice">Не все ссылки магазина готовы. Список сохранён. {cart.link_error}</p>
            )}
            {editing ? (
              <div className="action-stack">
                <button className="primary full" disabled={busy || invalid} onClick={() => void save()}>
                  {busy ? 'Сохраняем покупки…' : 'Сохранить изменения'}
                </button>
                <button
                  className="secondary full"
                  disabled={busy}
                  onClick={() => {
                    setEditing(false);
                    setItems(cart.items.map((i) => ({ sku_id: i.sku_id, packages: String(i.packages) })));
                    setUnresolved(cart.unresolved.map((_, i) => i));
                    setError('');
                    setConflict(false);
                  }}
                >
                  Отмена
                </button>
              </div>
            ) : (
              <div className="action-stack">
                {(cart.cart_links?.length
                  ? cart.cart_links
                  : cart.cart_url
                    ? [{ url: cart.cart_url, sku_ids: [], error: null }]
                    : []
                ).map(
                  (link, index, links) =>
                    link.url && (
                      <ExternalLink key={index} href={link.url}>
                        {links.length === 1
                          ? 'Открыть корзину ВкусВилла'
                          : `ВкусВилл · часть ${index + 1} из ${links.length}`}
                      </ExternalLink>
                    ),
                )}
                <button
                  className="secondary full"
                  onClick={() => {
                    setEditing(true);
                    setOperation(crypto.randomUUID());
                  }}
                >
                  <Pencil size={17} />
                  Изменить список
                </button>
                <button
                  className="text-button"
                  onClick={() =>
                    discuss('Хочу изменить этот список покупок.', [{ kind: 'cart', id, title: cart.title }])
                  }
                >
                  <MessageCircle size={17} />
                  Обсудить с ассистентом
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
