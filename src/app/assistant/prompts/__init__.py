from importlib.resources import files
from typing import Literal

PromptName = Literal[
    "agent_system_prompt",
    "meal_plan",
    "edit_plan",
    "edit_cart",
    "product_selection",
    "compact",
    "input_guard",
]


def load_prompt(name: PromptName) -> str:
    """Читает Markdown-ресурс установленного пакета.

    :param name: Имя известного промпта без расширения
    :return: Текст инструкций
    """
    return files("app.assistant.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
