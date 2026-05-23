# GPT Promo Grabber

Pure-HTTP code harvester. Multi-threaded, optional DataImpulse proxy,
CapSolver-backed reCAPTCHA v2.

> by **@putrm**  |  buy a license: <https://t.me/putrm>

## Features

- Pure HTTP, no headless browser. ~10-30 KB of bandwidth per run.
- Multi-thread workers with shared TCP/TLS connections.
- Optional DataImpulse rotating proxy with sticky sub-sessions per worker.
- Rich UI progress bar / summary, with a plain-text fallback.
- License gate using the KISS license server
  (<https://github.com/boii/license>) with HMAC-signed responses and an
  offline grace period.

## Quick start (Windows)

1. Run `run.bat`. On first launch it creates a venv, installs the
   requirements, and copies `.env.example` to `.env`.
2. Fill in `.env`:
   - `CAPSOLVER_API_KEY` - from <https://dashboard.capsolver.com>.
   - `EMAIL_DOMAINS` - comma-separated list, e.g. `example.com,example.org`.
   - DataImpulse credentials if you want to proxy upstream traffic.
   - `LICENSE_KEY` is **optional**: if left blank, the app prompts for the
     key on first run and saves it under `~/.config/gptcode/license.key`
     so later runs start silently.
3. Run `run.bat` again. Pick how many codes and how many parallel workers.

You can also pass flags directly:

```cmd
run.bat -n 50 -w 5
```

## Files

| Path                | Role                                                      |
|---------------------|-----------------------------------------------------------|
| `main.py`           | Entry point: license check, worker pool, summary          |
| `license_client.py` | KISS license client (HMAC-verified)                       |
| `_prompt.py`        | Interactive prompt invoked by `run.bat`                   |
| `run.bat`           | Setup + launcher (single batch file)                      |
| `.env.example`      | Sample env config                                         |
| `codes.txt`         | Harvested codes, appended one per line                    |
| `errors.txt`        | Error log (ignored if `codes.txt` is what you care about) |

## License gating

The gate is enforced inside `main.py:check_license_or_exit()`. It uses the
KISS license server protocol from <https://github.com/boii/license>:

- `POST /v1/activate` on first run, `POST /v1/validate` on subsequent runs.
- The first time the app starts, it prompts for the license key and stores
  it at `~/.config/gptcode/license.key` (alongside the per-machine
  `machine.id`). Later runs read it back automatically.
- Strict mode: every startup must reach the server and pass both
  `activate` and `validate`. There is no offline grace. While work is in
  progress, a background watchdog re-validates every 60 seconds and
  halts running workers on the first failure (revoked, expired,
  signature mismatch, network error). A rejected stored key is wiped
  from disk so the next run prompts again.
- Every response is signed with HMAC-SHA256. The shared secret is baked
  into `main.py` at build time, alongside the server URL and product id.

Buy or top up a key at <https://t.me/putrm>.

## Credits

- Author: [@putrm](https://t.me/putrm)
- License server: <https://github.com/boii/license>
