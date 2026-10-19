import { useEffect, useState } from 'react';
import {
  ArrowRight,
  BookOpen,
  CalendarDays,
  ChefHat,
  ChevronRight,
  Clock3,
  Search,
  ShoppingBag,
} from 'lucide-react';
import { api, difficulties, errorText, go, money } from './api';
import type { Cart, Plan, Recipe, Result } from './api';
import { Empty, ErrorNote, FoodImage, Loading } from './ui';

export function Collections({
  kind,
  discuss,
}: {
  kind: 'plans' | 'recipes' | 'carts';
  discuss: (s: string) => void;
}) {
  const [plans, setPlans] = useState<Result<Plan>[]>([]);
  const [carts, setCarts] = useState<Result<Cart>[]>([]);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  useEffect(() => {
    let alive = true;
    let previousActive = false;
    let checking = false;
    const load = async () => {
      const [p, c, r] = await Promise.all([
        api<Result<Plan>[]>('/plans'),
        api<Result<Cart>[]>('/carts'),
        api<Recipe[]>('/recipes'),
      ]);
      if (alive) {
        setPlans(p);
        setCarts(c);
        setRecipes(r);
      }
    };
    setLoading(true);
    setError('');
    load()
      .catch((e) => alive && setError(errorText(e)))
      .finally(() => alive && setLoading(false));
    const timer = setInterval(async () => {
      if (checking) return;
      checking = true;
      try {
        const chats = await api<{ active: boolean }[]>('/chats');
        const active = chats.some((c) => c.active);
        if (active || previousActive) await load();
        previousActive = active;
      } catch (e) {
        if (alive) setError(errorText(e));
      } finally {
        checking = false;
      }
    }, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [kind]);
  const title = kind === 'plans' ? 'Ваши планы' : kind === 'recipes' ? 'Рецепты' : 'Покупки';
  const filtered = recipes.filter((r) =>
    r.title.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')),
  );
  return (
    <div className="page-content collection-page">
      <div className="section-intro">
        <h1>{title}</h1>
        <p>
          {kind === 'plans'
            ? 'Продуманное меню, день за днём.'
            : kind === 'recipes'
              ? 'Всё, что готовите по вашим планам.'
              : 'Всё нужное — в одном списке.'}
        </p>
      </div>
      <ErrorNote text={error} />
      {loading ? (
        <Loading />
      ) : (
        <>
          {kind === 'plans' &&
            (plans.length ? (
              <div className="plan-list">
                {plans.map(({ data: plan, status }) => {
                  const photo = recipes.find((r) => r.recipe_id === plan.meals[0]?.recipe_id);
                  return (
                    <button
                      className="plan-cover"
                      key={plan.plan_id}
                      onClick={() => go(`/plan/${plan.plan_id}`)}
                    >
                      <FoodImage src={photo?.image_url} alt="" />
                      <div className="plan-cover-body">
                        <div className="inline">
                          <span className="tag">
                            <CalendarDays size={14} />
                            {plan.days} дн.
                          </span>
                          <small>
                            {!plan.meals.length
                              ? 'Меню не составлено'
                              : status === 'needs_revision'
                                ? 'Есть замечания'
                                : 'Меню готово'}
                          </small>
                        </div>
                        <h2>План на {plan.days} дн.</h2>
                        <p>
                          {plan.meals.length
                            ? `${new Set(plan.meals.map((meal) => `${meal.day}:${meal.meal}`)).size} приёмов пищи · ≈ ${money(plan.totals.mean_rub)}`
                            : 'Посмотрите пояснение ассистента'}
                        </p>
                        <span className="cover-link">
                          Открыть меню
                          <ArrowRight size={18} />
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <Empty
                icon={<CalendarDays />}
                title="Меню начинается с разговора"
                action={
                  <button
                    className="primary"
                    onClick={() => discuss('Составь план питания с учётом моей анкеты.')}
                  >
                    Составить план
                    <ArrowRight size={18} />
                  </button>
                }
              >
                Расскажите ассистенту, на сколько дней и что вам хочется приготовить.
              </Empty>
            ))}
          {kind === 'recipes' && (
            <>
              {recipes.length > 0 && (
                <label className="search-field">
                  <Search size={18} />
                  <input
                    aria-label="Поиск среди рецептов планов"
                    placeholder="Найти рецепт"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </label>
              )}
              {filtered.length ? (
                <div className="recipe-list">
                  {filtered.map((r) => (
                    <button
                      className="recipe-row"
                      key={r.recipe_id}
                      onClick={() => go(`/recipe/${r.recipe_id}`)}
                    >
                      <FoodImage src={r.image_url} alt="" />
                      <span>
                        <strong>{r.title}</strong>
                        <small>
                          <Clock3 size={13} />
                          {r.total_minutes} мин. · {difficulties[r.difficulty]}
                        </small>
                        <small>≈ {money(r.cost.mean_rub)}</small>
                      </span>
                      <ChevronRight size={18} />
                    </button>
                  ))}
                </div>
              ) : (
                <Empty
                  icon={<BookOpen />}
                  title={search ? 'Такого рецепта здесь нет' : 'Ваши рецепты появятся здесь'}
                >
                  {search
                    ? 'Попробуйте другое название. Поиск по всей базе доступен в чате.'
                    : 'Составьте план — его рецепты сохранятся в этой подборке.'}
                </Empty>
              )}
            </>
          )}
          {kind === 'carts' &&
            (carts.length ? (
              <div className="shopping-list">
                {carts.map(({ data: cart, status }, i) => (
                  <button
                    className="shopping-summary"
                    key={cart.cart_id}
                    onClick={() => go(`/cart/${cart.cart_id}`)}
                  >
                    <div className="inline">
                      <ShoppingBag size={23} />
                      <span className={`tag ${status === 'partial' ? 'warning' : ''}`}>
                        {status === 'partial'
                          ? 'Частично'
                          : status === 'needs_revision'
                            ? 'Есть замечания'
                            : 'Готово'}
                      </span>
                    </div>
                    <h2>{i === 0 ? 'Последние покупки' : 'Список покупок'}</h2>
                    <p>
                      {cart.items.length} позиций
                      {cart.unresolved.length > 0 ? ` · ${cart.unresolved.length} не подобрано` : ''}
                    </p>
                    <div className="inline">
                      <strong>
                        {cart.total_is_complete ? '' : 'От '}
                        {money(cart.subtotal_rub)}
                      </strong>
                      <ArrowRight size={19} />
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <Empty
                icon={<ShoppingBag />}
                title="Сначала меню — потом покупки"
                action={
                  <button className="primary" onClick={() => discuss('Помоги собрать корзину продуктов.')}>
                    Обсудить покупки
                    <ArrowRight size={18} />
                  </button>
                }
              >
                Ассистент соберёт продукты из плана, рецептов или вашего списка.
              </Empty>
            ))}
          {kind === 'plans' && plans.length > 0 && (
            <button className="secondary full" onClick={() => discuss('Давай составим новый план питания.')}>
              <ChefHat size={19} />
              Обсудить новый план
            </button>
          )}
        </>
      )}
    </div>
  );
}
