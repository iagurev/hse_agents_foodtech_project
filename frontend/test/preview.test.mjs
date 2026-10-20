import { test } from 'node:test';
import assert from 'node:assert/strict';
import { positiveNumber, packageNumber, mealTotals, sumTotals, itemPrice, knownSubtotal, ingredientAmount, cookingTime } from '../src/preview.ts';

const recipe = { nutrition: { kcal: 180, protein_g: 6, fat_g: 3, carbs_g: 30 }, cost: { mean_rub: 20 } };
test('fractional servings, removal and unknown/incomplete numbers', () => {
  assert.equal(positiveNumber('1,5'), 1.5);
  for (const text of ['', ' ', '1.', '0', '-1', 'NaN', 'Infinity']) assert.equal(positiveNumber(text), null);
  assert.deepEqual(mealTotals(recipe, '1.5'), { kcal: 270, protein_g: 9, fat_g: 4.5, carbs_g: 45, mean_rub: 30 });
  assert.equal(sumTotals([mealTotals(recipe, '1'), mealTotals(recipe, '1.5')]).mean_rub, 50);
  assert.equal(sumTotals([mealTotals(recipe, '1.5')]).mean_rub, 30);
  assert.equal(sumTotals([mealTotals(recipe, '')]), null);
  assert.equal(mealTotals(undefined, '1'), null);
  assert.equal(sumTotals([]).mean_rub, 0);
});
test('weighted quantities and unknown prices stay distinct from zero', () => {
  const weighted = { product: { sale_unit: 'kg', price_rub: 145.5 } };
  assert.equal(itemPrice(weighted, '0,25'), 36.38);
  assert.equal(packageNumber('0.251', weighted), null);
  assert.equal(packageNumber('1.5', { product: { sale_unit: null } }), null);
  assert.equal(itemPrice({ product: { price_rub: null } }, '2'), null);
  assert.equal(itemPrice(undefined, '2'), null);
  assert.equal(itemPrice(weighted, ''), null);
});

test('subtotal describes the known part and does not assign prices to unknown products', () => {
  assert.equal(knownSubtotal([36.38, null]), 36.38);
  assert.equal(knownSubtotal([null]), null);
  assert.equal(knownSubtotal([]), 0);
});

 test('ingredient quantities do not round small positive amounts to zero', () => {
  assert.equal(ingredientAmount(0.035714), '0,04');
  assert.equal(ingredientAmount(0.001), '<0,01');
  assert.equal(ingredientAmount(250 * 1.5), '375');
  assert.equal(ingredientAmount(null), '—');
  assert.equal(cookingTime(485), '8 ч 5 мин.');
});

test('daily average uses all planned days and preserves unknown draft values', async () => {
  const { dailyAverage } = await import('../src/preview.ts');
  const total = { kcal: 6000, protein_g: 300, fat_g: 180, carbs_g: 720, mean_rub: 1500 };
  assert.equal(dailyAverage(total, 3).kcal, 2000);
  assert.equal(dailyAverage(total, 3).protein_g, 100);
  assert.equal(total.mean_rub, 1500);
  assert.equal(dailyAverage(null, 3), null);
  assert.equal(dailyAverage({ ...total, kcal: null }, 3), null);
  assert.equal(dailyAverage(total, 0), null);
});
