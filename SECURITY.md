# Security & Threat Model

1. **Untrusted Input Boundary:** All user messages and external webhook payloads are treated as untrusted data. Known instruction-override patterns are rejected deterministically before LLM planning.
2. **No Arbitrary Command Execution:** Tools are strictly whitelisted Python callables in `app/tools.py`; the model can only select a registered tool name and structured arguments.
3. **Human-in-the-Loop for State Changes:** Any tool with `mutating=True` pauses execution and emits an ephemeral confirmation code that must be confirmed via `/v1/approve`.
4. **Hard Cost Bounds:** Daily and monthly caps are enforced atomically in SQLite (`app/budget.py`) before any external API call is made.
5. **Loopback Binding:** Binds to `127.0.0.1:8081` by default.
