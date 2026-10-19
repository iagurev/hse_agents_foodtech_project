import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  BookOpen,
  CalendarDays,
  Leaf,
  MessageCircle,
  Settings as SettingsIcon,
  ShoppingBag,
} from 'lucide-react';
import '@fontsource/manrope/400.css';
import '@fontsource/manrope/500.css';
import '@fontsource/manrope/600.css';
import '@fontsource/manrope/700.css';
import '@fontsource/manrope/800.css';
import { api, ApiError, errorText, go } from './api';
import type { Me } from './api';
import { Auth, Settings } from './Profile';
import { ChatScreen } from './Chat';
import { Collections } from './Collections';
import { CartDetail, PlanDetail, RecipeDetail } from './Details';
import { ErrorNote, Loading } from './ui';
import './styles.css';
import './forms.css';
import './chat.css';
import './objects.css';

function App() {
  const [me, setMe] = useState<Me | null>();
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [route, setRoute] = useState(location.hash.slice(1) || '/chat');
  async function load() {
    setError('');
    try {
      setMe(await api<Me>('/me'));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setMe(null);
      else setError(errorText(e));
    }
  }
  useEffect(() => {
    void load();
    const changed = () => setRoute(location.hash.slice(1) || '/chat');
    addEventListener('hashchange', changed);
    return () => removeEventListener('hashchange', changed);
  }, []);
  useEffect(() => {
    const viewport = window.visualViewport;
    const update = () => {
      document.documentElement.style.setProperty('--app-height', `${viewport?.height ?? innerHeight}px`);
    };
    update();
    viewport?.addEventListener('resize', update);
    return () => viewport?.removeEventListener('resize', update);
  }, []);
  function discuss(text: string) {
    setDraft(text);
    go('/chat');
  }
  const [section, id] = route.slice(1).split('/');
  const detail = ['plan', 'cart', 'recipe', 'settings'].includes(section);
  const active =
    section === 'plan' ? 'plans' : section === 'cart' ? 'carts' : section === 'recipe' ? 'recipes' : section;
  return (
    <div className="app-shell">
      {me === undefined ? (
        <>
          <Loading />
          <ErrorNote text={error} />
          {error && (
            <button className="secondary" onClick={() => void load()}>
              Повторить
            </button>
          )}
        </>
      ) : !me ? (
        <Auth
          done={(user) => {
            setMe(user);
            go('/chat');
          }}
        />
      ) : (
        <>
          {!detail && (
            <header className="app-header">
              <a className="brand" href="#/chat">
                <Leaf size={25} strokeWidth={1.7} />
                <span>к столу</span>
              </a>
              <button className="icon-button" aria-label="Настройки" onClick={() => go('/settings')}>
                <SettingsIcon size={21} />
              </button>
            </header>
          )}
          <main className={`app-main ${section === 'chat' || !section ? 'chat-main' : ''}`} key={me.user_id}>
            {(section === 'chat' || !section) && (
              <ChatScreen userId={me.user_id} draft={draft} setDraft={setDraft} />
            )}
            {['plans', 'recipes', 'carts'].includes(section) && (
              <Collections kind={section as 'plans' | 'recipes' | 'carts'} discuss={discuss} />
            )}
            {section === 'plan' && id && <PlanDetail key={id} id={id} discuss={discuss} />}
            {section === 'recipe' && id && <RecipeDetail key={id} id={id} discuss={discuss} />}
            {section === 'cart' && id && <CartDetail key={id} id={id} discuss={discuss} />}
            {section === 'settings' && (
              <Settings
                me={me}
                update={setMe}
                logout={() => {
                  setMe(null);
                  setDraft('');
                  go('/chat');
                }}
              />
            )}
          </main>
          <nav className="bottom-nav" aria-label="Основная навигация">
            {[
              { key: 'chat', label: 'Чат', icon: MessageCircle },
              { key: 'plans', label: 'Планы', icon: CalendarDays },
              { key: 'recipes', label: 'Рецепты', icon: BookOpen },
              { key: 'carts', label: 'Покупки', icon: ShoppingBag },
            ].map((tab) => (
              <a
                key={tab.key}
                className={active === tab.key ? 'active' : ''}
                href={`#/${tab.key}`}
                aria-current={active === tab.key ? 'page' : undefined}
              >
                <span>
                  <tab.icon size={22} strokeWidth={active === tab.key ? 2 : 1.6} />
                </span>
                {tab.label}
              </a>
            ))}
          </nav>
        </>
      )}
    </div>
  );
}
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
