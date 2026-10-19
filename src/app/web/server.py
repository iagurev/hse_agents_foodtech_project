import asyncio
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from loguru import logger
from openai import AsyncOpenAI
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from app.assistant.agent import build_agent, build_model
from app.assistant.session import Session
from app.assistant.tokens import GLMTokenCounter
from app.catalog import Catalog
from app.config import Config, get_config
from app.integrations import Vkusvill
from app.llm import ModelGateway
from app.models import Result
from app.web.contracts import (
    CartEdit,
    ChatUpdate,
    Credentials,
    MemoryUpdate,
    PlanEdit,
    Profile,
    PurchasedUpdate,
    Registration,
    TurnRequest,
)
from app.web.guard import InputGuard
from app.web.objects import ObjectEditor
from app.web.presentation import cart_view, recipe_view
from app.web.runs import Runs
from app.web.storage import WebStore

COOKIE = "meal_session"


def create_app(
    config: Config | None = None,
    *,
    store: WebStore | None = None,
    catalog: Catalog | None = None,
    session: Session | None = None,
    guard: InputGuard | None = None,
) -> FastAPI:
    """Собирает приложение; зависимости можно подменить в контролируемых проверках.

    :param config: Настройки запуска
    :param store: Необязательное хранилище проверки
    :param catalog: Необязательный готовый каталог для контролируемых проверок
    :param session: Необязательный готовый harness
    :param guard: Необязательная проверка входа
    :return: ASGI-приложение
    """
    config = config or get_config()
    store = store or WebStore(config.database_path)
    catalog = catalog or Catalog(
        config.recipes_database_path, config.products_database_path, config.sources_database_path
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await catalog.validate()
        await store.initialize()
        async with (
            httpx.AsyncClient() as client,
            httpx.AsyncClient(timeout=config.integration_timeout_seconds) as product_client,
        ):
            active_session, active_guard = session, guard
            if active_session is None:
                model = build_model(config, client)
                counter = await GLMTokenCounter.load(model, config.tokenizer_cache_path, client)
                active_session = Session(
                    config,
                    build_agent(model),
                    store,
                    catalog,
                    Vkusvill(
                        product_client,
                        config.vkusvill_mcp_url,
                        config.integration_timeout_seconds,
                    ),
                    ModelGateway(
                        model,
                        config.request_limit,
                        planning_model=build_model(config, client, planning=True),
                        planning_effort=config.planning_reasoning_effort,
                    ),
                    counter,
                )
            if active_guard is None:
                guard_client = AsyncOpenAI(
                    api_key=config.openrouter_api_key.get_secret_value(),
                    base_url=config.openrouter_base_url,
                    http_client=client,
                    max_retries=0,
                    timeout=config.guard_timeout_seconds,
                )
                guard_model = OpenRouterModel(
                    config.guard_model,
                    provider=OpenRouterProvider(openai_client=guard_client),
                    settings={"openrouter_usage": {"include": True}},
                )
                active_guard = InputGuard(guard_model)
            app.state.runs = Runs(config, store, active_session, active_guard)
            app.state.editor = ObjectEditor(store, catalog, active_session.shopper)
            yield
            await app.state.runs.close()

    app = FastAPI(title="Ассистент питания", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and (
                urlsplit(origin).scheme != request.url.scheme
                or urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def user(request: Request) -> str:
        return await store.session_user(request.cookies.get(COOKIE))

    async def login_response(user_id: str, response: Response, request: Request) -> dict:
        token = await store.issue_session(user_id, config.session_days)
        response.set_cookie(
            COOKIE,
            token,
            max_age=config.session_days * 86400,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        return await store.me(user_id)

    def checked(result: Result) -> Result:
        if result.error and result.error.code in {"NOT_FOUND", "VERSION_CONFLICT"}:
            raise HTTPException(409 if result.error.code == "VERSION_CONFLICT" else 404, result.error.message)
        return result

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/auth/register")
    async def register(body: Registration, response: Response, request: Request) -> dict:
        return await login_response(await store.register(body), response, request)

    @app.post("/api/auth/login")
    async def login(body: Credentials, response: Response, request: Request) -> dict:
        return await login_response(await store.authenticate(body.login, body.password), response, request)

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response, uid: str = Depends(user)) -> dict:
        await store.logout(request.cookies[COOKIE])
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(uid: str = Depends(user)) -> dict:
        return await store.me(uid)

    @app.put("/api/profile")
    async def profile(body: Profile, uid: str = Depends(user)) -> dict:
        await store.profile(uid, body)
        return await store.me(uid)

    @app.put("/api/memory")
    async def memory(body: MemoryUpdate, uid: str = Depends(user)) -> Response:
        result = await store.update_memory(uid, body.revision, body.text)
        return JSONResponse(
            result.model_dump(mode="json"), status_code=409 if result.status == "error" else 200
        )

    @app.get("/api/chats")
    async def chats(uid: str = Depends(user)) -> list:
        return await store.chats(uid)

    @app.post("/api/chats")
    async def new_chat(uid: str = Depends(user)) -> dict:
        return await store.create_chat(uid)

    @app.patch("/api/chats/{chat_id}")
    async def rename(chat_id: str, body: ChatUpdate, uid: str = Depends(user)) -> dict:
        await store.chat(uid, chat_id)
        if not body.title.strip():
            raise HTTPException(422, "Название не должно быть пустым")
        await store.rename_chat(chat_id, body.title.strip())
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def delete(chat_id: str, uid: str = Depends(user)) -> dict:
        await store.chat(uid, chat_id)
        async with app.state.runs.locks.setdefault(chat_id, asyncio.Lock()):
            await app.state.runs.cancel(chat_id)
            await store.delete_chat(chat_id)
            app.state.runs.live.pop(chat_id, None)
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/messages")
    async def messages(chat_id: str, uid: str = Depends(user)) -> list:
        await store.chat(uid, chat_id)
        return await app.state.runs.messages(chat_id)

    @app.post("/api/chats/{chat_id}/turns")
    async def turn(chat_id: str, body: TurnRequest, uid: str = Depends(user)) -> dict:
        return await app.state.runs.start(uid, chat_id, body.turn_id, body.text)

    @app.post("/api/chats/{chat_id}/stop")
    async def stop(chat_id: str, uid: str = Depends(user)) -> dict:
        await store.chat(uid, chat_id)
        await app.state.runs.cancel(chat_id)
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/turns/{turn_id}/events")
    async def events(chat_id: str, turn_id: str, uid: str = Depends(user)) -> StreamingResponse:
        await store.chat(uid, chat_id)
        return StreamingResponse(
            app.state.runs.events(chat_id, turn_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/plans")
    async def plans(uid: str = Depends(user)) -> list:
        return await store.latest(uid, "plan")

    @app.get("/api/plans/{object_id}")
    async def plan(object_id: str, uid: str = Depends(user)) -> Result:
        return checked(await store.get_object(uid, "plan", object_id))

    @app.put("/api/plans/{object_id}")
    async def edit_plan(object_id: str, body: PlanEdit, uid: str = Depends(user)) -> Result:
        return checked(await app.state.editor.plan(uid, object_id, body))

    @app.get("/api/carts")
    async def carts(uid: str = Depends(user)) -> list:
        return await store.latest(uid, "cart")

    @app.get("/api/carts/{object_id}")
    async def cart(object_id: str, uid: str = Depends(user)) -> Result:
        return await cart_view(catalog, checked(await store.get_object(uid, "cart", object_id)))

    @app.put("/api/carts/{object_id}")
    async def edit_cart(object_id: str, body: CartEdit, uid: str = Depends(user)) -> Result:
        return checked(await app.state.editor.cart(uid, object_id, body))

    @app.get("/api/carts/{object_id}/purchased")
    async def marks(object_id: str, uid: str = Depends(user)) -> dict:
        return {"purchased": await app.state.editor.marks(uid, object_id)}

    @app.put("/api/carts/{object_id}/purchased")
    async def mark(object_id: str, body: PurchasedUpdate, uid: str = Depends(user)) -> dict:
        return await app.state.editor.mark(uid, object_id, body.sku_id, body.purchased)

    @app.get("/api/recipes")
    async def recipes(uid: str = Depends(user)) -> list:
        saved = await store.latest(uid, "plan")
        ids = list(dict.fromkeys(m["recipe_id"] for p in saved for m in p["data"]["meals"]))
        return [view for recipe_id in ids if (view := await recipe_view(catalog, recipe_id)) is not None]

    @app.get("/api/recipes/{recipe_id}")
    async def recipe(recipe_id: str, uid: str = Depends(user)) -> dict:
        value = await recipe_view(catalog, recipe_id)
        if value is None:
            raise HTTPException(404, "Рецепт не найден")
        return value

    @app.get("/{path:path}")
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "Маршрут не найден")
        root = config.frontend_path.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if not target.is_file():
            target = root / "index.html"
        if not target.is_file():
            raise HTTPException(503, "Соберите frontend: pnpm --dir frontend build")
        return FileResponse(target)

    return app


def main() -> None:
    """Запускает локальное приложение одним процессом."""
    config = get_config()
    logger.remove()
    logger.add(sys.stderr, level=config.log_level, serialize=True, diagnose=False)
    uvicorn.run(create_app(config), host=config.web_host, port=config.web_port)
