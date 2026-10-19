# Spec: <Feature Or Workflow>

Replace placeholders and omit irrelevant sections. Follow [the spec workflow](../standards/spec-workflow.md); when saving in `active/` or `completed/`, update relative links for that location.

## Metadata

- Status: Proposed
- Owner: <author or maintainer>
- Created: YYYY-MM-DD
- Last updated: YYYY-MM-DD
- Approval: <user request or approved plan covering these decisions; pending if unapproved>
- Related documents: <links to actual relevant design, technical, or spec documents>
- Scope: <one-sentence boundary>

This spec becomes authoritative for its scope when approved. A draft does not override current approved behavior. The original user request or approved plan is sufficient authorization when it already settles the decisions documented here.

## Objective And Boundaries

Describe the outcome, why it matters, how success is observed, and what is outside this change.

## Users And Workflow

Describe affected users or systems and their observable steps, where relevant.

## Requirements

Use stable IDs for durable, observable behavior according to [executable specs](../standards/executable-specs.md).

| Spec ID | Requirement |
| --- | --- |
| `<AREA>-<CAPABILITY>-001` | <Observable behavior or invariant> |

## Affected Contracts

Describe relevant API, data, configuration, output/export, UX, or deployment changes and compatibility constraints. Link actual technical documents as needed; omit areas this feature does not affect.

## Acceptance Scenarios

- <Main successful outcome>
- <Important boundary or invalid input>
- <Failure or recovery scenario, if relevant>

## Risks And Open Questions

| Item | Status | Decision Or Remaining Work |
| --- | --- | --- |
| <Risk or question> | open/resolved | <Notes or authority for the decision> |

## Verification Matrix

Use automated tests for suitable behavior and regression coverage. Add builds, lint, type checks, container checks, or manual verification for the risks they cover. If this document defines no executable behavior, record appropriate acceptance evidence instead of a matrix.

| Spec ID | Requirement | Verification Type | Command/Procedure | Evidence/Artifact | Status |
| --- | --- | --- | --- | --- | --- |
| `<AREA>-<CAPABILITY>-001` | <Short requirement> | unit/integration/contract/build/lint/typecheck/Docker/API/browser/manual | <Exact command or steps> | <Test path and actual result, or evidence reference; pending initially> | planned |

Valid row statuses are `planned`, `covered`, `manual`, `blocked`, and `retired`. Record actual evidence before marking a row completed. Keep the spec active while any row is planned or blocked.

## Change Log

- YYYY-MM-DD: Created as `Proposed`; record approval and implementation transitions as they occur.
