# Git workflow

Use these rules for branches, commits, and pull requests.

## Branch names

Use lowercase names with letters, numbers, hyphens, underscores, and slashes.

Recommended patterns:

- `feat/<description>`
- `fix/<description>`
- `docs/<description>`
- `ops/<description>`
- `refactor/<description>`

## Commits

Commit subjects should:

- Use imperative mood, such as `Add invite audit log`
- Start with a capital letter
- Stay under 72 characters when practical
- Omit trailing periods
- Avoid emojis

Add a body when the reason is not obvious from the diff. Explain why the change
exists, not a line-by-line summary.

## Pull requests

Pull requests should include:

- Summary
- Testing
- Deployment or migration notes
- Rollback notes for runtime changes
- Screenshots for visible UI changes
- Loaded `.ai` modules
- Domain review notes for database, security, runtime, UI, or dependency risk

Keep pull requests focused. Split broad work into stacked pull requests when
review would become hard.

## Review discipline

- Do not approve your own pull request.
- Do not squash feedback commits until the branch is ready to merge.
- Use full external URLs for issues or source references.
- Use relative paths for files in this repository.
