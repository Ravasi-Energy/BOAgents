# BO-A01 — VAL3-01 — PROGRES

## 24.09.2026 — implementare completă predată (TESTAT_LOCAL)

- Worktree nou `boagents-val3-01` din `origin/main` integrat `e3abaf7`; ramură `bo/val3-01-a01-router`.
- Implementat `openexecutive/bo/routing/` (catalog, engine, store, serialize, observe) — motor determinist observe-only cu filtre înainte de scor.
- Integrare reală: hook fire-and-forget în `audit/usage.py:log_model_usage`; ruta reală persistată separat, neschimbată; zero apeluri provider suplimentare.
- 8 setări `bo.router.*` (tenant, CAS, audit); capabilități `routing:read`/`routing:write`; API `/bo/routing/*`.
- UI `/bo/routing` (Modele și rutare, RO, ambele teme) + nav; probe Playwright 14/14 verzi pe instanță live.
- `bo.model-observation.v1` prin adaptorul de telemetrie existent; fixture-uri din serializatorul real validate contra schemei A02; receptor-stub a primit și validat evenimentele (flush `{"sent":2,"failed":0}`).
- 68 teste țintite verzi; 3676 unit passed pe suită; ruff/mypy/next build curate; arhitectura actualizată.
- Evaluare sintetică: calibrare 12/12 la prag 0.6, test disjunct 8/8; limite documentate.
- **headSHA head-ul curent (vezi referința canonică)** → PR draft #6. Bundle `agenti/BO-A01/bo-a01-val3-01.bundle` SHA256 `vezi referința canonică`.
- Fără merge, fără deploy, fără activare; artefactul `bo.package.v1` înghețat neatins; VAL1-02/VAL2-00 separate.
