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

## Profiles (W3-D)

- Profiles directory: `profiles/`
  - `local.yaml`
  - `lan.yaml`
  - `travel.yaml`
- Apply a profile:
  - `python3 -m orxaq_autonomy.cli --root . profile-apply local --config ./config/router.example.yaml --profiles-dir ./profiles --output ./config/router.active.yaml`
- Run router checks using a profile directly:
  - `python3 -m orxaq_autonomy.cli --root . router-check --config ./config/router.example.yaml --profile travel --profiles-dir ./profiles --active-config ./config/router.active.yaml --output ./artifacts/router_check.json --strict`

## Orxaq Integration

`orxaq` live LLM calls support router-mode via env vars:

- `ORXAQ_LLM_ROUTER_BASE_URL` (e.g. `http://127.0.0.1:4000/v1`)
- `ORXAQ_LLM_PROVIDER` (trace label, e.g. `router`, `openai`, `gemini`, `claude`)
