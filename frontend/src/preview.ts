import type { Item, Recipe, Totals } from './api';

export function servingDescription(value: string): string {
  return value.replace('авторской адаптации', 'приготовленного блюда');
}

export function ingredientAmount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  if (value > 0 && value < 0.01) return '<0,01';
  return value.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

export function cookingTime(minutes: number): string {
  if (minutes < 60) return `${minutes} мин.`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return `${hours} ч${rest ? ` ${rest} мин.` : ''}`;
}

export function positiveNumber(text: string): number | null {
  if (!/^(?:\d+(?:[.,]\d+)?|[.,]\d+)$/.test(text.trim())) return null;
  const value = Number(text.replace(',', '.'));
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function packageNumber(text: string, item?: Item): number | null {
  const value = positiveNumber(text);
  if (value === null) return null;
  const scaled = item?.product?.sale_unit ? value * 100 : value;
  return Math.abs(scaled - Math.round(scaled)) < 1e-8 ? value : null;
}

export function mealTotals(recipe: Recipe | undefined, servings: string): Totals | null {
  const count = positiveNumber(servings);
  if (!recipe || count === null) return null;
  const base = { ...recipe.nutrition, mean_rub: recipe.cost.mean_rub };
  if (Object.values(base).some((v) => v == null || !Number.isFinite(v))) return null;
  const totals = Object.fromEntries(
    Object.entries(base).map(([k, v]) => [k, v * count]),
  ) as unknown as Totals;
  return Object.values(totals).every(Number.isFinite) ? totals : null;
}

export function sumTotals(rows: (Totals | null)[]): Totals | null {
  if (rows.some((r) => r === null)) return null;
  const total: Totals = { kcal: 0, protein_g: 0, fat_g: 0, carbs_g: 0, mean_rub: 0 };
  for (const row of rows) for (const key of Object.keys(total) as (keyof Totals)[]) total[key] += row![key];
  return Object.values(total).every(Number.isFinite) ? total : null;
}

export function dailyAverage(total: Totals | null, days: number): Totals | null {
  if (!total || !Number.isInteger(days) || days <= 0) return null;
  if (Object.values(total).some((value) => value == null || !Number.isFinite(value))) return null;
  return Object.fromEntries(
    Object.entries(total).map(([key, value]) => [key, value / days]),
  ) as unknown as Totals;
}

export function itemPrice(item: Item | undefined, text: string): number | null {
  const quantity = packageNumber(text, item);
  const price = item?.product?.price_rub;
  if (quantity === null || price == null) return null;
  const amount = Math.round((price * quantity + Number.EPSILON) * 100) / 100;
  return Number.isFinite(amount) ? amount : null;
}

export function knownSubtotal(prices: (number | null)[]): number | null {
  const known = prices.filter((price): price is number => price !== null);
  if (prices.length && !known.length) return null;
  const total = Math.round(known.reduce((sum, price) => sum + price, 0) * 100) / 100;
  return Number.isFinite(total) ? total : null;
}
