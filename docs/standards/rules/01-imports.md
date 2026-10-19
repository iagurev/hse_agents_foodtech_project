---
name: import_standards
description: Requires absolute Python imports and consistent import roots.
---

# Import Standards

Use absolute imports rooted in the application package or a module at the repository root. Do not use relative imports.

```python
# Prohibited
from ..utils import helper
from .models import Item

# Required pattern; replace application_package with the actual package name
from application_package.utils import helper
from application_package.models import Item
```

The shared settings module is `src/app/config.py` in the application package, so import it with `from app.config import get_config` when using the pattern in `06-configuration.md`. Do not invent a package name or modify `sys.path` to make imports work. Configure actual package installation and execution through the project's `uv` setup when application code is introduced.
