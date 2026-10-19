import { useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowLeft, ArrowRight, Check, Eye, EyeOff, Leaf, LogOut } from 'lucide-react';
import { api, ApiError, budgetLabel, budgets, difficulties, errorText, goals } from './api';
import type { Me, Profile } from './api';
import { BackHeader, ErrorNote, Sheet } from './ui';

export function ProfileForm({
  value,
  submit,
  busy,
  button = 'Сохранить изменения',
}: {
  value?: Profile;
  submit: (value: Profile) => void;
  busy: boolean;
  button?: string;
}) {
  const [budget, setBudget] = useState(value?.monthly_budget ?? '');
  const [difficulty, setDifficulty] = useState(value?.cooking_difficulty ?? '');
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    submit({
      goal: data.get('goal') as Profile['goal'],
      sex: data.get('sex') as Profile['sex'],
      age: Number(data.get('age')),
      height_cm: Number(data.get('height')),
      weight_kg: Number(data.get('weight')),
      monthly_budget: budget || null,
      cooking_difficulty: (difficulty as Profile['cooking_difficulty']) || null,
      restrictions: String(data.get('restrictions')),
    });
  }
  return (
    <form onSubmit={save} className="profile-form">
      <label>
        Ваша цель
        <select aria-label="Ваша цель" name="goal" defaultValue={value?.goal ?? ''} required>
          <option value="" disabled>
            Выберите, что вам ближе
          </option>
          {Object.entries(goals).map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <div className="form-row">
        <label>
          Пол
          <select aria-label="Пол" name="sex" defaultValue={value?.sex ?? ''} required>
            <option value="" disabled>
              Выберите
            </option>
            <option value="female">Женский</option>
            <option value="male">Мужской</option>
          </select>
        </label>
        <label>
          Возраст
          <input
            name="age"
            type="number"
            inputMode="numeric"
            min="14"
            max="120"
            required
            defaultValue={value?.age}
            placeholder="Лет"
          />
        </label>
      </div>
      <div className="form-row">
        <label>
          Рост, см
          <input
            name="height"
            type="number"
            inputMode="decimal"
            min="150"
            max="230"
            step="any"
            required
            defaultValue={value?.height_cm}
            placeholder="150–230"
          />
        </label>
        <label>
          Вес, кг
          <input
            name="weight"
            type="number"
            inputMode="decimal"
            min="50"
            max="200"
            step="any"
            required
            defaultValue={value?.weight_kg}
            placeholder="50–200"
          />
        </label>
      </div>
      <fieldset>
        <legend>
          Бюджет на еду в месяц<span>Необязательно · на одного человека</span>
        </legend>
        <div className="choices">
          {budgets.map((b) => (
            <button
              type="button"
              key={b}
              aria-pressed={budget === b}
              className={budget === b ? 'choice selected' : 'choice'}
              onClick={() => setBudget(budget === b ? '' : b)}
            >
              {budget === b && <Check size={15} />} {budgetLabel(b)}
            </button>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>
          Сложность приготовления<span>Необязательно · что вам комфортно готовить</span>
        </legend>
        <div className="choices">
          {Object.entries(difficulties).map(([id, name]) => (
            <button
              type="button"
              key={id}
              aria-pressed={difficulty === id}
              className={difficulty === id ? 'choice selected' : 'choice'}
              onClick={() => setDifficulty(difficulty === id ? '' : id)}
            >
              {name}
            </button>
          ))}
        </div>
      </fieldset>
      <label>
        Предпочтения и ограничения
        <span className="field-hint">Необязательно · аллергии, любимые продукты, особенности питания</span>
        <textarea
          name="restrictions"
          defaultValue={value?.restrictions}
          rows={3}
          placeholder="Например, не ем рыбу и люблю тёплые завтраки"
        />
      </label>
      <button className="primary full" disabled={busy}>
        {busy ? 'Сохраняем…' : button}
        <ArrowRight size={18} />
      </button>
    </form>
  );
}

export function Auth({ done }: { done: (me: Me) => void }) {
  const [mode, setMode] = useState<'register' | 'login'>('register');
  const [step, setStep] = useState(0);
  const [credentials, setCredentials] = useState({ login: '', password: '' });
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function send(profile?: Profile) {
    setBusy(true);
    setError('');
    try {
      done(await api<Me>(`/auth/${mode}`, 'POST', { ...credentials, ...(profile ? { profile } : {}) }));
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) setStep(0);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-page">
      <div className="brand">
        <Leaf size={26} strokeWidth={1.7} />
        <span>к столу</span>
      </div>
      {step === 0 ? (
        <>
          <div className="auth-intro">
            <h1>
              Питание,
              <br />
              которое подходит
              <br />
              <em>именно вам.</em>
            </h1>
            <p>
              От «что приготовить?»
              <br />
              до плана и списка покупок.
            </p>
          </div>
          <div className="segmented">
            <button
              className={mode === 'register' ? 'selected' : ''}
              onClick={() => {
                setMode('register');
                setError('');
              }}
            >
              Регистрация
            </button>
            <button
              className={mode === 'login' ? 'selected' : ''}
              onClick={() => {
                setMode('login');
                setError('');
              }}
            >
              Вход
            </button>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (mode === 'login') void send();
              else {
                setError('');
                setStep(1);
              }
            }}
            className="auth-form"
          >
            <label>
              Логин
              <input
                autoComplete="username"
                required
                minLength={1}
                maxLength={64}
                value={credentials.login}
                onChange={(e) => setCredentials({ ...credentials, login: e.target.value })}
                placeholder="Как вас найти"
                autoCapitalize="none"
              />
            </label>
            <label>
              Пароль
              <div className="password-field">
                <input
                  aria-label="Пароль"
                  required
                  type={show ? 'text' : 'password'}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  value={credentials.password}
                  onChange={(e) => setCredentials({ ...credentials, password: e.target.value })}
                  placeholder="Ваш пароль"
                />
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => setShow(!show)}
                  aria-label={show ? 'Скрыть пароль' : 'Показать пароль'}
                >
                  {show ? <EyeOff size={20} /> : <Eye size={20} />}
                </button>
              </div>
            </label>
            <ErrorNote text={error} />
            <button className="primary full" disabled={busy}>
              {busy ? 'Входим…' : mode === 'login' ? 'Войти' : 'Продолжить'}
              <ArrowRight size={18} />
            </button>
          </form>
          <p className="auth-footnote">
            Меньше решений на каждый день.
            <br />
            Больше удовольствия от еды.
          </p>
        </>
      ) : (
        <>
          <button className="text-button" onClick={() => setStep(0)}>
            <ArrowLeft size={18} />
            Назад
          </button>
          <div className="section-intro">
            <h1>Давайте знакомиться</h1>
            <p>Несколько деталей помогут подобрать питание под вас. Всё можно изменить позже.</p>
          </div>
          <ErrorNote text={error} />
          <ProfileForm busy={busy} submit={(p) => void send(p)} button="Начать общение" />
        </>
      )}
    </main>
  );
}

