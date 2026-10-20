import { useState } from 'react';
import { BookOpen, CalendarDays, Check, ChevronRight, Plus, ShoppingBag, X } from 'lucide-react';
import { openDetail, money } from '../api';
import type { Artifact, ObjectReference } from '../api';
import { FoodImage } from './ui';
import { positiveNumber, cookingTime, servingDescription } from '../preview';

export function References({
  values,
  remove,
}: {
  values: ObjectReference[];
  remove?: (index: number) => void;
}) {
  if (!values.length) return null;
  return (
    <div className="message-references">
      {values.map((ref, index) => {
        const Icon = ref.kind === 'plan' ? CalendarDays : ref.kind === 'cart' ? ShoppingBag : BookOpen;
        const title = ref.title || { plan: 'Меню', cart: 'Покупки', recipe: 'Рецепт' }[ref.kind];
        return (
          <div className="message-reference" key={`${ref.kind}:${ref.id}:${index}`}>
            <button
              type="button"
              onClick={() =>
                openDetail(
                  `/${ref.kind}/${ref.id}${ref.kind === 'recipe' && ref.servings !== undefined ? `?servings=${ref.servings}` : ''}`,
                )
              }
            >
              <Icon size={16} />
              <span>
                {title}
                {ref.servings !== undefined && (
                  <small>{ref.servings.toLocaleString('ru-RU')} баз. порц.</small>
                )}
              </span>
            </button>
            {remove && (
              <button type="button" aria-label={`Убрать привязку: ${title}`} onClick={() => remove(index)}>
                <X size={16} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ArtifactCard({ card, servings = 1 }: { card: Artifact; servings?: number }) {
  const title =
    card.title || (card.kind === 'plan' ? `План на ${card.days || 'несколько'} дн.` : 'Список покупок');
  return (
    <button
      className="artifact-card"
      onClick={() =>
        openDetail(`/${card.kind}/${card.id}${card.kind === 'recipe' ? `?servings=${servings}` : ''}`)
      }
    >
      {card.kind === 'recipe' ? (
        <FoodImage src={card.image_url} alt="" />
      ) : (
        <span className="artifact-icon">
          {card.kind === 'plan' ? <CalendarDays size={24} /> : <ShoppingBag size={24} />}
        </span>
      )}
      <span>
        <strong>{title}</strong>
        <small>
          {card.kind === 'recipe'
            ? [
                card.minutes != null ? cookingTime(card.minutes) : '',
                card.kcal != null ? `${card.kcal} ккал` : '',
              ]
                .filter(Boolean)
                .join(' · ')
            : card.kind === 'plan'
              ? money(card.cost)
              : money(card.total)}
          {card.status === 'needs_revision'
            ? ' · Есть замечания'
            : card.status === 'partial'
              ? ' · Частично'
              : ''}
        </small>
      </span>
      <ChevronRight size={19} />
    </button>
  );
}
export function RecipePicker({
  cards,
  storageKey,
  disabled,
  send,
}: {
  cards: Artifact[];
  storageKey: string;
  disabled: boolean;
  send: (text: string, references?: ObjectReference[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Record<string, string>>(() => {
    try {
      return JSON.parse(localStorage.getItem(storageKey) || '{}');
    } catch {
      return {};
    }
  });
  const unique = [...new Map(cards.map((c) => [c.id, c])).values()];
  const chosen = unique.filter((c) => selected[c.id] !== undefined);
  const valid = chosen.every((c) => positiveNumber(selected[c.id]) !== null);
  function update(next: Record<string, string>) {
    setSelected(next);
    localStorage.setItem(storageKey, JSON.stringify(next));
  }
  return (
    <div className="recipe-picker">
      {unique.map((card) => (
        <div className="recipe-option" key={card.id}>
          <ArtifactCard card={card} servings={positiveNumber(selected[card.id] || '1') ?? 1} />
          <p className="recipe-portion">
            Одна порция: {servingDescription(card.base_serving || 'см. рецепт')}
          </p>
          <div className="recipe-choice">
            <button
              className="secondary"
              aria-pressed={selected[card.id] !== undefined}
              aria-label={`Выбрать: ${card.title}`}
              onClick={() => {
                const next = { ...selected };
                if (next[card.id] === undefined) next[card.id] = '1';
                else delete next[card.id];
                update(next);
              }}
            >
              {selected[card.id] !== undefined ? (
                <>
                  <Check size={17} />
                  Выбрано
                </>
              ) : (
                <>
                  <Plus size={17} />
                  Выбрать
                </>
              )}
            </button>
            {selected[card.id] !== undefined && (
              <label className="quantity-field">
                Порции
                <input
                  inputMode="decimal"
                  aria-label={`Порции: ${card.title}`}
                  value={selected[card.id]}
                  aria-invalid={positiveNumber(selected[card.id]) === null}
                  onChange={(e) => update({ ...selected, [card.id]: e.target.value })}
                />
              </label>
            )}
          </div>
        </div>
      ))}
      <div className="action-stack">
        <button
          className="primary full"
          disabled={disabled || !chosen.length || !valid}
          onClick={() =>
            void send(
              'Собери покупки для выбранных блюд.',
              chosen.map((c) => ({
                kind: 'recipe',
                id: c.id,
                title: c.title,
                servings: positiveNumber(selected[c.id])!,
              })),
            )
          }
        >
          <ShoppingBag size={18} />
          Собрать покупки{chosen.length ? ` · ${chosen.length}` : ''}
        </button>
        {!valid && (
          <p className="error-note" role="status">
            Укажите положительное число порций.
          </p>
        )}
        <button
          className="text-button"
          disabled={disabled}
          onClick={() =>
            void send(
              'Предложи другие блюда вместо этих, с учётом наших пожеланий.',
              unique.map((c) => ({ kind: 'recipe', id: c.id, title: c.title })),
            )
          }
        >
          Предложить другое
        </button>
      </div>
    </div>
  );
}
