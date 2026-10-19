# Executable Specs

Use requirement IDs to connect durable, observable behavior to verification. They are useful for substantial feature specs and shared contracts; routine local fixes do not need a new ID or matrix.

## Requirement IDs

- Use `<AREA>-<CAPABILITY>-001`, replacing placeholders with stable descriptive names.
- Keep an ID when refining the same requirement. Give a different requirement a new ID; do not recycle retired IDs.
- Define the requirement in its approved source document and reference it from plans, tests, or verification records where useful.
- Keep IDs independent of filenames so moving a spec does not change its contracts.

## Verification Matrix

For each requirement, record a suitable automated test or other concrete verification method. One check can cover several requirements; use multiple rows if one requirement needs different checks.

| Spec ID | Requirement | Verification Type | Command/Procedure | Evidence/Artifact | Status |
| --- | --- | --- | --- | --- | --- |
| `<AREA>-<CAPABILITY>-001` | <Observable behavior> | unit/integration/contract/build/lint/typecheck/Docker/API/browser/manual | <Exact command or repeatable steps> | <Test path and actual result, or evidence reference> | planned |

Examples and placeholders are not execution evidence. Replace them when implementing a spec. Record commands and results in the matrix or a linked plan/progress entry; do not commit transient logs or screenshots unless they are useful project artifacts.

| Row status | Meaning |
| --- | --- |
| `planned` | Verification is identified but has not successfully run. |
| `covered` | An automated test or executable check ran successfully; its command and actual result are recorded. |
| `manual` | A documented manual check was performed; its procedure and actual result are recorded. |
| `blocked` | Verification could not complete; record the reason and remaining work. |
| `retired` | The requirement is intentionally inactive; link or describe the authorized decision. |

Prefer deterministic regression tests for application logic and bug fixes. Builds and smoke checks cover different risks and do not by themselves demonstrate every functional requirement. Use manual evidence when appropriate, explain important coverage limitations, and never reclassify an unperformed check as manual just to close a spec.

Only `covered`, `manual`, and `retired` are completed row statuses. Follow [the spec workflow](spec-workflow.md) for lifecycle transitions and archiving; unresolved rows keep the spec active.
