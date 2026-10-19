# Project Agent Instructions

## Startup

For development work, read every Markdown file in `docs/memory/` and the relevant rule files in `docs/standards/rules/` completely before acting. Development includes features, fixes, design and technical documentation, implementation review, tests, verification, and questions about application behavior. Read the relevant approved design, technical, and spec documents before changing application code.

Pure git operations, file listings, and simple shell or status tasks do not require loading Memory or development rules. If the task also involves implementation review or a technical decision, use the development startup procedure.

Rule index:

- `01-imports.md`: absolute Python imports.
- `02-package-manager.md`: Python dependencies and execution through `uv`.
- `03-docstrings.md`: Russian reST docstrings and type annotations.
- `04-architecture.md`: modularity, dependency injection, and appropriate async usage.
- `05-logging.md`: structured `loguru` logging with English messages.
- `06-configuration.md`: `src/app/config.py` and `pydantic-settings`.
- `07-docker.md`: project-specific Docker build, run, and verification conventions.

Do not rely on this index instead of reading the relevant rule. If required Memory files are missing, recreate them from known facts and mark unknowns explicitly before substantial development.

## Memory

Memory lives only in `docs/memory/`:

- `progress.md`: current capabilities, remaining work, and verification evidence.
- `decisions.md`: product decisions, scope, user workflows, and product-facing trade-offs.
- `known-issues.md`: defects, technical debt, and unresolved risks.

Keep Memory concise. Update it after significant progress, durable decisions, or newly discovered issues, and when requested. Read all Memory files before updating them. Do not record technical implementation choices, tooling, testing policy, or repository workflow as product decisions; use technical documents, standards, plans, or progress as appropriate.

Keep stable product knowledge in `docs/design/`, technical contracts in `docs/technical/`, feature specifications in `docs/specs/`, engineering standards in `docs/standards/`, and implementation plans in `docs/plans/`. Do not add more Memory files to duplicate that context.

## Source Of Truth And Task Size

Source-of-truth documents are approved product/design documents in `docs/design/`, approved technical documents in `docs/technical/`, and approved active specs in `docs/specs/active/`. Documents marked `Approved`, `In implementation`, or `Implemented` are authoritative for their stated scope. Completed specs in `docs/specs/completed/` apply only to relevant, still-current implemented contracts. Superseded and retired behavior does not override current decisions.

Unapproved drafts, contributor notes, templates, plans, tests, comments, and Memory do not independently establish product or technical requirements. A contributor directory under `docs/design/` does not imply approval. An explicit user request or approved plan is also authority for the requested change, including corresponding documentation updates. If source documents do not yet exist, use that request and clarify material gaps rather than inventing application scope from the repository name.

- For a clear local fix or mechanical change, implement and verify directly. A short acceptance note in the task or final report is sufficient.
- For a small, clear behavior change, update relevant acceptance criteria and tests within the authorized task; a separate spec or approval round is unnecessary.
- For substantial behavior, public contract, or architecture changes, follow `docs/standards/spec-workflow.md`. Keep decisions and acceptance criteria documented before implementation; use a separate execution plan when work spans multiple sessions or needs staged delivery.

Choose routine implementation details within the agreed scope. Ask focused questions only when material uncertainty, conflicting requirements, or an expansion of scope needs a user decision. Existing authorization covers matching design/spec updates; do not ask the user to approve the same decision again. When unclear, inspect the repository first, explain the consequential choice, and clarify it before dependent work. This procedure requires no separately installed planning skill.

Do not churn design/spec documents for formatting, mechanical changes, or dependency lock updates that do not change a documented contract. Record important new decisions where future agents can find them.

## Testing And Verification

Add or update meaningful automated tests for new application logic and bug fixes, including regression coverage where practical. Prefer deterministic checks with controlled inputs; isolate external dependencies when possible. Do not create tests that merely mirror the implementation or require live credentials for ordinary local runs.

Use the project's actual test runner and commands through `uv`. If no runner exists yet, introduce the smallest suitable test setup when application behavior first needs coverage. This Harness installation alone does not require creating an application or test framework.

Builds, lint, type checks, and Docker/API/browser/manual smoke checks complement tests. Run checks appropriate to the changed behavior, record commands and results, and state any unavailable checks or remaining gaps. Documentation-only changes need link, consistency, and diff checks rather than application tests.

## Priority And Conflicts

Current user instructions take precedence over repository guidance. This file defines the workflow; rule files define detailed engineering standards, and approved design/technical/spec documents define intended application behavior. Memory records context and does not override those sources.

Within engineering rules, use the narrowest applicable rule. Identify conflicts explicitly; if they cannot be resolved by scope or current user instructions, clarify the conflicting decision before dependent implementation. Treat obsolete documentation as a gap to resolve, not permission to silently change behavior.
