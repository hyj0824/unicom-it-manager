# Callback Demo

FastAPI skeleton for the Rock Pi 3A operator network ledger and callback demo.
The original callback details remain in `docs/callback-demo-plan.md`; the
expanded product baselines are in:

- `docs/data-model-baseline.md`
- `docs/permission-workflow-baseline.md`
- `docs/ui-baseline.md`
- `docs/migration-plan.md`

## Run

```bash
uv sync
cp .env.example .env
# edit ADMIN_PASSWORD and SESSION_SECRET before starting
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The web app refuses to start when `ADMIN_PASSWORD` is blank or left as
`change-me`.

## Hardware smoke test

```bash
uv run python scripts/hardware_smoke.py YOUR_TEST_PHONE /path/to/audio.wav
```

The script dials through the configured A7670E serial port, waits for
`VOICE CALL: BEGIN`, plays the WAV file with `aplay`, and prints the serial log.
