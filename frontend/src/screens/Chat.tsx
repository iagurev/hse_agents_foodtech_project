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
import { api, errorText, go, isActive } from '../api';
import type { Chat, Turn, ChatDraft, ObjectReference } from '../api';
import { ErrorNote, Loading, Sheet } from '../components/ui';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArtifactCard, RecipePicker, References } from '../components/ChatCards';
import { OperationStatus, ResponseTime } from '../components/ResponseStatus';

export function ChatScreen({
  userId,
  selectedChat,
  draft: composerDraft,
  setDraft: updateDraft,
}: {
  userId: string;
  selectedChat: string | null;
  draft: ChatDraft;
  setDraft: (draft: ChatDraft) => void;
}) {
  const draft = composerDraft.text;
  const setDraft = (text: string) => updateDraft({ ...composerDraft, text });
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatId, setChatId] = useState(selectedChat || localStorage.getItem(`chat:${userId}`) || '');
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
  const restored = useRef('');
  const submitting = useRef(false);
  const current = chats.find((c) => c.chat_id === chatId);
  const active = turns.find((t) => isActive(t.state));
  const refreshChats = useCallback(async () => {
    const rows = await api<Chat[]>('/chats');
    setChats(rows);
    return rows;
  }, []);
  const select = useCallback(
    (id: string) => {
      if (id !== chatId) {
        setTurns([]);
        setLoading(Boolean(id));
        restored.current = '';
      }
      setChatId(id);
      go(id ? `/chat?chat=${encodeURIComponent(id)}` : '/chat');
      localStorage.setItem(`chat:${userId}`, id);
      setList(false);
      setError('');
      follow.current = true;
    },
    [userId, chatId],
  );
  useEffect(() => {
    if (selectedChat && selectedChat !== chatId) select(selectedChat);
  }, [selectedChat]);
  useEffect(() => {
    let alive = true;
    refreshChats()
      .then((rows) => {
        if (!alive) return;
        if (!rows.some((c) => c.chat_id === chatId)) select(rows[0]?.chat_id || '');
        else go(`/chat?chat=${encodeURIComponent(chatId)}`);
        if (!rows.length) setLoading(false);
      })
      .catch((e) => {
        if (alive) {
          setError(errorText(e));
          setLoading(false);
        }
      });
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
      .then((rows) => alive && setTurns((current) => (current.length ? current : rows)))
      .catch((e) => alive && setError(errorText(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [chatId]);
  useEffect(() => {
    if (!active || !chatId) return;
    const id = active.turn_id;
    let listening = true;
    const events = new EventSource(`/api/chats/${chatId}/turns/${id}/events`);
    events.onmessage = (event) => {
      const update = JSON.parse(event.data) as Turn;
      setTurns((rows) => rows.map((row) => (row.turn_id === id ? { ...row, ...update } : row)));
      if (!isActive(update.state)) {
        events.close();
        api<Turn[]>(`/chats/${chatId}/messages`)
          .then((saved) => {
            if (!listening) return;
            setTurns((current) => [
              ...saved,
              ...current.filter((row) => !saved.some((item) => item.turn_id === row.turn_id)),
            ]);
          })
          .catch((e) => setError(errorText(e)));
        void refreshChats();
      }
    };
    return () => {
      listening = false;
      events.close();
    };
  }, [active?.turn_id, chatId, refreshChats]);
  useEffect(() => {
    if (loading) return;
    if (restored.current !== chatId && feed.current) {
      restored.current = chatId;
      const saved = sessionStorage.getItem(`chat-scroll:${userId}:${chatId}`);
      if (saved !== null) {
        feed.current.scrollTop = Number(saved);
        follow.current = feed.current.scrollHeight - feed.current.scrollTop - feed.current.clientHeight < 70;
        return;
      }
    }
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
  async function send(instruction?: string, selectedReferences: ObjectReference[] = []) {
    const text = (instruction ?? draft).trim();
    const references = instruction === undefined ? composerDraft.references : selectedReferences;
    if (!text || active || busy || loading || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError('');
    try {
      let id = chatId;
      if (!id) {
        const chat = await api<Chat>('/chats', 'POST');
        id = chat.chat_id;
        select(id);
      }
      const key = `pending:${userId}:${id}`;
      let pending: { id: string; text: string; references: ObjectReference[] } | null = null;
      try {
        pending = JSON.parse(localStorage.getItem(key) || 'null');
      } catch {
        /* No pending request. */
      }
      if (
        !pending ||
        pending.text !== text ||
        JSON.stringify(pending.references) !== JSON.stringify(references)
      )
        pending = { id: crypto.randomUUID(), text, references };
      localStorage.setItem(key, JSON.stringify(pending));
      const result = await api<{ turn_id: string; state: string }>(`/chats/${id}/turns`, 'POST', {
        turn_id: pending.id,
        text,
        references: references.map(({ title: _title, ...reference }) => reference),
      });
      setTurns((rows) =>
        rows.some((t) => t.turn_id === result.turn_id)
          ? rows
          : [...rows, { ...result, text, references, output: '', artifacts: [], elapsed_seconds: 0 }],
      );
      if (instruction === undefined) updateDraft({ text: '', references: [] });
      localStorage.removeItem(key);
      follow.current = true;
      void refreshChats().catch((e) => setError(errorText(e)));
    } catch (e) {
      setError(errorText(e));
    } finally {
      submitting.current = false;
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
          if (loading || restored.current !== chatId) return;
          sessionStorage.setItem(`chat-scroll:${userId}:${chatId}`, String(element.scrollTop));
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
                onClick={() =>
                  updateDraft({
                    text: 'Помоги составить план питания на три дня с учётом моей анкеты.',
                    references: [],
                  })
                }
              >
                <CalendarDays size={21} />
                <span>Продумать меню</span>
                <ArrowRight size={18} />
              </button>
              <button
                onClick={() => updateDraft({ text: 'Предложи простой рецепт на ужин.', references: [] })}
              >
                <BookOpen size={21} />
                <span>Найти идею на ужин</span>
                <ArrowRight size={18} />
              </button>
              <button
                onClick={() => updateDraft({ text: 'Помоги составить список покупок.', references: [] })}
              >
                <ShoppingBag size={21} />
                <span>Собрать покупки</span>
                <ArrowRight size={18} />
              </button>
            </div>
          </div>
        ) : (
          turns.map((turn) => (
            <div className="turn" key={turn.turn_id}>
              <div className="user-message">
                {turn.text}
                <References values={turn.references || []} />
              </div>
              <div className="assistant-message">
                <div className="assistant-label">
                  <Leaf size={15} />К столу
                  <ResponseTime turn={turn} />
                </div>
                {turn.output && (
                  <div className="markdown">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.output}</ReactMarkdown>
                  </div>
                )}
                {turn.artifacts
                  .filter((card) => !card.selectable)
                  .map((card) => (
                    <ArtifactCard card={card} key={`${card.kind}:${card.id}`} />
                  ))}
                {turn.artifacts.some((c) => c.selectable) && (
                  <RecipePicker
                    key={`${chatId}:${turn.turn_id}`}
                    cards={turn.artifacts.filter((c) => c.selectable)}
                    storageKey={`recipes:${userId}:${chatId}:${turn.turn_id}`}
                    disabled={Boolean(active) || busy}
                    send={send}
                  />
                )}
                {isActive(turn.state) && <OperationStatus turn={turn} />}
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
        <References
          values={composerDraft.references}
          remove={(index) =>
            updateDraft({
              ...composerDraft,
              references: composerDraft.references.filter((_, i) => i !== index),
            })
          }
        />
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
            <button
              className="send"
              aria-label="Отправить сообщение"
              disabled={!draft.trim() || busy || loading}
            >
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
