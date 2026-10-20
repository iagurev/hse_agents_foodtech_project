import { useEffect, useState } from 'react';
import { Check, MessageCircle, Pencil, Trash2 } from 'lucide-react';
import { api, ApiError, errorText, go, detailPath, money } from '../api';
import type { Cart, Result, Discuss } from '../api';
import {
  BackHeader,
  ErrorNote,
  ExternalLink,
  Loading,
  ObjectActions,
  useRestoreScroll,
} from '../components/ui';
import { positiveNumber, packageNumber, itemPrice, knownSubtotal } from '../preview';

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
