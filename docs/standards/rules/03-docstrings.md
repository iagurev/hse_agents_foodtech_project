---
name: docstring_standards
description: Requires Russian reST docstrings for documented functions, methods, and classes, with strict signature annotations.
---

# Docstring Standards

Write docstrings only for functions, methods, and classes. Do not add module-level docstrings to modules, scripts, or package `__init__.py` files.

## Language And Format

- Write docstrings in Russian using reStructuredText syntax.
- Explain what the function, method, or class does, including important edge cases when relevant.
- Document the applicable input parameters with `:param name:` and returned values with `:return:`. Omit sections that do not apply, such as parameters for a parameterless function or return values for a class description.
- Put types in signatures rather than duplicating them in docstrings.
- Keep documentation concise and avoid restating obvious implementation steps.

## Type Annotations

Annotate function and method inputs and return values with specific types; conventional `self` and `cls` parameters need no explicit annotation. Prefer built-in collection annotations and use `typing` constructs when needed. Use `Any` only when a specific type cannot reasonably describe the data, and explain why in the docstring.

```python
from typing import Any


def process_data(input_data: dict[str, Any], validate: bool = True) -> dict[str, str]:
    """Преобразует входные данные в строковые значения.

    Any используется для значений внешнего объекта с неоднородной структурой.

    :param input_data: Словарь исходных значений
    :param validate: Нужно ли проверять данные перед преобразованием
    :return: Словарь преобразованных значений
    """
    ...
```

This example illustrates documentation and typing, not an application contract or a complete implementation.
