# GPT Promo Grabber

Pure-HTTP code harvester. Multi-threaded, optional DataImpulse proxy,
captcha-solver-backed reCAPTCHA v2.

> by **@putrm**  |  buy access: <https://t.me/putrm>

## Features

- Pure HTTP, no headless browser. ~10-30 KB of bandwidth per run.
- Multi-thread workers with shared TCP/TLS connections.
- Optional DataImpulse rotating proxy with sticky sub-sessions per worker.
- Rich UI progress bar / summary, with a plain-text fallback.
- Online integrity check on every startup, with a background heartbeat
  while work is in progress.

## Quick start (Windows)

1. Run `run.bat`. On first launch it creates a venv, installs the
   requirements, and copies `.env.example` to `.env`.
2. Fill in `.env`:
   - `CAPSOLVER_API_KEY` - your captcha solver key.
   - `EMAIL_DOMAINS` - comma-separated list, e.g. `example.com,example.org`.
   - DataImpulse credentials if you want to proxy upstream traffic.
   - `LICENSE_KEY` is **optional**: if blank, the app prompts on first
     run and stores the token in your user state dir so later runs start
     silently.
3. Run `run.bat` again. Pick how many codes and how many parallel workers.

You can also pass flags directly:

```cmd
run.bat -n 50 -w 5
```

## Files

| Path                | Role                                       |
|---------------------|--------------------------------------------|
| `main.py`           | Entry point: worker pool, summary          |
| `license_client.py` | Signed-RPC client                          |
| `_codec.py`         | Internal byte tables                       |
| `_prompt.py`        | Interactive prompt invoked by `run.bat`    |
| `run.bat`           | Setup + launcher (single batch file)       |
| `codes.txt`         | Harvested codes, appended one per line     |
| `errors.txt`        | Error log                                  |

## Credits

- Author: [@putrm](https://t.me/putrm)
