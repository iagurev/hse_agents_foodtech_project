import asyncio
import html
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from loguru import logger
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from app.models import Product
from app.tracing import outcome, stage

CART_SKU_LIMIT = 30


class Products(Protocol):
    """Минимальный контракт поставщика товаров."""

    async def search(self, query: str, limit: int) -> list[Product]: ...
    async def analogs(self, sku_id: str, limit: int) -> list[Product]: ...
    async def cart_link(self, quantities: dict[str, float]) -> str: ...


class IntegrationError(RuntimeError):
    """Ожидаемый отказ внешнего подключения без секретных подробностей."""


def product_id(value: str | int) -> int:
    """Проверяет ID MCP без извлечения цифр из произвольной строки.

    :param value: ID товара из MCP
    :return: Числовой ID в диапазоне схемы ВкусВилла
    """
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,9}", str(value)):
        raise IntegrationError("Некорректный ID товара ВкусВилла")
    result = int(value)
    if not 1 <= result <= 999999999:
        raise IntegrationError("Некорректный ID товара ВкусВилла")
    return result


def parse_product(row: dict) -> Product:
    """Проецирует native MCP-карточку; price.current уже выражен в рублях.

    weight не используется как фасовка: сервер иногда возвращает 1 кг для штучной пачки 400 г.

    :param row: Товар из data.items или data.products ВкусВилла
    :return: Кандидат SKU; неизвестная фасовка остаётся null
    """
    sku = product_id(row.get("id"))
    title = html.unescape(row["name"]) if isinstance(row.get("name"), str) else ""
    price = row.get("price", {})
    value = price.get("current") if isinstance(price, dict) else None
    if (
        not title
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or price.get("currency") != "RUB"
    ):
        raise IntegrationError("Ответ MCP не содержит название или достоверную цену в рублях")
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*(кг|мл|шт\.?|г|л)(?![а-я])", title.lower())
    amount, unit, basis = None, None, None
    sale_unit = row.get("unit")
    if sale_unit in {"кг", "л"}:
        amount, unit = 1000, {"кг": "g", "л": "ml"}[sale_unit]
        basis = f"Единица продажи MCP: {sale_unit}"
    elif sale_unit == "шт" and len(matches) == 1 and not re.search(r"\d\s*[xх×*]\s*\d", title.lower()):
        quantity, raw_unit = matches[0]
        amount = float(quantity.replace(",", "."))
        unit = {"кг": "g", "г": "g", "мл": "ml", "л": "ml", "шт": "pcs", "шт.": "pcs"}[raw_unit]
        if raw_unit in {"кг", "л"}:
            amount *= 1000
        if amount > 0:
            basis = "Название товара"
        else:
            amount, unit = None, None
    composition = next(
        (
            p.get("value")
            for p in (row.get("properties") or [])
            if isinstance(p, dict) and p.get("name") == "Состав"
        ),
        None,
    )
    if isinstance(composition, str):
        composition = html.unescape(re.sub(r"<[^>]*>", " ", composition)).strip()
    else:
        composition = None
    return Product(
        sku_id=str(sku),
        title=title,
        price_rub=float(value),
        sale_unit={"кг": "kg", "л": "l"}.get(sale_unit),
        package_amount=amount,
        package_unit=unit,
        source_url=row.get("url"),
        composition=composition,
        package_basis=basis,
        price_source="MCP ВкусВилла",
        price_observed_at=datetime.now(UTC).isoformat(),
    )


