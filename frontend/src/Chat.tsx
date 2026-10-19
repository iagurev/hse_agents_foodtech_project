import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  BookOpen,
  CalendarDays,
  Check,
  ChevronRight,
  Leaf,
  MessageCircle,
  Pencil,
  Plus,
  ShoppingBag,
  Square,
  Trash2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, errorText, go, isActive, money } from './api';
import type { Artifact, Chat, Turn } from './api';
import { ErrorNote, FoodImage, Loading, Sheet } from './ui';

const tools: Record<string, string> = {
  generate_plan: 'Составляем план',
  edit_plan: 'Обновляем план',
  search_recipes: 'Ищем рецепты',
  get_recipes: 'Открываем рецепты',
  create_cart: 'Собираем покупки',
  edit_cart: 'Обновляем покупки',
  search_products: 'Ищем продукты',
  get_product_analogs: 'Подбираем замену',
  update_memory: 'Обновляем память',
  list_plans: 'Смотрим планы',
  get_plan: 'Открываем план',
  list_carts: 'Смотрим покупки',
  get_cart: 'Открываем корзину',
};
export function ArtifactCard({ card }: { card: Artifact }) {
  const title =
    card.title || (card.kind === 'plan' ? `План на ${card.days || 'несколько'} дн.` : 'Список покупок');
  return (
    <button className="artifact-card" onClick={() => go(`/${card.kind}/${card.id}`)}>
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
            ? 'Ингредиенты и приготовление'
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
export function ChatScreen({
  userId,
  draft,
  setDraft,
}: {
  userId: string;
  draft: string;
  setDraft: (s: string) => void;
}) {
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatId, setChatId] = useState(localStorage.getItem(`chat:${userId}`) || '');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [list, setList] = useState(false);
  const [rename, setRename] = useState<Chat | null>(null);
  const [deleteChat, setDeleteChat] = useState<Chat | null>(null);
  const [title, setTitle] = useState('');
  const [unread, setUnread] = useState(false);
  const feed = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const request = useRef<{ id: string; text: string } | null>(null);
  const current = chats.find((c) => c.chat_id === chatId);
  const active = turns.find((t) => isActive(t.state));
  const refreshChats = useCallback(async () => {
    const rows = await api<Chat[]>('/chats');
    setChats(rows);
    return rows;
  }, []);
  const select = useCallback(
    (id: string) => {
      setChatId(id);
      localStorage.setItem(`chat:${userId}`, id);
      setList(false);
      setError('');
      follow.current = true;
    },
    [userId],
  );
  useEffect(() => {
    let alive = true;
    refreshChats()
      .then((rows) => {
        if (!alive) return;
        if (!rows.some((c) => c.chat_id === chatId)) select(rows[0]?.chat_id || '');
      })
      .catch((e) => alive && setError(errorText(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [refreshChats]); // Initial selection only.
  useEffect(() => {
    let alive = true;
    if (!chatId) {
      setTurns([]);
      return;
    }
    setLoading(true);
    api<Turn[]>(`/chats/${chatId}/messages`)
      .then((rows) => alive && setTurns(rows))
      .catch((e) => alive && setError(errorText(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [chatId]);
  useEffect(() => {
    if (!active || !chatId) return;
    const id = active.turn_id;
    const events = new EventSource(`/api/chats/${chatId}/turns/${id}/events`);
    events.onmessage = (event) => {
      const update = JSON.parse(event.data) as Turn;
      setTurns((rows) => rows.map((row) => (row.turn_id === id ? { ...row, ...update } : row)));
      if (!isActive(update.state)) {
        events.close();
        api<Turn[]>(`/chats/${chatId}/messages`)
          .then(setTurns)
          .catch((e) => setError(errorText(e)));
        void refreshChats();
      }
    };
    return () => events.close();
  }, [active?.turn_id, chatId, refreshChats]);
  useEffect(() => {
    if (follow.current) {
      bottom.current?.scrollIntoView({ block: 'end', behavior: 'instant' });
      setUnread(false);
    } else if (active) setUnread(true);
  }, [turns, loading]);
  async function create() {
    setBusy(true);
    setError('');
    try {
      const chat = await api<Chat>('/chats', 'POST');
      await refreshChats();
      select(chat.chat_id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function send() {
    const text = draft.trim();
    if (!text || active || busy) return;
    setBusy(true);
    setError('');
    if (!request.current || request.current.text !== text)
      request.current = { id: crypto.randomUUID(), text };
    try {
      let id = chatId;
      if (!id) {
        const chat = await api<Chat>('/chats', 'POST');
        id = chat.chat_id;
        select(id);
      }
      const result = await api<{ turn_id: string; state: string }>(`/chats/${id}/turns`, 'POST', {
        turn_id: request.current.id,
        text,
      });
      setTurns((rows) =>
        rows.some((t) => t.turn_id === result.turn_id)
          ? rows
          : [...rows, { ...result, text, output: '', artifacts: [] }],
      );
      setDraft('');
      request.current = null;
      follow.current = true;
      await refreshChats();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="chat-layout">
      <div className="chat-toolbar">
        <button className="chat-picker" aria-label="Выбрать чат" onClick={() => setList(true)}>
          <MessageCircle size={17} />
          <span>{current?.title || 'Новый разговор'}</span>
          <ChevronRight size={16} />
        </button>
        <button className="icon-button" aria-label="Новый чат" disabled={busy} onClick={() => void create()}>
          <Plus size={22} />
        </button>
      </div>
      <div
        className="chat-feed"
        ref={feed}
        onScroll={() => {
          const element = feed.current!;
          follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 70;
          if (follow.current) setUnread(false);
        }}
      >
        {loading ? (
          <Loading />
        ) : turns.length === 0 ? (
          <div className="chat-welcome">
            <span className="welcome-mark">
              <Leaf size={32} strokeWidth={1.4} />
            </span>
            <h1>
              Что сегодня
              <br />
              <em>к столу?</em>
            </h1>
            <p>
              Придумаем меню, найдём рецепт
              <br />
              или соберём покупки. Просто напишите.
            </p>
            <div className="suggestions">
              <button
                onClick={() => setDraft('Помоги составить план питания на три дня с учётом моей анкеты.')}
              >
                <CalendarDays size={21} />
                <span>Продумать меню</span>
                <ArrowRight size={18} />
              </button>
              <button onClick={() => setDraft('Предложи простой рецепт на ужин.')}>
                <BookOpen size={21} />
                <span>Найти идею на ужин</span>
                <ArrowRight size={18} />
              </button>
              <button onClick={() => setDraft('Помоги составить список покупок.')}>
                <ShoppingBag size={21} />
                <span>Собрать покупки</span>
                <ArrowRight size={18} />
              </button>
            </div>
          </div>
        ) : (
          turns.map((turn) => (
            <div className="turn" key={turn.turn_id}>
              <div className="user-message">{turn.text}</div>
              <div className="assistant-message">
                <div className="assistant-label">
                  <Leaf size={15} />К столу
                </div>
                {turn.output && (
                  <div className="markdown">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.output}</ReactMarkdown>
                  </div>
                )}
                {turn.artifacts.map((card) => (
                  <ArtifactCard card={card} key={`${card.kind}:${card.id}`} />
                ))}
                {isActive(turn.state) && (
                  <div className="working" role="status">
                    <span className="spinner" />
                    {turn.state === 'guarding'
                      ? 'Проверяем запрос'
                      : turn.tool
                        ? tools[turn.tool] || 'Работаем с запросом'
                        : 'Готовим ответ'}
                  </div>
                )}
                {['cancelled', 'interrupted'].includes(turn.state) && (
                  <small className="muted">Выполнение остановлено</small>
                )}
              </div>
            </div>
          ))
        )}
        <div ref={bottom} />
      </div>
      {unread && (
        <button
          className="new-messages"
          onClick={() => {
            follow.current = true;
            bottom.current?.scrollIntoView({ behavior: 'smooth' });
            setUnread(false);
          }}
        >
          <ArrowDown size={16} />К новым сообщениям
        </button>
      )}
      <div className="composer-area">
        {!list && !rename && !deleteChat && <ErrorNote text={error} />}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            aria-label="Сообщение ассистенту"
            placeholder="Что хочется приготовить?"
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (
                e.key === 'Enter' &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing &&
                window.matchMedia('(pointer: fine)').matches
              ) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {active ? (
            <button
              type="button"
              className="send stop"
              aria-label="Остановить ответ"
              onClick={() => api(`/chats/${chatId}/stop`, 'POST').catch((e) => setError(errorText(e)))}
            >
              <Square size={17} fill="currentColor" />
            </button>
          ) : (
            <button className="send" aria-label="Отправить сообщение" disabled={!draft.trim() || busy}>
              <ArrowUp size={22} />
            </button>
          )}
        </form>
      </div>
      {list && (
        <Sheet title="Ваши чаты" close={() => setList(false)}>
          <ErrorNote text={error} />
          <button className="primary full" disabled={busy} onClick={() => void create()}>
            <Plus size={18} />
            Новый чат
          </button>
          <div className="chat-list">
            {chats.map((chat) => (
              <div className="chat-list-row" key={chat.chat_id}>
                <button onClick={() => select(chat.chat_id)}>
                  {chat.chat_id === chatId ? <Check size={18} /> : <MessageCircle size={18} />}
                  <span>
                    {chat.title}
                    {Boolean(chat.active) && <small>Выполняется запрос</small>}
                  </span>
                </button>
                <button
                  className="icon-button"
                  aria-label={`Переименовать ${chat.title}`}
                  onClick={() => {
                    setRename(chat);
                    setTitle(chat.title);
                    setList(false);
                  }}
                >
                  <Pencil size={17} />
                </button>
                <button
                  className="icon-button"
                  aria-label={`Удалить ${chat.title}`}
                  onClick={() => {
                    setDeleteChat(chat);
                    setList(false);
                  }}
                >
                  <Trash2 size={17} />
                </button>
              </div>
            ))}
          </div>
        </Sheet>
      )}
      {rename && (
        <Sheet title="Название чата" close={() => setRename(null)}>
          <ErrorNote text={error} />
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api(`/chats/${rename.chat_id}`, 'PATCH', { title });
                await refreshChats();
                setRename(null);
              } catch (e) {
                setError(errorText(e));
              }
            }}
          >
            <label>
              Название
              <input required maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <button className="primary full">Сохранить</button>
          </form>
        </Sheet>
      )}
      {deleteChat && (
        <Sheet title="Удалить чат?" close={() => setDeleteChat(null)}>
          <ErrorNote text={error} />
          <p>
            Переписка исчезнет. Планы, покупки и общая память останутся. Выполняющийся запрос будет
            остановлен.
          </p>
          <button
            className="primary full"
            onClick={async () => {
              try {
                await api(`/chats/${deleteChat.chat_id}`, 'DELETE');
                const rows = await refreshChats();
                if (chatId === deleteChat.chat_id) select(rows[0]?.chat_id || '');
                setDeleteChat(null);
              } catch (e) {
                setError(errorText(e));
              }
            }}
          >
            Удалить чат
          </button>
        </Sheet>
      )}
    </div>
  );
}
