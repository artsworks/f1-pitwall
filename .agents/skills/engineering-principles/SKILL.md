---
name: engineering-principles
description: General, repo-agnostic coding discipline for agents working on this repo. Apply before and during any code change.
---

# Engineering principles

Apply these four principles to every change. They hold for any codebase.

## 1. Think before coding

Addresses wrong assumptions, hidden confusion and missing tradeoffs.

- State your assumptions before you write code. Check each one against the code,
  the docs or the requester.
- If the request is ambiguous, ask. Do not guess.
- Name the tradeoffs of each realistic approach before you pick one.

## 2. Simplicity first

Addresses overcomplication and bloated abstractions.

- Choose the smallest, clearest solution that meets the goal.
- Do not add an abstraction, option or layer until a second real use needs it.
- Prefer existing code and libraries over new code.

## 3. Surgical changes

Addresses orthogonal edits and changes to code you should not touch.

- Make the minimal change that solves the problem.
- Do not refactor, reformat or rename unrelated code.
- If you find an unrelated problem, report it. Do not fix it in the same change.

## 4. Goal-driven execution

Addresses weak verification. Tests and clear success criteria give leverage.

- Define verifiable success criteria before you start.
- Before a fix, write or find a test that fails. After the fix, confirm that the
  test passes.
- Do not call the work done until every criterion passes.

## How this maps to this repo

`docs/25-report-analysis-prompt.md` already enforces these principles for
decision fixes. Follow its rules there: make the smallest fix, do not change
unrelated calls, and write a pytest test that fails before the fix and passes
after it.
