---
name: architecture_principles
description: Requires modular design, practical dependency injection, low coupling, and appropriate async usage.
---

# Architecture Standards

- Keep modules focused and dependencies explicit. Separate domain logic from external I/O when that makes the behavior easier to understand and test.
- Pass settings, clients, and other external dependencies into components that need them. Use simple constructor or function arguments before introducing a dependency injection framework.
- Keep logic independently testable with controlled inputs and replaceable external dependencies. Imports should not start network requests, services, or other runtime work.
- Add abstraction only for a concrete need. Do not introduce service layers, distributed components, or framework scaffolding merely to satisfy this rule.

## Async

Use `async`/`await` when the chosen I/O library and workload benefit from concurrency. Database, HTTP, and file operations are not automatically asynchronous: use the appropriate library interface and do not block an active event loop with synchronous I/O.

Keep straightforward synchronous flows synchronous. Do not mark CPU-bound computations as async merely for consistency. Introduce thread/process offloading only when a measured or clearly established workload requires it.

Record significant architecture decisions in the relevant approved technical document or feature spec. Routine module organization within the agreed scope does not require another approval round.
