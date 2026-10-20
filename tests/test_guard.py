import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from app.web.guard import InputGuard


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_temporary_provider_error_uses_one_fallback_with_same_rules(status):
    calls = []

    def primary(messages, info):
        calls.append(("primary", messages, info.instructions))
        raise ModelHTTPError(status, "primary", {"message": "Unavailable"})

    def fallback(messages, info):
        calls.append(("fallback", messages, info.instructions))
        return ModelResponse(parts=[TextPart('{"allowed":true,"message":""}')])

    usage = RunUsage()
    guard = InputGuard(FunctionModel(primary), fallback_model=FunctionModel(fallback))
    assert (await guard.check("Меню", {"goal": "gain_mass"}, [], usage)).allowed
    assert [call[0] for call in calls] == ["primary", "fallback"]
    assert calls[0][1][0].parts[0].content == calls[1][1][0].parts[0].content
    assert calls[0][2] and calls[0][2] == calls[1][2]


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_permanent_provider_error_is_not_retried(status):
    def primary(messages, info):
        raise ModelHTTPError(status, "primary", {})

    def forbidden(messages, info):
        pytest.fail("Не повторять постоянный отказ")

    guard = InputGuard(FunctionModel(primary), fallback_model=FunctionModel(forbidden))
    with pytest.raises(ModelHTTPError):
        await guard.check("Меню", {}, [], RunUsage())


async def test_denied_message_is_not_sent_to_fallback():
    def primary(messages, info):
        return ModelResponse(parts=[TextPart('{"allowed":false,"message":"Отказ"}')])

    def forbidden(messages, info):
        pytest.fail("Не пересматривать запрет")

    result = await InputGuard(FunctionModel(primary), fallback_model=FunctionModel(forbidden)).check(
        "Секреты", {}, [], RunUsage()
    )
    assert not result.allowed and result.message == "Отказ"


async def test_fallback_failure_propagates_without_another_attempt():
    calls = []

    def unavailable(messages, info):
        calls.append(1)
        raise ModelHTTPError(503, "unavailable", {})

    guard = InputGuard(FunctionModel(unavailable), fallback_model=FunctionModel(unavailable))
    with pytest.raises(ModelHTTPError):
        await guard.check("Меню", {}, [], RunUsage())
    assert len(calls) == 2