def _tool_data(result: CallToolResult) -> dict:
    """Извлекает объект MCP, отличая пустую выдачу от ошибки протокола.

    :param result: Ответ штатного MCP SDK
    :return: Содержимое data успешного инструмента
    """
    if result.isError:
        raise IntegrationError("Инструмент MCP ВкусВилла сообщил об ошибке")
    payload = result.structuredContent
    if payload is None:
        texts = [part.text for part in result.content if isinstance(part, TextContent)]
        if len(texts) != 1:
            raise IntegrationError("MCP вернул неожиданный формат результата")
        try:
            payload = json.loads(texts[0])
        except ValueError as exc:
            raise IntegrationError("MCP вернул некорректный JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise IntegrationError("ВкусВилл не выполнил операцию")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IntegrationError("MCP не вернул данные операции")
    return data


def _http_status(exc: BaseException) -> int | None:
    """Извлекает HTTP-код из группы ошибок транспорта без содержимого ответа."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, BaseExceptionGroup):
        return next((code for item in exc.exceptions if (code := _http_status(item)) is not None), None)
    return None


def _transient(exc: BaseException) -> bool:
    """Разрешает повтор только для известных временных отказов транспорта.

    :param exc: Исходная ошибка, в том числе группа SDK
    :return: Можно ли повторить операцию в оставшемся временном бюджете
    """
    if isinstance(exc, BaseExceptionGroup):
        return all(_transient(item) for item in exc.exceptions)
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(
        exc, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException, TimeoutError)
    )


def _error_types(exc: BaseException) -> list[str]:
    """Возвращает типы вложенных ошибок без содержимого запроса или ответа."""
    if isinstance(exc, BaseExceptionGroup):
        return [name for item in exc.exceptions for name in _error_types(item)]
    return [type(exc).__name__]


class Vkusvill:
    """Прямой клиент публичного MCP ВкусВилла без авторизации и локального сервера."""

    def __init__(self, client: httpx.AsyncClient, mcp_url: str, timeout: float = 60) -> None:
        self.client, self.mcp_url, self.timeout = client, mcp_url, timeout

    async def _call(self, name: str, arguments: dict) -> dict:
        deadline = time.monotonic() + self.timeout
        for attempt in range(2):
            try:
                with stage(name, arguments | {"attempt": attempt + 1}) as span:
                    result = await self._request(name, arguments, deadline - time.monotonic())
                    outcome(span, result)
                    return result
            except IntegrationError as exc:
                if attempt or not _transient(exc.__cause__ or exc) or time.monotonic() >= deadline:
                    raise
                logger.bind(tool=name, attempt=attempt + 1).warning("Retrying transient MCP failure")
        raise AssertionError("Unreachable")

    async def _request(self, name: str, arguments: dict, timeout: float) -> dict:
        started = time.monotonic()
        try:
            # Каждый вызов владеет своей сессией; HTTP-пул общий, параллельные поиски независимы.
            async with asyncio.timeout(timeout):
                async with streamable_http_client(self.mcp_url, http_client=self.client) as (read, write, _):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=timeout)
                    ) as session:
                        await session.initialize()
                        # SDK проверяет результат по схеме; получаем её до операции создания ссылки.
                        await session.list_tools()
                        result = await session.call_tool(name, arguments)
            data = _tool_data(result)
        except Exception as exc:
            status = _http_status(exc)
            logger.warning(
                "Vkusvill MCP operation failed",
                tool=name,
                status_code=status,
                error_types=_error_types(exc),
                elapsed_seconds=time.monotonic() - started,
            )
            if isinstance(exc, IntegrationError):
                raise
            message = (
                f"MCP ВкусВилла вернул HTTP {status}"
                if status is not None
                else "Не удалось получить ответ MCP ВкусВилла"
            )
            raise IntegrationError(message) from exc
        logger.info("Vkusvill MCP operation completed", tool=name, elapsed_seconds=time.monotonic() - started)
        return data

    @staticmethod
    def _products(data: dict, key: str, limit: int) -> list[Product]:
        rows = data.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise IntegrationError("MCP не вернул список товаров")
        try:
            return [parse_product(row) for row in rows[:limit]]
        except (TypeError, ValueError) as exc:
            raise IntegrationError("MCP вернул некорректную карточку товара") from exc

    async def search(self, query: str, limit: int = 10) -> list[Product]:
        """Вызывает готовый поиск MCP, сохраняя исходный поисковый запрос.

        :param query: Буквальный запрос пользователя или агрегированного ингредиента
        :param limit: До десяти кандидатов
        :return: Товары с составом и ценой
        """
        data = await self._call(
            "vkusvill_products_search",
            {
                "q": query,
                "page": 1,
                "limit": min(limit, 10),
                "sort": "popularity",
                "vvonly": 1,
                "mode": "custom",
                "fields": ["id", "name", "price", "unit", "url", "properties"],
            },
        )
        return self._products(data, "items", limit)

    async def analogs(self, sku_id: str, limit: int = 10) -> list[Product]:
        """Вызывает готовый подбор аналогов MCP по ID найденного товара.

        :param sku_id: ID из поиска MCP
        :param limit: До десяти кандидатов
        :return: Аналоги из ответа ВкусВилла
        """
        data = await self._call("vkusvill_product_analogs", {"id": product_id(sku_id)})
        return self._products(data, "products", limit)

    async def cart_link(self, quantities: dict[str, float]) -> str:
        """Передаёт выбранные ID в готовый инструмент ссылки без оформления заказа.

        :param quantities: SKU и количество единиц продажи; весовые кг/л могут быть дробными
        :return: Ссылка ВкусВилла
        """
        if not 1 <= len(quantities) <= CART_SKU_LIMIT or any(
            type(q) not in (int, float)
            or not math.isfinite(q)
            or not 0.01 <= q <= 40
            or Decimal(str(q)) % Decimal("0.01") != 0
            for q in quantities.values()
        ):
            raise IntegrationError(
                "В одну ссылку можно включить 1–30 разных товаров и 0,01–40 единиц каждого; ссылка не создана"
            )
        data = await self._call(
            "vkusvill_cart_link_create",
            {
                "products": [{"id": product_id(sku), "q": q} for sku, q in quantities.items()],
            },
        )
        url = data.get("link")
        if not isinstance(url, str):
            raise IntegrationError("MCP не вернул ссылку корзины")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"vkusvill.ru", "www.vkusvill.ru"}
            or parsed.username
        ):
            raise IntegrationError("MCP не вернул действительную ссылку ВкусВилла")
        return url
