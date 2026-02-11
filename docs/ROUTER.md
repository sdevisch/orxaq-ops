# Unified LLM Router (W3-A)

## Goal

Provide one OpenAI-compatible interface for multi-provider routing with lane-based fallback.

## Config

- Example config: `config/router.example.yaml`
- Check command:
  - `python3 -m orxaq_autonomy.cli --root . router-check --config ./config/router.example.yaml --output ./artifacts/router_check.json --strict`

## Routing Model

- `L0`: local LM Studio-first for cheapest tasks.
- `L1`: low-cost remote providers.
- `L2`: stronger remote providers for complex tasks.
- `L3`: audit-grade provider for safety/release decisions.

## Health and Fallback

- `router-check` probes each selected provider using `GET /v1/models`.
- Report: `artifacts/router_check.json`
- Required providers drive strict pass/fail (`required_down == 0`).
- Fallback order comes from `router.fallback_order`.

## Orxaq Integration

`orxaq` live LLM calls support router-mode via env vars:

- `ORXAQ_LLM_ROUTER_BASE_URL` (e.g. `http://127.0.0.1:4000/v1`)
- `ORXAQ_LLM_PROVIDER` (trace label, e.g. `router`, `openai`, `gemini`, `claude`)
