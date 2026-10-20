import asyncio

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from app.assistant.tools import RecipeQuery, RecipeSearchResult
from app.catalog.store import Catalog
from app.config import Config
from app.models import Result
from app.web.contracts import (
    CartEdit,
    ChatUpdate,
    Credentials,
    MemoryUpdate,
    ObjectTitle,
    PlanEdit,
    Profile,
    PurchasedUpdate,
    Registration,
    TurnRequest,
)
from app.web.presentation import image_source, image_url, object_labels, object_view, recipe_view
from app.web.storage import WebStore

COOKIE = "meal_session"


def register_routes(app: FastAPI, config: Config, store: WebStore, catalog: Catalog) -> None:
    """Подключает HTTP-контракты к готовым зависимостям приложения.

    :param app: Приложение с состоянием исполнения
    :param config: Настройки сессий и фотографий
    :param store: Пользовательское хранилище
    :param catalog: Каталог рецептов и продуктов
    """

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
        retriever = getattr(app.state, "retriever", None)
        return {
            "status": "ok",
            "chat_enabled": app.state.chat_enabled,
            "retrieval": {
                "enabled": retriever is not None,
                "dense": retriever is not None and retriever.matrix is not None,
                "index_id": retriever.index_id if retriever else None,
            },
        }

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
        return await app.state.runs.start(uid, chat_id, body.turn_id, body.text, body.references)

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
        labels = await object_labels(store, catalog, uid)
        return [
            await object_view(store, catalog, uid, Result.model_validate(r), labels)
            for r in await store.latest(uid, "plan")
        ]

    @app.get("/api/plans/{object_id}")
    async def plan(object_id: str, uid: str = Depends(user)) -> Result:
        return await object_view(store, catalog, uid, checked(await store.get_object(uid, "plan", object_id)))

    @app.put("/api/plans/{object_id}")
    async def edit_plan(object_id: str, body: PlanEdit, uid: str = Depends(user)) -> Result:
        return await object_view(
            store, catalog, uid, checked(await app.state.editor.plan(uid, object_id, body))
        )

    @app.get("/api/carts")
    async def carts(uid: str = Depends(user)) -> list:
        labels = await object_labels(store, catalog, uid)
        return [
            await object_view(store, catalog, uid, Result.model_validate(r), labels)
            for r in await store.latest(uid, "cart")
        ]

    @app.get("/api/carts/{object_id}")
    async def cart(object_id: str, uid: str = Depends(user)) -> Result:
        return await object_view(store, catalog, uid, checked(await store.get_object(uid, "cart", object_id)))

    @app.put("/api/carts/{object_id}")
    async def edit_cart(object_id: str, body: CartEdit, uid: str = Depends(user)) -> Result:
        return await object_view(
            store, catalog, uid, checked(await app.state.editor.cart(uid, object_id, body))
        )

    @app.patch("/api/plans/{object_id}/title")
    @app.patch("/api/carts/{object_id}/title")
    async def object_title(
        object_id: str, body: ObjectTitle, request: Request, uid: str = Depends(user)
    ) -> dict:
        kind = "plan" if request.url.path.startswith("/api/plans/") else "cart"
        if not await store.set_object_metadata(uid, kind, object_id, title=body.title):
            raise HTTPException(404, "Объект не найден")
        return {"title": body.title}

    @app.post("/api/plans/{object_id}/hide")
    @app.post("/api/carts/{object_id}/hide")
    async def hide_object(object_id: str, request: Request, uid: str = Depends(user)) -> dict:
        kind = "plan" if request.url.path.startswith("/api/plans/") else "cart"
        if not await store.set_object_metadata(uid, kind, object_id, hide=True):
            raise HTTPException(404, "Объект не найден")
        return {"hidden": True}

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

    @app.post("/api/recipes/search")
    async def search_recipes(body: RecipeQuery, uid: str = Depends(user)) -> RecipeSearchResult:
        if app.state.retriever:
            result = await app.state.retriever.search(body.query, body.limits)
        else:
            result = await catalog.search(body.query, body.limits)
        response = RecipeSearchResult.from_result(result)
        for recipe in response.data.recipes:
            recipe.image_url = image_url(recipe.recipe_id, recipe.image_url)
        return response

    @app.get("/api/recipes/{recipe_id}/image")
    async def recipe_image(recipe_id: str, uid: str = Depends(user)) -> Response:
        recipes = await catalog.recipes_by_ids([recipe_id])
        recipe = recipes.get(recipe_id)
        source = image_source(recipe.image_url) if recipe else None
        if not source:
            raise HTTPException(404, "Фотография недоступна")
        try:
            async with asyncio.timeout(config.image_timeout_seconds):
                async with app.state.media_client.stream("GET", source) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}:
                        raise ValueError("Invalid image type")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > config.image_max_bytes:
                            raise ValueError("Image too large")
                        data.extend(chunk)
                    if not data:
                        raise ValueError("Empty image")
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.bind(recipe_id=recipe_id, error_type=type(exc).__name__).warning(
                "Recipe image unavailable"
            )
            raise HTTPException(502, "Не удалось загрузить фотографию") from exc
        return Response(
            bytes(data),
            media_type=mime,
            headers={"Cache-Control": f"private, max-age={config.image_cache_seconds}"},
        )

    @app.get("/api/recipes/{recipe_id}")
    async def recipe(recipe_id: str, uid: str = Depends(user)) -> dict:
        value = await recipe_view(catalog, recipe_id)
        if value is None:
            raise HTTPException(404, "Рецепт не найден")
        return value
