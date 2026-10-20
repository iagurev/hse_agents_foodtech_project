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
import { api, difficulties, errorText, openDetail, money } from '../api';
import type { Cart, Plan, Recipe, RecipeSearchData, RecipeSearchLimits, RecipeSummary, Result } from '../api';
import { Empty, ErrorNote, FoodImage, Loading, useRestoreScroll } from '../components/ui';

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
  const [search, setSearch] = useState(sessionStorage.getItem('recipe-search') || '');
  const [found, setFound] = useState<RecipeSummary[]>([]);
  const [searching, setSearching] = useState(false);
  useEffect(() => {
    if (kind !== 'recipes' || !search.trim()) {
      setSearching(false);
      setFound([]);
      return;
    }
    let alive = true;
    setSearching(true);
    setError('');
    setFound([]);
    const timer = setTimeout(() => {
      api<Result<RecipeSearchData>>('/recipes/search', 'POST', {
        query: search,
        limits: {
          exclude_products: null,
          forbidden_equipment: null,
          max_total_minutes: null,
          max_difficulty: null,
          meal_roles: null,
          diet: null,
          budget_rub: null,
        } satisfies RecipeSearchLimits,
      })
        .then((result) => {
          if (alive) {
            setFound(result.data.recipes);
          }
        })
        .catch((e) => alive && setError(errorText(e)))
        .finally(() => alive && setSearching(false));
    }, 350);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [search, kind]);
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
  useRestoreScroll(!loading && !searching);
  const title = kind === 'plans' ? 'Ваши планы' : kind === 'recipes' ? 'Рецепты' : 'Покупки';
  const filtered = search.trim() ? found : recipes;
  return (
    <div className="page-content collection-page">
      <div className="section-intro">
        <h1>{title}</h1>
        <p>
          {kind === 'plans'
            ? 'Продуманное меню, день за днём.'
            : kind === 'recipes'
              ? 'Ваши подборки и поиск по всей базе рецептов.'
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
                      onClick={() => openDetail(`/plan/${plan.plan_id}`)}
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
                        <h2>{plan.title}</h2>
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
              {
                <label className="search-field">
                  <Search size={18} />
                  <input
                    aria-label="Поиск рецептов по всей базе"
                    placeholder="Название блюда или ингредиенты"
                    value={search}
                    onChange={(e) => {
                      setSearch(e.target.value);
                      sessionStorage.setItem('recipe-search', e.target.value);
                    }}
                  />
                </label>
              }
              <h2>{search.trim() ? 'Результаты по всей базе' : 'Из ваших планов'}</h2>
              {searching ? (
                <Loading />
              ) : filtered.length ? (
                <div className="recipe-list">
                  {filtered.map((r) => (
                    <button
                      className="recipe-row"
                      key={r.recipe_id}
                      onClick={() => openDetail(`/recipe/${r.recipe_id}`)}
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
                  title={search ? 'Подходящие рецепты не найдены' : 'Найдите что-нибудь вкусное'}
                >
                  {search
                    ? 'Попробуйте другое название блюда или ингредиенты.'
                    : 'Введите блюдо или продукты в поиске. Рецепты ваших планов также сохранятся здесь.'}
                </Empty>
              )}
            </>
          )}
          {kind === 'carts' &&
            (carts.length ? (
              <div className="shopping-list">
                {carts.map(({ data: cart, status }) => (
                  <button
                    className="shopping-summary"
                    key={cart.cart_id}
                    onClick={() => openDetail(`/cart/${cart.cart_id}`)}
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
                    <h2>{cart.title}</h2>
                    <p>
                      Позиций: {cart.items.length}
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
