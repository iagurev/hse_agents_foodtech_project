export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public data?: unknown,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const message =
      typeof data.detail === 'string'
        ? data.detail
        : data.error?.message ||
          (Array.isArray(data.detail)
            ? 'Проверьте заполненные поля.'
            : 'Не удалось выполнить действие. Попробуйте ещё раз.');
    throw new ApiError(response.status, message, data);
  }
  return data as T;
}
export const errorText = (error: unknown) =>
  error instanceof TypeError
    ? 'Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.'
    : error instanceof Error
      ? error.message
      : 'Не удалось выполнить действие';
export const money = (value?: number | null) =>
  value == null ? 'Цена неизвестна' : `${Math.round(value).toLocaleString('ru-RU')} ₽`;
export const number = (value?: number | null, maximumFractionDigits = 1) =>
  value == null ? '—' : value.toLocaleString('ru-RU', { maximumFractionDigits });
export const go = (path: string) => {
  window.location.hash = path;
};
export const detailPath = (path: string, from = location.hash.slice(1) || '/chat') =>
  `${path}${path.includes('?') ? '&' : '?'}from=${encodeURIComponent(from)}`;
export const openDetail = (path: string) => go(detailPath(path));

export type Difficulty = 'easy' | 'intermediate' | 'advanced';
export const difficulties = { easy: 'Простая', intermediate: 'Средняя', advanced: 'Высокая' };
export const budgets = ['15-20', '20-25', '25-30', '30-35', '35-40', '40-50', 'unlimited'] as const;
export const budgetLabel = (value: string) =>
  value === 'unlimited' ? 'Без ограничений' : `${value.replace('-', '–')} тыс. ₽`;
export const goals = {
  healthier: 'Питаться здоровее',
  maintain: 'Поддерживать форму',
  lose_weight: 'Снизить вес',
  gain_mass: 'Набрать массу',
  ideas: 'Понять, что готовить',
};
export const activities = {
  inactive: ['Мало движения', 'В основном сидячий день, обычные бытовые дела'],
  low_active: ['Немного движения', 'Бытовые дела и регулярные прогулки'],
  active: ['Много движения', 'Подвижный день и заметная физическая нагрузка'],
  very_active: ['Очень много движения', 'Тяжёлая физическая работа или длительные интенсивные тренировки'],
} as const;
export interface Profile {
  goal: keyof typeof goals;
  sex: 'female' | 'male';
  age: number;
  height_cm: number;
  weight_kg: number;
  activity: keyof typeof activities | null;
  monthly_budget: string | null;
  cooking_difficulty: Difficulty | null;
  restrictions: string;
}
export interface Me {
  user_id: string;
  login: string;
  profile: Profile;
  memory: string;
  memory_revision: number;
}
export interface Recipe {
  recipe_id: string;
  title: string;
  image_url: string | null;
  ingredients: { product_id: string; name: string; amount: number; unit: string }[];
  steps: string[];
  restrictions: string;
  base_serving: string;
  active_minutes: number;
  total_minutes: number;
  difficulty: Difficulty;
  equipment: string[];
  source_url: string;
  nutrition: { kcal: number; protein_g: number; fat_g: number; carbs_g: number };
  cost: { mean_rub: number; min_rub: number; max_rub: number };
}
export type RecipeSummary = Pick<
  Recipe,
  'recipe_id' | 'title' | 'image_url' | 'total_minutes' | 'difficulty' | 'cost'
>;
export interface RecipeSearchLimits {
  exclude_products: string[] | null;
  forbidden_equipment: string[] | null;
  max_total_minutes: number | null;
  max_difficulty: 'easy' | 'intermediate' | 'advanced' | null;
  meal_roles: ('breakfast' | 'lunch' | 'dinner' | 'snack' | 'dessert')[] | null;
  diet: 'vegetarian' | 'vegan' | null;
  budget_rub: number | null;
}

export interface RecipeSearchData {
  recipes: RecipeSummary[];
}
export interface Totals {
  kcal: number;
  protein_g: number;
  fat_g: number;
  carbs_g: number;
  mean_rub: number;
}
export interface Meal {
  day: number;
  meal: string;
  recipe_id: string;
  servings: number;
}
export interface ObjectMeta {
  title: string;
  hidden: boolean;
}
export interface Plan extends ObjectMeta {
  plan_id: string;
  parent_id: string | null;
  days: number;
  meals: Meal[];
  explanation: string;
  totals: Totals;
  daily: Record<string, Totals>;
  limits: { budget_rub: number | null; max_difficulty: Difficulty | null };
}
export interface Item {
  sku_id: string;
  packages: number;
  product: {
    title: string;
    price_rub: number | null;
    source_url?: string;
    sale_unit?: 'kg' | 'l' | null;
  } | null;
}
export interface Cart extends ObjectMeta {
  cart_id: string;
  parent_id: string | null;
  items: Item[];
  unresolved: {
    product_id: string | null;
    product_name?: string | null;
    recipe_ids: string[];
    reason: string;
  }[];
  subtotal_rub: number | null;
  total_is_complete: boolean;
  cart_url: string | null;
  cart_links?: { sku_ids: string[]; url: string | null; error: string | null }[];
  link_error: string | null;
}
export interface Result<T> {
  data: T;
  status: string;
  issues: { message: string }[];
  error?: { message: string } | null;
}
export interface Artifact {
  kind: 'plan' | 'cart' | 'recipe';
  id: string;
  title?: string;
  days?: number;
  status?: string;
  image_url?: string;
  total?: number;
  cost?: number;
  base_serving?: string;
  selectable?: boolean;
  minutes?: number;
  kcal?: number;
  hidden?: boolean;
}
export interface OperationProgress {
  name: string;
  call_id?: string | null;
  stage?: string;
  completed?: number;
  total?: number;
  found?: number;
}
export interface Turn {
  turn_id: string;
  text: string;
  references?: ObjectReference[];
  output: string;
  state: string;
  artifacts: Artifact[];
  tool?: string | null;
  progress?: OperationProgress | null;
  elapsed_seconds?: number | null;
  elapsed_is_partial?: boolean;
}
export interface ObjectReference {
  kind: 'plan' | 'cart' | 'recipe';
  id: string;
  title?: string;
  servings?: number;
}
export interface ChatDraft {
  text: string;
  references: ObjectReference[];
}
export type Discuss = (text: string, references?: ObjectReference[]) => void;
export interface Chat {
  chat_id: string;
  title: string;
  active: boolean;
}
export function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return [hours ? `${hours} ч` : '', hours || minutes ? `${minutes} мин` : '', `${total % 60} с`]
    .filter(Boolean)
    .join(' ');
}
export const isActive = (state: string) => ['queued', 'guarding', 'running'].includes(state);

export const mealName = (value: string) =>
  ({ breakfast: 'Завтрак', lunch: 'Обед', dinner: 'Ужин', snack: 'Перекус', dessert: 'Десерт' })[
    value.toLowerCase()
  ] || value;
