import asyncio
import json

import httpx
import pytest

from app.integrations.vkusvill import IntegrationError, Vkusvill, parse_product


@pytest.fixture
def native_product() -> dict:
    return {
        "id": 185,
        "name": "Творог 5%, 400&nbsp;г",
        "unit": "шт",
        "price": {"current": 203, "currency": "RUB"},
        "weight": {"value": 1, "unit": "кг"},
        "url": "https://vkusvill.ru/goods/tvorog-5-185/",
        "properties": [{"name": "Состав", "value": "молоко;<br>закваска"}],
    }


class MCPServer:
    """Контролируемый MCP-транспорт; SDK и сериализация остаются настоящими."""

    def __init__(self, data: dict, *, tool_error: bool = False, structured: bool = False) -> None:
        self.data, self.tool_error, self.structured = data, tool_error, structured
        self.calls: list[dict] = []
        self.status = 200
        self.delay = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp"
        assert "authorization" not in request.headers
        if self.status != 200:
            return httpx.Response(self.status)
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in (
                        "vkusvill_products_search",
                        "vkusvill_product_analogs",
                        "vkusvill_cart_link_create",
                    )
                ]
            }
        else:
            assert body["method"] == "tools/call"
            self.calls.append(body["params"])
            if self.delay:
                await asyncio.sleep(self.delay)
            result = {"isError": self.tool_error, "content": []}
            if self.structured:
                result["structuredContent"] = self.data
            else:
                result["content"] = [{"type": "text", "text": json.dumps(self.data)}]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


async def test_native_search_analogs_and_link_contracts(native_product):
    server = MCPServer({"ok": True, "data": {"items": [native_product]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        vv = Vkusvill(client, "https://mcp.vkusvill.ru/mcp")
        rows = await vv.search("творог без сахара", 10)
        assert rows[0].sku_id == "185" and rows[0].price_rub == 203
        assert rows[0].package_amount == 400  # Не ошибочные 1 кг из weight.
        assert rows[0].composition == "молоко; закваска"
        call = server.calls[-1]
        assert call["name"] == "vkusvill_products_search"
        assert call["arguments"]["q"] == "творог без сахара"
        assert call["arguments"]["limit"] == 10
        assert "properties" in call["arguments"]["fields"]
        server.data = {"ok": True, "data": {"products": [native_product] * 20}}
        assert len(await vv.analogs("185")) == 10
        assert server.calls[-1] == {"name": "vkusvill_product_analogs", "arguments": {"id": 185}}
        server.data = {"ok": True, "data": {"link": "https://vkusvill.ru/mobile?basket&share=123"}}
        assert await vv.cart_link({"185": 2}) == server.data["data"]["link"]
        assert server.calls[-1] == {
            "name": "vkusvill_cart_link_create",
            "arguments": {"products": [{"id": 185, "q": 2}]},
        }
        assert not client.is_closed


@pytest.mark.parametrize("structured", [False, True])
async def test_empty_search_and_parallel_sessions(structured):
    server = MCPServer({"ok": True, "data": {"items": []}}, structured=structured)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        vv = Vkusvill(client, "https://mcp.vkusvill.ru/mcp")
        assert await asyncio.gather(vv.search("яблоки"), vv.search("хлеб")) == [[], []]
        assert len(server.calls) == 2
        assert not client.is_closed


@pytest.mark.parametrize(
    "data,tool_error",
    [
        ({"ok": False, "error": "failure"}, False),
        ({"ok": True, "data": {}}, False),
        ({"ok": True, "data": {"items": []}}, True),
    ],
)
async def test_mcp_errors_are_not_empty_search(data, tool_error):
    server = MCPServer(data, tool_error=tool_error)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(IntegrationError):
            await Vkusvill(client, "https://mcp.vkusvill.ru/mcp").search("творог")
    assert len(server.calls) == 1


async def test_http_error_is_safe_and_not_retried():
    server = MCPServer({})
    server.status = 403
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(IntegrationError, match="HTTP 403"):
            await Vkusvill(client, "https://mcp.vkusvill.ru/mcp").search("творог")
    assert not server.calls


async def test_timeout_and_cancellation_do_not_retry():
    server = MCPServer({"ok": True, "data": {"items": []}})
    server.delay = 5
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(IntegrationError):
            await Vkusvill(client, "https://mcp.vkusvill.ru/mcp", timeout=0.1).search("творог")
        assert len(server.calls) == 1
        task = asyncio.create_task(Vkusvill(client, "https://mcp.vkusvill.ru/mcp").search("яблоки"))
        while len(server.calls) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(server.calls) == 2 and not client.is_closed


@pytest.mark.parametrize(
    "quantities",
    [
        {},
        {"185": 41},
        {"185": 0},
        {"185": True},
        {"185": 0.001},
        {"185": float("nan")},
        {"sku185": 1},
        {"0": 1},
        {str(i): 1 for i in range(1, 32)},
    ],
)
async def test_invalid_cart_is_rejected_without_network(quantities):
    server = MCPServer({})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(IntegrationError):
            await Vkusvill(client, "https://mcp.vkusvill.ru/mcp").cart_link(quantities)
    assert not server.calls


@pytest.mark.parametrize("link", [None, "https://vkusvill.ru.attacker.test/cart", "http://vkusvill.ru/cart"])
async def test_invalid_link_is_not_published(link):
    server = MCPServer({"ok": True, "data": {"link": link}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(IntegrationError):
            await Vkusvill(client, "https://mcp.vkusvill.ru/mcp").cart_link({"185": 1})


def test_native_prices_are_rubles_and_units_are_sale_units(native_product):
    native_product["price"]["current"] = 1499.9
    assert parse_product(native_product).price_rub == 1499.9
    native_product.update(name="Яблоки", unit="кг")
    parsed = parse_product(native_product)
    assert (parsed.package_amount, parsed.package_unit) == (1000, "g")
    native_product.update(name="Творог", unit="шт")
    assert parse_product(native_product).package_amount is None


@pytest.mark.parametrize("price", [None, "203", True, -1, float("nan")])
def test_missing_or_invalid_price_is_not_zero(native_product, price):
    native_product["price"]["current"] = price
    with pytest.raises(IntegrationError):
        parse_product(native_product)


async def test_link_transmits_fractional_weight_without_integer_rounding():
    server = MCPServer({"ok": True, "data": {"link": "https://vkusvill.ru/mobile?basket&share=test"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as client:
        await Vkusvill(client, "https://mcp.vkusvill.ru/mcp").cart_link({"1066": 0.25, "185": 2})
    assert server.calls[-1]["arguments"]["products"] == [{"id": 1066, "q": 0.25}, {"id": 185, "q": 2}]
