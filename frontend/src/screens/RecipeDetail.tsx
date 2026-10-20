import { useEffect, useState } from 'react';
import { Clock3, MessageCircle } from 'lucide-react';
import { api, difficulties, errorText, money, number } from '../api';
import type { Recipe, Discuss } from '../api';
import { BackHeader, ErrorNote, ExternalLink, FoodImage, Loading, useRestoreScroll } from '../components/ui';
import { mealTotals, ingredientAmount, cookingTime, servingDescription } from '../preview';
import { Nutrition } from '../components/ui';

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
