import { useEffect, useState } from 'react';
import { Check, Clock3, MessageCircle, Pencil, ShoppingBag, Trash2 } from 'lucide-react';
import { api, ApiError, difficulties, errorText, go, mealName, money, number } from './api';
import type { Cart, Plan, Recipe, Result, Totals } from './api';
import { BackHeader, ErrorNote, ExternalLink, FoodImage, Loading } from './ui';

export function Nutrition({ value }: { value: Omit<Totals, 'mean_rub'> }) {
  return (
    <div className="nutrition">
      {[
        ['ккал', value.kcal],
        ['белки, г', value.protein_g],
        ['жиры, г', value.fat_g],
        ['углев., г', value.carbs_g],
      ].map(([label, v]) => (
        <div key={label}>
          <strong>{number(v as number)}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}
export function RecipeDetail({ id, discuss }: { id: string; discuss: (s: string) => void }) {
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
  return (
    <>
      <BackHeader title="Рецепт" back="/recipes" />
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
                {recipe.total_minutes} мин.
              </span>
              <span>{difficulties[recipe.difficulty]}</span>
              <span>≈ {money(recipe.cost.mean_rub)}</span>
            </div>
            <p className="base-serving">Базовая порция: {recipe.base_serving}</p>
            <Nutrition value={recipe.nutrition} />
            <section>
              <h2>Ингредиенты</h2>
              <ul className="ingredients">
                {recipe.ingredients.map((i, index) => (
                  <li key={`${i.product_id}:${index}`}>
                    <span>{i.name}</span>
                    <strong>
                      {number(i.amount)} {{ g: 'г', ml: 'мл', pcs: 'шт.' }[i.unit] || i.unit}
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
              onClick={() => discuss(`Хочу обсудить рецепт «${recipe.title}», recipe_id: ${id}.`)}
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
export function PlanDetail({ id, discuss }: { id: string; discuss: (s: string) => void }) {
  const [result, setResult] = useState<Result<Plan>>();
  const [recipes, setRecipes] = useState<Record<string, Recipe>>({});
  const [recipesLoading, setRecipesLoading] = useState(true);
  const [day, setDay] = useState(1);
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
    if (
      rows.some(
        (r) =>
          !Number.isFinite(Number(r.servings.replace(',', '.'))) || Number(r.servings.replace(',', '.')) <= 0,
      )
    ) {
      setError('Количество порций должно быть больше нуля.');
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
      go(`/plan/${updated.data.plan_id}`);
    } catch (e) {
      setError(errorText(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  const plan = result?.data;
  return (
    <>
      <BackHeader title="План питания" back="/plans" />
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
              <h1>Меню на {plan.days} дн.</h1>
              <p>
                {plan.meals.length
                  ? `≈ ${money(plan.totals.mean_rub)} на весь план`
                  : 'Не удалось подобрать меню по заданным условиям.'}
              </p>
            </div>
            {result.issues.length > 0 && (
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
                  onClick={() => setDay(i + 1)}
                >
                  День {i + 1}
                </button>
              ))}
            </div>
            {plan.daily[String(day)] && <Nutrition value={plan.daily[String(day)]} />}
            <div className="meals">
              {rows
                .filter((r) => plan.meals[r.index].day === day)
                .map((row) => {
                  const meal = plan.meals[row.index];
                  const recipe = recipes[meal.recipe_id];
                  return (
                    <article className="meal" key={row.index}>
                      <div className="inline">
                        <h2>{mealName(meal.meal)}</h2>
                        {editing && (
                          <button
                            className="icon-button danger"
                            aria-label={`Удалить ${mealName(meal.meal)}`}
                            onClick={() => {
                              setRows(rows.filter((r) => r.index !== row.index));
                              setOperation(crypto.randomUUID());
                            }}
                          >
                            <Trash2 size={18} />
                          </button>
                        )}
                      </div>
                      <button className="meal-recipe" onClick={() => go(`/recipe/${meal.recipe_id}`)}>
                        <FoodImage src={recipe?.image_url} alt="" />
                        <span>
                          <strong>
                            {recipe?.title || (recipesLoading ? 'Загружаем рецепт…' : 'Рецепт недоступен')}
                          </strong>
                          <small>
                            {recipe
                              ? `${recipe.total_minutes} мин. · ${difficulties[recipe.difficulty]}`
                              : recipesLoading
                                ? ''
                                : meal.recipe_id}
                          </small>
                          {!editing && <small>Порций: {number(meal.servings, 3)}</small>}
                        </span>
                      </button>
                      {editing && (
                        <label className="quantity-field">
                          Порции
                          <input
                            aria-label={`Порции: ${mealName(meal.meal)}`}
                            inputMode="decimal"
                            value={row.servings}
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
            </div>
            {plan.explanation && (
              <details className="plan-explanation">
                <summary>Почему такой план</summary>
                <p>{plan.explanation}</p>
              </details>
            )}
            {editing ? (
              <div className="action-stack">
                <button className="primary full" disabled={busy} onClick={() => void save()}>
                  {busy ? 'Сохраняем…' : 'Сохранить изменения'}
                </button>
                <button
                  className="secondary full"
                  disabled={busy}
                  onClick={() => {
                    setEditing(false);
                    setRows(plan.meals.map((m, index) => ({ index, servings: String(m.servings) })));
                    setError('');
                  }}
                >
                  Отмена
                </button>
              </div>
            ) : (
              <div className="action-stack">
                <button
                  className="primary full"
                  onClick={() => discuss(`Создай корзину по плану plan_id: ${id}.`)}
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
                  onClick={() => discuss(`Хочу изменить план plan_id: ${id}. `)}
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
export function CartDetail({ id, discuss }: { id: string; discuss: (s: string) => void }) {
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
    if (items.some((i) => !Number.isInteger(Number(i.packages)) || Number(i.packages) <= 0)) {
      setError('Укажите целое число упаковок больше нуля.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const next = await api<Result<Cart>>(`/carts/${id}`, 'PUT', {
        operation_id: operation,
        items: items.map((i) => ({ ...i, packages: Number(i.packages) })),
        unresolved_indices: unresolved,
      });
      setEditing(false);
      go(`/cart/${next.data.cart_id}`);
    } catch (e) {
      setError(errorText(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  const cart = result?.data;
  return (
    <>
      <BackHeader title="Список покупок" back="/carts" />
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
              <h1>Всё для вашего меню</h1>
              <p>
                {cart.items.length} позиций · {marks.length} куплено
              </p>
            </div>
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
                      {editing ? (
                        <label className="quantity-field">
                          Упаковки
                          <input
                            aria-label={`Упаковки: ${item.product?.title || row.sku_id}`}
                            inputMode="numeric"
                            value={row.packages}
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
                          {item.packages} уп. ·{' '}
                          {money(item.product ? item.product.price_rub * item.packages : null)}
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
              <span>{cart.total_is_complete ? 'Итого' : 'Стоимость найденной части'}</span>
              <strong>{money(cart.subtotal_rub)}</strong>
            </div>
            {!cart.total_is_complete && (
              <p className="muted">Расчёт неполный: для части покупки ещё нет данных.</p>
            )}
            {result.issues.map((issue, i) => (
              <p className="notice" key={i}>
                {issue.message}
              </p>
            ))}
            {cart.link_error && (
              <p className="notice">
                Не удалось обновить ссылку магазина. Список сохранён. {cart.link_error}
              </p>
            )}
            {editing ? (
              <div className="action-stack">
                <button className="primary full" disabled={busy} onClick={() => void save()}>
                  {busy ? 'Сохраняем и обновляем ссылку…' : 'Сохранить изменения'}
                </button>
                <button
                  className="secondary full"
                  disabled={busy}
                  onClick={() => {
                    setEditing(false);
                    setItems(cart.items.map((i) => ({ sku_id: i.sku_id, packages: String(i.packages) })));
                    setUnresolved(cart.unresolved.map((_, i) => i));
                    setError('');
                  }}
                >
                  Отмена
                </button>
              </div>
            ) : (
              <div className="action-stack">
                {cart.cart_url && <ExternalLink href={cart.cart_url}>Открыть корзину ВкусВилла</ExternalLink>}
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
                  onClick={() => discuss(`Хочу изменить корзину cart_id: ${id}. `)}
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
