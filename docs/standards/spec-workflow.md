# Spec Workflow

Use documentation in proportion to the work. Clear local tasks should move directly to implementation and verification; substantial features and architecture changes need documented decisions and acceptance criteria.

## Sources And Authorization

Follow the source-of-truth and priority rules in [AGENTS.md](../../AGENTS.md):

- `docs/design/`: approved product scope, workflows, and UX decisions.
- `docs/technical/`: approved architecture, data model, API, and deployment contracts.
- `docs/specs/active/`: approved active feature specifications.
- `docs/specs/completed/`: relevant, still-current implemented contracts.

Documents marked `Approved`, `In implementation`, or `Implemented` are authoritative for their scope. Contributor notes and documents without approval are drafts. Superseded or retired behavior does not override current decisions. Memory, plans, tests, and comments do not create requirements by themselves.

An explicit user request or approved plan authorizes the corresponding implementation and documentation updates. Record its reference or a short description in the spec's approval metadata. Do not request a second approval for decisions already covered. A material new choice outside that authorization requires clarification.

## Choose The Smallest Useful Workflow

| Work | Required documentation |
| --- | --- |
| Mechanical change or clear local fix | A short acceptance note in the task or final report; update existing tests as needed. |
| Small, clear behavior change | Relevant acceptance criteria and tests; update existing source documents if their contract changes. |
| Substantial feature, public contract, or architecture change | An approved spec or an update to the relevant approved design/technical document before implementation. |
| Work spanning multiple sessions or requiring staged delivery | The relevant source document plus an execution plan. |

Touching multiple files alone does not require a full spec. Pure git operations, listings, and simple shell/status tasks do not require this workflow.

## Clarify And Specify

1. Read Memory, relevant rules, and relevant source documents. Inspect the implementation before asking about discoverable facts.
2. If a material requirement is unclear, explain the choice and ask a focused question with meaningful alternatives. Continue independent work while awaiting an answer.
3. Stop questioning when the consequential decisions are settled. Do not require an external planning skill or an interview for a clear request.
4. For spec-sized work, use [the spec template](../specs/_template.md). Define the goal, boundaries, requirements, affected contracts, acceptance scenarios, and remaining risks. Omit irrelevant sections.
5. Use stable requirement IDs and a verification matrix for durable behavior according to [executable specs](executable-specs.md). Tests are a normal verification type.

A spec starts as `Proposed` if material decisions remain unapproved. If the user request or approved plan already settles them, record that authority and mark it `Approved`. Never claim approval for a new assumption that materially changes the requested outcome.

## Plan And Implement

For work requiring a separate execution plan, use [the plan template](../plans/_template.md) in `docs/plans/active/`. Link the source decisions, order the tasks by dependency, and identify acceptance checks and relevant risks. Plans describe implementation without silently adding requirements.

Implement in focused steps. Add or update meaningful tests for changed application logic and fixes. Run the applicable existing checks and planned verification, then record actual outcomes. Update verification matrix rows when evidence becomes available. Do not mark a planned command as executed.

If implementation exposes a consequential missing or conflicting decision, clarify it and update the source document before dependent work. Routine implementation choices within scope do not need another approval gate.

## Lifecycle And Closure

| Status | Meaning |
| --- | --- |
| `Proposed` | Draft with material decisions not yet approved. |
| `Approved` | Decisions are authorized and implementation may begin. |
| `In implementation` | Approved behavior is being built or verified; remaining gaps are recorded. |
| `Implemented` | Acceptance criteria are met and verification evidence is recorded. |
| `Superseded` | A linked replacement defines the current behavior. |
| `Retired` | An authorized decision intentionally removes the behavior. |

Before closing work:

- Run the planned checks or record blockers; update tests and verification evidence.
- Update Memory when durable progress, decisions, issues, or verification results changed.
- Mark the document accurately and link replacements or retirement decisions when applicable.
- Move a finished execution plan to `docs/plans/completed/`.
- Move a spec to `docs/specs/completed/` only when its lifecycle status is `Implemented`, `Superseded`, or `Retired` and all matrix rows are `covered`, `manual`, or `retired`. Any `planned` or `blocked` row keeps it active.
- If a document defines no executable behavior, record acceptance evidence without inventing a verification matrix.
- Update incoming links when moving documents.

When code and documentation disagree, determine whether the implementation is wrong, a source document is stale, or a decision is missing. Resolve that discrepancy explicitly and record durable unresolved gaps in Memory.