export function Settings({ me, update, logout }: { me: Me; update: (m: Me) => void; logout: () => void }) {
  const [panel, setPanel] = useState<'profile' | 'memory'>('profile');
  const [memory, setMemory] = useState(me.memory);
  const [revision, setRevision] = useState(me.memory_revision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [clear, setClear] = useState(false);
  async function saveMemory(text: string) {
    setBusy(true);
    setError('');
    setSaved('');
    try {
      await api('/memory', 'PUT', { text, revision });
      const next = await api<Me>('/me');
      update(next);
      setMemory(next.memory);
      setRevision(next.memory_revision);
      setSaved('Память сохранена');
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) {
        const next = await api<Me>('/me');
        update(next);
        setRevision(next.memory_revision);
        setError(
          `${errorText(e)} Актуальная память: ${next.memory || 'пуста'}. Проверьте свой текст перед повторным сохранением.`,
        );
      }
    } finally {
      setBusy(false);
      setClear(false);
    }
  }
  return (
    <>
      <BackHeader title="Настройки" back="/chat" />
      <div className="page-content">
        <div className="account-row">
          <div className="avatar">{me.login.slice(0, 1).toUpperCase()}</div>
          <div>
            <h2>{me.login}</h2>
            <p>Ваш профиль</p>
          </div>
        </div>
        <div className="segmented">
          <button
            className={panel === 'profile' ? 'selected' : ''}
            onClick={() => {
              setPanel('profile');
              setError('');
              setSaved('');
            }}
          >
            Анкета
          </button>
          <button
            className={panel === 'memory' ? 'selected' : ''}
            onClick={() => {
              setPanel('memory');
              setError('');
              setSaved('');
            }}
          >
            Память
          </button>
        </div>
        <ErrorNote text={error} />
        {saved && (
          <p className="success" role="status">
            <Check size={16} />
            {saved}
          </p>
        )}
        {panel === 'profile' ? (
          <ProfileForm
            value={me.profile}
            busy={busy}
            submit={async (p) => {
              setBusy(true);
              setError('');
              setSaved('');
              try {
                update(await api<Me>('/profile', 'PUT', p));
                setSaved('Анкета сохранена');
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          />
        ) : (
          <div className="memory-form">
            <h2>Что ассистент помнит о вас</h2>
            <p>
              Общая память для всех чатов. Здесь остаются устойчивые предпочтения, а не разовые пожелания.
            </p>
            <label>
              Текст памяти
              <textarea
                rows={10}
                value={memory}
                onChange={(e) => setMemory(e.target.value)}
                placeholder="Пока память пуста"
              />
            </label>
            <button className="primary full" disabled={busy} onClick={() => void saveMemory(memory)}>
              {busy ? 'Сохраняем…' : 'Сохранить память'}
            </button>
            <button className="text-button danger" disabled={busy || !memory} onClick={() => setClear(true)}>
              Очистить память
            </button>
          </div>
        )}
        <button
          className="logout"
          onClick={async () => {
            try {
              await api('/auth/logout', 'POST');
              logout();
            } catch (e) {
              setError(errorText(e));
            }
          }}
        >
          <LogOut size={19} />
          Выйти из аккаунта
        </button>
        {clear && (
          <Sheet title="Очистить память?" close={() => setClear(false)}>
            <p>Ассистент забудет сохранённые сведения. Анкета и переписки останутся.</p>
            <button className="primary full" disabled={busy} onClick={() => void saveMemory('')}>
              Очистить
            </button>
          </Sheet>
        )}
      </div>
    </>
  );
}
