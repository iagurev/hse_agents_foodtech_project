import type { Totals } from '../api';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowLeft, ArrowUpRight, ChefHat, Ellipsis, X } from 'lucide-react';
import { api, errorText, go, number } from '../api';

export function ErrorNote({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (text) {
      ref.current?.scrollIntoView({ block: 'center' });
      ref.current?.focus({ preventScroll: true });
    }
  }, [text]);
  return text ? (
    <p ref={ref} className="error-note" role="alert" tabIndex={-1}>
      {text}
    </p>
  ) : null;
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <span className="spinner" />
      Загружаем…
    </div>
  );
}
export function Empty({
  icon,
  title,
  children,
  action,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon}</div>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function Sheet({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef(document.activeElement as HTMLElement | null);
  useEffect(() => {
    ref.current?.showModal();
    const element = ref.current;
    return () => {
      element?.close();
      const target = opener.current;
      requestAnimationFrame(() => {
        if (!target?.isConnected) return;
        const menu = target.closest('details');
        (menu && !menu.open ? menu.querySelector('summary') : target)?.focus();
      });
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="sheet"
      aria-label={title}
      onCancel={close}
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="sheet-content">
        <div className="sheet-handle" />
        <header>
          <h2>{title}</h2>
          <button className="icon-button" onClick={close} aria-label="Закрыть">
            <X size={22} />
          </button>
        </header>
        {children}
      </div>
    </dialog>
  );
}
export function BackHeader({ title, back, children }: { title: string; back: string; children?: ReactNode }) {
  return (
    <header className="page-header">
      <button className="icon-button" aria-label="Назад" onClick={() => go(back)}>
        <ArrowLeft size={22} />
      </button>
      <h1>{title}</h1>
      {children}
    </header>
  );
}
export function FoodImage({
  src,
  alt,
  className = '',
}: {
  src?: string | null;
  alt: string;
  className?: string;
}) {
  const [result, setResult] = useState<{ src: string; status: 'loaded' | 'failed' } | null>(null);
  const loaded = result?.src === src && result?.status === 'loaded';
  const failed = result?.src === src && result?.status === 'failed';
  return (
    <div className={`food-photo media ${className}`}>
      {!loaded && (
        <span className="photo-fallback" aria-label="Фотография недоступна">
          <ChefHat size={30} strokeWidth={1.3} />
        </span>
      )}
      {src && !failed && (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          className={loaded ? 'loaded' : ''}
          onLoad={() => setResult({ src, status: 'loaded' })}
          onError={() => setResult({ src, status: 'failed' })}
        />
      )}
    </div>
  );
}
export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  if (!/^https?:\/\//i.test(href)) return null;
  return (
    <a className="external-link" href={href} target="_blank" rel="noopener noreferrer">
      {children}
      <ArrowUpRight size={18} />
    </a>
  );
}

export function useRestoreScroll(ready: boolean) {
  const route = location.hash;
  const saved = useRef({ route, top: Number(sessionStorage.getItem(`scroll:${route}`) || 0) });
  if (saved.current.route !== route)
    saved.current = { route, top: Number(sessionStorage.getItem(`scroll:${route}`) || 0) };
  useEffect(() => {
    if (ready) {
      const main = document.querySelector('main');
      if (main) main.scrollTop = saved.current.top;
    }
  }, [ready, route]);
}

export function ObjectActions({
  kind,
  id,
  title,
  hidden,
  renamed,
}: {
  kind: 'plans' | 'carts';
  id: string;
  title: string;
  hidden: boolean;
  renamed: (title: string) => void;
}) {
  const [mode, setMode] = useState<'rename' | 'hide' | null>(null);
  const menu = useRef<HTMLDetailsElement>(null);
  const [value, setValue] = useState(title);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const normalized = value.replace(/\s+/g, ' ').trim();
  async function save() {
    setBusy(true);
    setError('');
    try {
      if (mode === 'rename') {
        const result = await api<{ title: string }>(`/${kind}/${id}/title`, 'PATCH', { title: normalized });
        renamed(result.title);
      } else {
        await api(`/${kind}/${id}/hide`, 'POST');
        go(`/${kind}`);
      }
      setMode(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <details className="object-menu" ref={menu}>
        <summary className="icon-button" aria-label="Действия с сохранённым списком">
          <Ellipsis size={22} />
        </summary>
        <div className="object-actions">
          <button
            className="text-button"
            onClick={() => {
              setValue(title);
              setError('');
              setMode('rename');
              if (menu.current) menu.current.open = false;
            }}
          >
            Переименовать
          </button>
          {!hidden && (
            <button
              className="text-button"
              onClick={() => {
                setError('');
                setMode('hide');
                if (menu.current) menu.current.open = false;
              }}
            >
              Убрать из списка
            </button>
          )}
          {hidden && <span className="muted">Убрано из списка</span>}
        </div>
      </details>
      {mode && (
        <Sheet
          title={mode === 'rename' ? 'Название' : 'Убрать из списка?'}
          close={() => !busy && setMode(null)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void save();
            }}
          >
            {mode === 'rename' ? (
              <label>
                Название
                <input autoFocus value={value} maxLength={80} onChange={(e) => setValue(e.target.value)} />
              </label>
            ) : (
              <p>Объект исчезнет из списка. Его можно будет открыть и изменить по карточке в переписке.</p>
            )}
            <ErrorNote text={error} />
            <button
              className="primary full"
              disabled={busy || (mode === 'rename' && (!normalized || normalized.length > 80))}
            >
              {busy ? 'Сохраняем…' : mode === 'rename' ? 'Сохранить название' : 'Убрать из списка'}
            </button>
          </form>
        </Sheet>
      )}
    </>
  );
}

export function Nutrition({ value }: { value: Omit<Totals, 'mean_rub'> | null }) {
  return (
    <div className="nutrition">
      {[
        ['ккал', value?.kcal],
        ['белки, г', value?.protein_g],
        ['жиры, г', value?.fat_g],
        ['углев., г', value?.carbs_g],
      ].map(([label, v]) => (
        <div key={label}>
          <strong>{number(v as number)}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}
