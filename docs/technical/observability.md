# Наблюдаемость в Langfuse

Статус: Implemented. Обновлено: 2026-10-20.

## Структура

Один новый ход веб-чата создаёт trace `chat.turn`. `session_id` равен ID чата, `user_id` — внутреннему ID владельца, `metadata.turn_id` связывает trace с сохранённым выполнением. Тег `k-stolu` отделяет приложение от других записей проекта. Повторная доставка существующего HTTP-хода возвращает сохранённое выполнение без новой трассы и модельных запросов.

```text
chat.turn
├── input_guard run
│   └── chat google/gemini-2.5-flash-lite       GENERATION
└── assistant run
    ├── chat openai/gpt-6-luna                GENERATION
    ├── generate_plan                        TOOL
    │   ├── plan.catalog                     чтение компактного каталога
    │   ├── meal_plan.generate run
    │   │   └── chat openai/gpt-6-luna        GENERATION
    │   ├── plan.validate                    расчёты и четыре проверки
    │   └── meal_plan.repair run              только при коррекции
    ├── create_cart                          TOOL
    │   ├── cart.aggregate
    │   ├── cart.search
    │   │   └── vkusvill_products_search      параллельные запросы
    │   ├── product_selection.generate run
    │   │   └── chat openai/gpt-6-luna        GENERATION
    │   ├── cart.calculate
    │   └── vkusvill_cart_link_create         одна или несколько ссылок
    └── chat openai/gpt-6-luna                итоговый ответ
```

Это пример возможной структуры, а не обязательная последовательность: обычный разговор обходится без tools; редакторы имеют свои `edit_plan`/`edit_cart` вызовы, `compaction run` появляется только при сжатии. Каждый generation хранит точные входные сообщения, инструкции, модель, параметры, ответ и usage. Длительность внешнего span включает вложенные этапы: их нельзя складывать как независимые затраты времени.

Инструменты получают нативный SDK-span, без второго декоративного span вокруг того же действия. Дополнительные spans выделяют только содержательные этапы кода и реальные обращения к MCP. HTTP, SQLite и служебные polling-запросы автоматически не трассируются.

## Как смотреть

1. В **Tracing** найдите `chat.turn` и тег `k-stolu`; фильтр по Session ID показывает все ходы одного разговора.
2. Откройте trace. Timeline показывает длительности и параллельность; дерево позволяет раскрыть инструмент, затем внутренний LLM-вызов.
3. В generation смотрите Input/Output, model parameters, tokens и cost. Полный каталог находится во внутреннем генераторе меню, а не в истории разговорного агента.
4. Для неудачного результата откройте помеченный инструмент: Output содержит структурированный результат и причины. `error`, `cancelled`, `interrupted` помечаются ERROR; `blocked`, `partial`, `needs_revision` — WARNING. Нативные исключения SDK сохраняются на соответствующем span.
5. Итоговый отчёт выполнения хранится в metadata корня. Стоимость trace равна сумме generations; отчёт не создаёт ещё одну generation и не удваивает расходы.

Фактическая `provider_details.cost` OpenRouter имеет приоритет над оценкой по тарифу. Если API не вернул стоимость, возможная оценка Langfuse не называется фактической. Input/output/total usage задаются явно: reasoning уже входит в output и не прибавляется повторно. Доступная детализация провайдера сохраняется в `metadata.usage_breakdown`. Расходы запроса, отменённого до получения usage, могут остаться неизвестными.

## Подключение

Приложение использует Langfuse Python SDK 3.x и нативную инструментацию Pydantic AI 1.68.0 с отдельным OpenTelemetry provider. Клиент создаётся один раз на процесс, exporter работает пакетно в фоне, очередь сбрасывается при штатном завершении вне event loop. Недоступный сервер трасс не останавливает агент; при аварийном завершении возможна потеря ещё не отправленных spans.

Все настройки в `src/app/config.py`, пример в `.env.example`:

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=http://127.0.0.1:3001
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_ENVIRONMENT=local
```

Для нового компьютера разверните Langfuse по [официальному Docker Compose quickstart](https://langfuse.com/self-hosting/deployment/docker-compose), создайте организацию/проект в интерфейсе и выпустите ключи проекта. У проекта нет встроенных общих паролей и ключей. На проверенном локальном стенде уже работающий Langfuse доступен на `http://127.0.0.1:3001`; существующие данные сохранены. Этот стек управляется отдельно от `compose.yaml` приложения.

В контейнере приложения loopback указывает на сам контейнер. Для Langfuse на хосте задайте `LANGFUSE_DOCKER_URL=http://host.docker.internal:3001`; Compose передаст его как `LANGFUSE_BASE_URL`. Для другого сервера укажите его адрес. Изменение `.env` требует перезапуска приложения.

При `LANGFUSE_ENABLED=false` клиент/exporter не запускаются. CLI также поддерживает трассировку, но без веб-guard. Веб-протокол идемпотентности действует до создания корневого span; CLI-повтор может оставить пустую корневую запись без повторного выполнения.

## Данные и доступ

В trace намеренно попадают тексты диалога, анкета, память, промпты и результаты tools: это нужно для разбора качества. Поэтому проект Langfuse должен оставаться закрытым, с доступом только разработчиков. Пароли, cookies и API-ключи не передаются: аргументы HTTP-запроса авторизации и Config не собираются. Удаление чата из приложения не удаляет его исторические traces; очистка/retention настраиваются отдельно в Langfuse. Публичную ссылку на traces с реальными анкетами не создавать.

## Реализация и основания

- `tracing.py`: жизненный цикл, корневой ход, этапы, статусы и точные usage/cost.
- `web/runs.py`: граница нового хода; `assistant/agent.py`, `web/guard.py`, `llm.py`: передача SDK instrumentation через зависимости.
- `assistant/tools.py`: статус существующего tool-span; `planning.py`, `shopping/service.py`, `integrations.py`: этапы операций.
- [Официальная интеграция Pydantic AI](https://langfuse.com/integrations/frameworks/pydantic-ai), [изоляция provider и расширенные настройки SDK](https://langfuse.com/docs/observability/sdk/advanced-features).

Новый оркестратор, отдельный логирующий агент и трассировочные tools не вводились. [Результаты проверки](verification.md#langfuse-анкета-и-readme--2026-10-20).
