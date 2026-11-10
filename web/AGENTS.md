# Web Agent Notes

Before editing frontend code, read [`.ai/AGENTS.md`](../.ai/AGENTS.md), then load:

- `.ai/code-style.md`
- `.ai/testing.md`
- `.ai/security.md` when fetching authenticated endpoints

Frontend code lives in `web/` (Next.js app router). Use the service layer
in `web/service/` for all API calls. Component tests live in `web/__tests__/`.
