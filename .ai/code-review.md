# Code Review

## Reviewer's job

- Confirm the change matches the stated intent. No scope creep.
- Read the diff in dependency order: data → repositories → services → controllers → tests.
- Reproduce the failing case before approving the fix.
- Block on missing tests for any new branch, error path, or external boundary.

## Author's responsibilities

- One PR = one intent. Split unrelated work.
- Include the test plan and the verification output in the PR description.
- Self-review the diff before requesting a human reviewer.
- Call out compatibility shims, deferred work, and follow-up TODOs explicitly.

## Standards

- No unexplained magic numbers.
- No `TODO` without a tracking issue or follow-up commit reference.
- No commented-out code in committed changes.
- Public functions have a one-line docstring describing intent (not mechanics).
