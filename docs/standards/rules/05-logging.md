---
name: logging_standards
description: Requires structured loguru logging with English messages and standard exception handling.
---

# Logging Standards

Use `loguru` for application logs. Write log messages in English and attach variable context as structured fields rather than interpolating it into message text. Log meaningful operation boundaries and failures; avoid adding a log to every small function.

```python
from loguru import logger

logger.bind(task_id="example").info("Operation completed", duration_ms=12)
```

## Configuration

Configure sinks once at application startup. Keep logger setup in a clearly named application module when needed; do not create a root `logging.py` that shadows Python's standard library.

Use Loguru's built-in serialization for JSON output. The following standalone configuration example writes structured records to stderr, including extra fields and exception details:

```python
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", serialize=True, diagnose=False)
```

In application setup, obtain the log level from the shared settings. A readable local sink is acceptable if it retains the relevant structured context. Do not introduce a custom JSON or traceback serializer without a concrete project requirement.

## Levels And Exceptions

| Level | Purpose |
| --- | --- |
| `DEBUG` | Detailed diagnostics for investigation. |
| `INFO` | Meaningful normal operation events. |
| `SUCCESS` | Significant successful completion. |
| `WARNING` | Recoverable problem or fallback. |
| `ERROR` | Failure of the current operation. |
| `CRITICAL` | Failure preventing the application from continuing. |

Inside an exception handler, use `logger.exception("Operation failed", task_id=task_id)` to retain traceback context, or `logger.opt(exception=exc).warning("Operation recovered", task_id=task_id)` when an explicit exception and a different level are appropriate. Do not log the same failure repeatedly at every layer or suppress it just because it was logged.

Use `logger.bind(...)` for a component or operation. Where concurrent operations need automatic correlation, use a scoped `with logger.contextualize(...)` block. Request middleware, custom context variables, and tracing infrastructure are needed only when actual application behavior calls for them.

Do not log tokens, passwords, full settings objects, or raw payloads containing secrets. Keep production exception diagnostics from exposing local variable values.
