# A2A Pons runtime intake status — 2026-06-04T19:59Z

## Intake result

Pons runtime/evidence bundle was exported over SSH from `root@10.1.1.120` using the approved 1Password `Cortex/Main` SSH key. No runtime services were restarted and no Valkey streams/config were mutated.

Local archive, outside the repo:

```text
/home/synapse/audits/a2a-recovery-artifacts/a2a-pons-runtime-export-20260604T195940Z.tar.gz
```

SHA-256:

```text
7a1d4d84c36321dd1bb06cb13a6dad9fa5b8c42967753a113aee876c0a71a3fa
```

Validation summary committed here:

```text
docs/a2a/source-trace/pons-runtime-20260604T195940Z-summary.json
```

## Reproduced Pons failing state

The bundle includes Pons runtime test output showing the expected failure class:

```text
8 failed, 48 passed
AttributeError: 'APIServerAdapter' object has no attribute '_handle_a2a_consult'
```

The failures are all in `tests/gateway/test_a2a_consult_wrapper.py` and point to missing API route glue for `/v1/a2a/consult`.

## Recovery applied on this branch

This branch now adds:

- `tools/a2a_consult_tool.py`
- `tests/tools/test_a2a_consult_tool.py`
- `tests/gateway/test_a2a_consult_wrapper.py`
- `tests/gateway/test_a2a_valkey_no_sqlite.py`
- `APIServerAdapter._handle_a2a_consult`
- native route registration for `POST /v1/a2a/consult`

The route is a wrapper over target `/v1/runs`, validates/redacts locally, maps malformed payloads to stable rejection contracts, and requires API auth when the API server has a key configured.

## Verification

```text
/home/synapse/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/gateway/test_api_server.py \
  tests/gateway/test_api_server_runs.py \
  tests/gateway/test_api_server_jobs.py \
  tests/gateway/test_a2a*.py \
  tests/tools/test_a2a_consult_tool.py -q

293 passed, 162 warnings in 10.91s
```

Warnings are existing aiohttp `NotAppKeyWarning` test fixture warnings.

## Runtime hold

This is source recovery only. No gateway restart, live route rollout, Valkey cleanup, firewall/listener change, or Discord/agent-comms wiring was performed.
