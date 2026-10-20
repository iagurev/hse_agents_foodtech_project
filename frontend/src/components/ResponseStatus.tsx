import { useEffect, useState } from 'react';
import { Check } from 'lucide-react';
import { duration, isActive } from '../api';
import type { Turn } from '../api';

const tools: Record<string, string> = {
  generate_plan: 'Составляю меню',
  edit_plan: 'Обновляю меню',
  search_recipes: 'Ищу рецепты',
  get_recipes: 'Открываю рецепты',
  create_cart: 'Собираю список продуктов',
  edit_cart: 'Обновляю покупки',
  search_products: 'Ищу продукты',
  get_product_analogs: 'Подбираю замену',
  update_memory: 'Обновляю память',
  list_plans: 'Смотрю планы',
  get_plan: 'Открываю план',
  list_carts: 'Смотрю покупки',
  get_cart: 'Открываю корзину',
};

const stages: Record<string, string> = {
  plan_prepare: 'Подготавливаю рецепты',
  plan_generate: 'Составляю меню',
  plan_check: 'Проверяю расчёты',
  plan_correct: 'Корректирую меню',
  plan_save: 'Сохраняю меню',
  cart_prepare: 'Собираю список продуктов',
  cart_search: 'Ищу товары во ВкусВилле',
  cart_select: 'Подбираю подходящие товары',
  cart_calculate: 'Рассчитываю количество и стоимость',
  cart_link: 'Создаю ссылку на корзину',
  cart_save: 'Сохраняю корзину',
};

export function ResponseTime({ turn }: { turn: Turn }) {
  const active = isActive(turn.state);
  const [seconds, setSeconds] = useState(turn.elapsed_seconds ?? 0);
  useEffect(() => {
    const base = turn.elapsed_seconds;
    if (!active || base == null) return;
    const received = performance.now();
    const tick = () => setSeconds(base + (performance.now() - received) / 1000);
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [turn.turn_id, turn.elapsed_seconds, active]);
  if (turn.elapsed_seconds == null) return null;
  const value = duration(active ? seconds : turn.elapsed_seconds);
  return (
    <span
      className="response-time"
      aria-live="off"
      title={turn.elapsed_is_partial ? 'Известное время до прерывания сервера' : 'Общее время ответа'}
    >
      {turn.elapsed_is_partial ? '≥ ' : ''}
      {value}
    </span>
  );
}

export function OperationStatus({ turn }: { turn: Turn }) {
  const progress = turn.progress;
  const stage = progress?.stage;
  const searching = stage === 'cart_search';
  const searchFinished =
    stage?.startsWith('cart_') &&
    !searching &&
    progress?.total != null &&
    progress.completed === progress.total;
  const label =
    (stage && stages[stage]) ||
    (turn.tool && tools[turn.tool]) ||
    (turn.state === 'guarding' ? 'Обрабатываю запрос' : 'Готовлю ответ');
  return (
    <div className="operation-status" role="status" aria-atomic="true">
      {progress && searchFinished && (
        <div className="operation-completed">
          <Check size={14} aria-hidden="true" />
          <span>
            Поиск завершён: варианты найдены для {progress.found} из {progress.total} продуктов
          </span>
        </div>
      )}
      <div className="working">
        <span className="spinner" aria-hidden="true" />
        <span>{label}</span>
      </div>
      {progress && searching && progress.total != null && (
        <div className="operation-detail">
          Обработано {progress.completed} из {progress.total} продуктов
        </div>
      )}
      {stage === 'cart_select' && (
        <div className="operation-detail">Учитываю состав, фасовку и предпочтения</div>
      )}
    </div>
  );
}
