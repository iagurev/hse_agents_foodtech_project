import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowLeft, ArrowUpRight, ChefHat, X } from 'lucide-react';
import { go } from './api';

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
  useEffect(() => {
    ref.current?.showModal();
    const element = ref.current;
    return () => element?.close();
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
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setFailed(false);
    setLoaded(false);
  }, [src]);
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
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
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
