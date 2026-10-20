import sys
from functools import partial
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from app.assistant.session import Session
from app.catalog.store import Catalog
from app.config import Config, get_config
from app.web.guard import InputGuard
from app.web.lifespan import lifespan
from app.web.routes import register_routes
from app.web.storage import WebStore


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

    app = FastAPI(
        title="Ассистент питания",
        lifespan=partial(lifespan, config=config, store=store, catalog=catalog, session=session, guard=guard),
    )

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
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    register_routes(app, config, store, catalog)

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
