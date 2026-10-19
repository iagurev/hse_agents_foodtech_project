# Plan: <Feature Or Workflow>

Use for work spanning multiple sessions or needing staged delivery. Replace placeholders, omit irrelevant sections, and adjust relative links when saving in `active/` or `completed/`.

## Metadata

- Status: <planned / in progress / blocked / completed>
- Created: YYYY-MM-DD
- Last updated: YYYY-MM-DD
- Source decisions: <links to actual approved design/technical/spec documents and the authorizing user request or approved plan>

This plan implements the linked decisions. If a consequential new product or technical choice falls outside their authorization, clarify it and update the relevant source document before dependent work. Routine implementation details do not require a separate approval gate.

## Goal And Scope

Describe the intended result and important boundaries in one short paragraph.

## Source Requirements

| Spec ID / Source Section | Implementation Outcome | Verification |
| --- | --- | --- |
| `<SPEC-ID or section>` | <Observable result> | <Test, command, or manual procedure> |

## Implementation Phases

### Phase 1: <Name>

- Work: <Steps in dependency order>
- Expected areas: <Relevant modules or documents>
- Verification: <Tests and other applicable checks>
- Risks/recovery: <Only when relevant>

### Phase 2: <Name>

- Work: <Steps in dependency order>
- Expected areas: <Relevant modules or documents>
- Verification: <Tests and other applicable checks>
- Risks/recovery: <Only when relevant>

## Tasks

- [ ] <Task description>
  - Source: <Requirement ID or source section>
  - Acceptance: <Observable condition>
  - Areas: <Likely modules or documents>
  - Tests: <Meaningful new/updated coverage, or why application tests do not apply>
  - Other verification: <Applicable build, lint, type, container, or manual check>

## Verification Results

Record each command or procedure, its actual result, and any remaining coverage gap. Link automated tests and durable evidence where useful. A planned check is not a successful check.

## Closure

- [ ] Implementation matches the authorized source decisions.
- [ ] Relevant tests and other planned checks ran; blockers and gaps are recorded.
- [ ] Verification matrix rows are updated where a matrix exists.
- [ ] Memory reflects significant progress, decisions, issues, and evidence.
- [ ] Completed plans/specs are archived according to [the spec workflow](../standards/spec-workflow.md), with incoming links updated.

## Change Log

- YYYY-MM-DD: Created.
