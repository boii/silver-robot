"""GPT Promo Grabber: pure-HTTP code harvester.

Flow:
  1. License check against the project's licensing service.
  2. Solve reCAPTCHA via CapSolver (sitekey + URL hardcoded, base64).
  3. POST email + token to the upstream promo endpoint.
  4. Parse `code` from the JSON response, append to codes.txt.

Bandwidth ~10-30 KB per run.
Total time ~CapSolver time + a couple seconds of HTTP overhead.

Author : @putrm   (https://t.me/putrm)
Buy    : https://t.me/putrm
"""

import argparse
import base64
import json
import os
import pathlib
import random
import re
import string
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

# Rich UI (graceful fallback when the library is not installed)
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import (
        Progress,
        SpinnerColumn,
        BarColumn,
        TextColumn,
        TimeElapsedColumn,
        MofNCompleteColumn,
    )
    from rich.text import Text
    from rich.align import Align
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

console = Console() if _HAS_RICH else None

# License client. See license_client.py.
from license_client import LicenseClient, LicenseError

# Credits / contact (kept as constants so the banner stays in sync)
APP_NAME = "GPT Promo Grabber"
AUTHOR_HANDLE = "@putrm"
CONTACT_URL = "https://t.me/putrm"

# License config baked into the build. End-users only supply LICENSE_KEY in
# their .env. URL / signing key / product name are hardcoded so they can't
# be redirected to a fake server. Do NOT rotate LICENSE_SIGNING_KEY after
# release; every existing client would reject server responses.
LICENSE_API_URL = "https://license.kin.my.id"
LICENSE_SIGNING_KEY = "46fb461acea1f62c4dcb1c0ee74c131dd45c2db54e4f21007d113237c1b6f548"
PRODUCT_ID = "gptcode"

# Persistent storage for the activated key (lives next to machine.id under
# the user's config dir, NOT in .env, so it survives even when users
# overwrite their .env from .env.example).
LICENSE_CONFIG_DIR = pathlib.Path.home() / ".config" / PRODUCT_ID
LICENSE_KEY_FILE = LICENSE_CONFIG_DIR / "license.key"

# Strict mode: revalidate against the server every N seconds while the app
# is doing real work. If a re-check fails (revoked, expired, network down,
# signature mismatch...), running workers bail out on their next iteration.
LICENSE_RECHECK_SECONDS = 60

# Endpoints discovered during the original network capture are stored here as
# base64. Not real encryption: the goal is just to keep the source clean of
# obvious branded strings so casual greps don't match. Decoded at startup.
_E = base64.b64decode

SUBMIT_URL = _E(
    "aHR0cHM6Ly93d3cuYmJ2YWRlc2N1ZW50b3MubXgvYWRtaW4tc2l0ZS9waHAvX2h0dHByZXF1ZXN0LnBocA=="
).decode()
RECAPTCHA_SITE_URL = _E(
    "aHR0cHM6Ly93d3cuYmJ2YWRlc2N1ZW50b3MubXgvZGV2ZWxvcC9vcGVuYWktM21zYw=="
).decode()
RECAPTCHA_REFERER_ORIGIN = _E("aHR0cHM6Ly93d3cuYmJ2YWRlc2N1ZW50b3MubXg=").decode()
RECAPTCHA_SITE_KEY = "6LfG0tIsAAAAAINTtPyFHgumY1_U11qbAxQuzh7O"

CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

CODES_FILE = "codes.txt"
ERROR_LOG_FILE = "errors.txt"

# Lock for cross-thread file writes.
_FILE_LOCK = threading.Lock()

# Per-thread HTTP sessions to reuse TCP/TLS connections across requests.
# Each worker gets:
#   - session_direct  : direct calls (CapSolver API). Never proxied.
#   - session_proxied : calls to the target site. Routed through DataImpulse.
# Goal: TLS handshake once per host, all subsequent requests reuse the socket.
_THREAD_LOCAL = threading.local()


def _make_session():
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def _get_direct_session():
    """Session for CapSolver calls (never proxied)."""
    sess = getattr(_THREAD_LOCAL, "session_direct", None)
    if sess is None:
        sess = _make_session()
        _THREAD_LOCAL.session_direct = sess
    return sess


def _get_proxied_session(proxy_config):
    """Session for target-site calls. Routed through proxy when given.

    Sessions are cached by (host, port, username) so a per-run sticky session
    spawns a fresh connection, while runs sharing the same proxy identity
    keep reusing the existing one.
    """
    if not proxy_config:
        return _get_direct_session()

    cache = getattr(_THREAD_LOCAL, "proxied_sessions", None)
    if cache is None:
        cache = {}
        _THREAD_LOCAL.proxied_sessions = cache

    key = (proxy_config["host"], proxy_config["port"], proxy_config["username"])
    sess = cache.get(key)
    if sess is None:
        sess = _make_session()
        sess.proxies = _requests_proxies(proxy_config)
        cache[key] = sess
    return sess


# Default DataImpulse gateway
DATAIMPULSE_DEFAULT_HOST = "gw.dataimpulse.com"
DATAIMPULSE_DEFAULT_PORT = 823

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def log_error(message, exc=None):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    if console is not None:
        console.print(f"[bold red]ERROR[/bold red] {message}")
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    try:
        with _FILE_LOCK, open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if exc is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                f.write(tb)
                if not tb.endswith("\n"):
                    f.write("\n")
    except OSError as write_exc:
        print(f"WARN: failed to write error log: {write_exc}", file=sys.stderr)


def log_warn(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] WARN {message}"
    if console is not None:
        console.print(f"[yellow]WARN[/yellow] {message}")
    else:
        print(f"WARN: {message}", file=sys.stderr)
    try:
        with _FILE_LOCK, open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def print_banner():
    """Print the credit banner shown at startup."""
    if console is None:
        print(f"=== {APP_NAME} ===")
        print(f"by {AUTHOR_HANDLE}  |  buy: {CONTACT_URL}")
        return

    banner = Text()
    banner.append(APP_NAME, style="bold cyan")
    banner.append("\nPure HTTP + multi-thread + DataImpulse + CapSolver", style="dim")
    banner.append(f"\nby {AUTHOR_HANDLE}  ", style="bold white")
    banner.append(f"|  buy: {CONTACT_URL}", style="bold green")
    console.print(Panel(Align.center(banner), border_style="cyan", padding=(0, 2)))


def _load_saved_key():
    """Read the saved license key from disk. Returns None if not yet stored."""
    try:
        if LICENSE_KEY_FILE.exists():
            key = LICENSE_KEY_FILE.read_text(encoding="utf-8").strip()
            return key or None
    except OSError:
        pass
    return None


def _save_key(key):
    """Persist the key under the user's config dir, mode 600 where supported."""
    try:
        LICENSE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LICENSE_KEY_FILE.write_text(key.strip() + "\n", encoding="utf-8")
        # Best-effort tighten perms (no-op on Windows but harmless).
        try:
            os.chmod(LICENSE_KEY_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log_warn(f"Could not persist license key to {LICENSE_KEY_FILE}: {exc}")


def _clear_saved_key():
    """Remove a stored key, e.g. after the server tells us it's invalid."""
    try:
        if LICENSE_KEY_FILE.exists():
            LICENSE_KEY_FILE.unlink()
    except OSError:
        pass


def _prompt_for_key():
    """Interactively ask the user for a license key. Returns the trimmed key."""
    if not sys.stdin.isatty():
        log_error(
            "No license key on file and no interactive terminal to ask for one. "
            f"Set LICENSE_KEY in .env or run interactively. Buy a key at {CONTACT_URL}"
        )
        sys.exit(2)

    if console is not None:
        console.print()
        console.print(
            Panel(
                Text.from_markup(
                    "[bold]No license key on file.[/bold]\n"
                    f"Buy or top up at [bold green]{CONTACT_URL}[/bold green]"
                ),
                title="[bold yellow]License activation[/bold yellow]",
                border_style="yellow",
                padding=(0, 1),
            )
        )
    else:
        print()
        print("=== License activation ===")
        print(f"Buy or top up at {CONTACT_URL}")

    while True:
        try:
            entered = input("Enter your license key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if entered:
            return entered
        print("Key cannot be empty.")


def check_license_or_exit():
    """Validate the license before doing any real work.

    Strict mode: the server MUST be reachable on every startup and MUST
    return valid=True. There is no offline grace, and a stored key that
    the server later rejects is wiped immediately.

    Lookup order for the key:
      1. LICENSE_KEY in .env (override)
      2. license.key file under the user's config dir
      3. Interactive prompt (TTY only)

    Side effect: on success, sets the module-level _LICENSE_CLIENT and
    _LICENSE_KEY so background re-checks can keep validating.
    """
    global _LICENSE_CLIENT, _LICENSE_KEY

    client = LicenseClient(
        api_url=LICENSE_API_URL,
        signing_key=LICENSE_SIGNING_KEY,
        product=PRODUCT_ID,
        # Strict mode does not use offline grace.
        offline_grace_days=0,
    )

    env_key = (os.getenv("LICENSE_KEY") or "").strip()
    saved_key = _load_saved_key()
    key = env_key or saved_key
    fresh_input = False

    if not key:
        key = _prompt_for_key()
        fresh_input = True

    while True:
        # 1) Bind / refresh activation. activate is idempotent on the
        #    server for the same (key, machine_id), so calling it on every
        #    start is fine and gives us a fresh signed payload.
        try:
            res = client.activate(key)
        except LicenseError as exc:
            log_error(f"License signature mismatch: {exc}")
            _clear_saved_key()
            sys.exit(2)
        except requests.RequestException as exc:
            # Strict: no offline grace. Refuse to start.
            log_error(
                f"License server unreachable: {exc}. "
                "Strict mode requires an online check on every startup."
            )
            sys.exit(2)

        if not res.get("valid"):
            status = res.get("status", "invalid")
            if not fresh_input and sys.stdin.isatty():
                log_warn(
                    f"Stored license key was rejected (status={status}). Asking again."
                )
                _clear_saved_key()
                saved_key = None
                key = _prompt_for_key()
                fresh_input = True
                continue
            log_error(
                f"License rejected (status={status}). Need help? Contact {CONTACT_URL}"
            )
            _clear_saved_key()
            sys.exit(2)

        # 2) Belt-and-braces: confirm the server still says this machine
        #    is good with a separate validate call. Catches the (rare)
        #    case where activation succeeds but the slot is already
        #    flagged elsewhere.
        try:
            chk = client.validate(key)
        except LicenseError as exc:
            log_error(f"License signature mismatch on validate: {exc}")
            _clear_saved_key()
            sys.exit(2)
        except requests.RequestException as exc:
            log_error(f"License server unreachable on validate: {exc}.")
            sys.exit(2)

        if not chk.get("valid"):
            status = chk.get("status", "invalid")
            log_error(
                f"License validate failed (status={status}). Contact {CONTACT_URL}"
            )
            _clear_saved_key()
            sys.exit(2)

        # 3) Persist and report.
        if not saved_key or saved_key != key:
            _save_key(key)

        _LICENSE_CLIENT = client
        _LICENSE_KEY = key

        if console is not None:
            info = chk.get("license") or res.get("license") or {}
            product_name = info.get("product", PRODUCT_ID)
            expires = info.get("expires_at")
            expires_str = (
                time.strftime("%Y-%m-%d", time.localtime(expires))
                if isinstance(expires, (int, float)) and expires
                else "lifetime"
            )
            console.print(
                f"[green]License OK[/green] [dim]({product_name}, expires {expires_str}, strict mode)[/dim]"
            )
        return


# === Strict-mode runtime re-checking ===

_LICENSE_CLIENT: "LicenseClient | None" = None
_LICENSE_KEY: "str | None" = None
_LICENSE_REVOKED = threading.Event()
_LICENSE_RECHECK_THREAD: "threading.Thread | None" = None
_LICENSE_RECHECK_STOP = threading.Event()


def license_is_revoked():
    """Return True if a background re-check has flagged the license bad."""
    return _LICENSE_REVOKED.is_set()


def _license_recheck_loop():
    """Periodically validate the license while the app is doing real work.

    Trips _LICENSE_REVOKED on the first failure so worker threads can bail.
    """
    if _LICENSE_CLIENT is None or not _LICENSE_KEY:
        return

    while not _LICENSE_RECHECK_STOP.wait(LICENSE_RECHECK_SECONDS):
        try:
            res = _LICENSE_CLIENT.validate(_LICENSE_KEY)
        except LicenseError:
            log_error("License signature mismatch during re-check. Halting.")
            _LICENSE_REVOKED.set()
            return
        except requests.RequestException as exc:
            log_error(
                f"License server unreachable during re-check: {exc}. "
                "Halting (strict mode has no offline grace)."
            )
            _LICENSE_REVOKED.set()
            return

        if not res.get("valid"):
            status = res.get("status", "invalid")
            log_error(
                f"License revoked mid-run (status={status}). Halting workers."
            )
            _clear_saved_key()
            _LICENSE_REVOKED.set()
            return


def start_license_watchdog():
    """Spin up the background re-check thread (daemon)."""
    global _LICENSE_RECHECK_THREAD
    if _LICENSE_CLIENT is None or _LICENSE_RECHECK_THREAD is not None:
        return
    _LICENSE_RECHECK_STOP.clear()
    _LICENSE_REVOKED.clear()
    t = threading.Thread(
        target=_license_recheck_loop,
        name="license-watchdog",
        daemon=True,
    )
    t.start()
    _LICENSE_RECHECK_THREAD = t


def stop_license_watchdog():
    """Signal the watchdog to exit. Called when work is finished."""
    global _LICENSE_RECHECK_THREAD
    _LICENSE_RECHECK_STOP.set()
    if _LICENSE_RECHECK_THREAD is not None:
        _LICENSE_RECHECK_THREAD.join(timeout=2)
        _LICENSE_RECHECK_THREAD = None


def main():
    args = parse_args()
    load_dotenv()

    print_banner()
    check_license_or_exit()

    domains = parse_domains(os.getenv("EMAIL_DOMAINS"))
    capsolver_api_key = (os.getenv("CAPSOLVER_API_KEY") or "").strip()
    capsolver_timeout = int(os.getenv("CAPSOLVER_TIMEOUT", "180"))

    if not domains:
        log_error("EMAIL_DOMAINS is not set in .env")
        sys.exit(1)
    if not capsolver_api_key:
        log_error("CAPSOLVER_API_KEY is not set in .env")
        sys.exit(1)

    proxy_config = build_proxy_config()

    runs = max(1, args.runs)
    workers = max(1, min(args.workers, runs))

    # Show the config table
    if console is not None:
        cfg = Table(show_header=False, box=None, padding=(0, 1))
        cfg.add_column(style="cyan")
        cfg.add_column(style="white")
        cfg.add_row("Runs", str(runs))
        cfg.add_row("Workers", str(workers))
        cfg.add_row("Domains", ", ".join(domains))
        cfg.add_row("Proxy", proxy_config["server"] if proxy_config else "[dim]disabled[/dim]")
        cfg.add_row("CapSolver timeout", f"{capsolver_timeout}s")
        console.print(cfg)
        console.print()
    else:
        print(f"Runs={runs} Workers={workers} Proxy={'on' if proxy_config else 'off'}")

    t_start = time.time()
    results = []  # list of dicts per run: {run_id, ok, code, email, duration, error}

    start_license_watchdog()
    try:
        if console is not None:
            with Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(complete_style="green", finished_style="green"),
                MofNCompleteColumn(),
                TextColumn("[green]✓ {task.fields[ok]}[/green] [red]✗ {task.fields[fail]}[/red]"),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            ) as progress:
                task_id = progress.add_task(
                    "Working",
                    total=runs,
                    ok=0,
                    fail=0,
                )

                def _execute_one(run_id):
                    t0 = time.time()
                    ok, code, email, err = run_single_quiet(
                        run_id=run_id,
                        domains=domains,
                        capsolver_api_key=capsolver_api_key,
                        capsolver_timeout=capsolver_timeout,
                        proxy_config=proxy_config,
                    )
                    duration = time.time() - t0
                    return {
                        "run_id": run_id,
                        "ok": ok,
                        "code": code,
                        "email": email,
                        "duration": duration,
                        "error": err,
                    }

                def _bump(progress_obj, task, ok):
                    # Update the success/fail counters on the progress bar.
                    if ok:
                        progress_obj.update(task, ok=progress_obj.tasks[task].fields["ok"] + 1)
                    else:
                        progress_obj.update(task, fail=progress_obj.tasks[task].fields["fail"] + 1)
                    progress_obj.advance(task)

                if workers == 1:
                    for i in range(runs):
                        if license_is_revoked():
                            break
                        r = _execute_one(i + 1)
                        results.append(r)
                        _bump(progress, task_id, r["ok"])
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {
                            pool.submit(_execute_one, i + 1): i + 1
                            for i in range(runs)
                        }
                        for fut in as_completed(futures):
                            run_id = futures[fut]
                            try:
                                r = fut.result()
                            except Exception as exc:
                                log_error(f"[run {run_id}] Unhandled exception: {exc}", exc=exc)
                                r = {
                                    "run_id": run_id,
                                    "ok": False,
                                    "code": None,
                                    "email": None,
                                    "duration": 0.0,
                                    "error": str(exc),
                                }
                            results.append(r)
                            _bump(progress, task_id, r["ok"])
        else:
            # Plain fallback when rich is missing
            if workers == 1:
                for i in range(runs):
                    if license_is_revoked():
                        break
                    ok, code, email, err = run_single_quiet(
                        run_id=i + 1,
                        domains=domains,
                        capsolver_api_key=capsolver_api_key,
                        capsolver_timeout=capsolver_timeout,
                        proxy_config=proxy_config,
                    )
                    results.append(
                        {"run_id": i + 1, "ok": ok, "code": code, "email": email, "duration": 0.0, "error": err}
                    )
                    print(f"[run {i+1}] {'OK' if ok else 'FAIL'} -> {code or err}")
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            run_single_quiet,
                            run_id=i + 1,
                            domains=domains,
                            capsolver_api_key=capsolver_api_key,
                            capsolver_timeout=capsolver_timeout,
                            proxy_config=proxy_config,
                        ): i + 1
                        for i in range(runs)
                    }
                    for fut in as_completed(futures):
                        run_id = futures[fut]
                        try:
                            ok, code, email, err = fut.result()
                        except Exception as exc:
                            ok, code, email, err = False, None, None, str(exc)
                        results.append(
                            {"run_id": run_id, "ok": ok, "code": code, "email": email, "duration": 0.0, "error": err}
                        )
                        print(f"[run {run_id}] {'OK' if ok else 'FAIL'} -> {code or err}")
    finally:
        stop_license_watchdog()

    total = time.time() - t_start
    success = sum(1 for r in results if r["ok"])
    failed = len(results) - success

    _print_summary(results, total, success, failed, runs)

    if license_is_revoked():
        log_error("Stopped early because the license is no longer valid.")
        sys.exit(2)
    if failed and not success:
        sys.exit(1)


def _print_summary(results, total, success, failed, runs):
    """Print the closing summary table."""
    if console is None:
        print(f"\nDone in {total:.1f}s: {success}/{runs} ok ({success / max(1, runs) * 100:.0f}%)")
        return

    console.print()

    success_rate = success / max(1, runs) * 100
    avg_duration = (
        sum(r["duration"] for r in results if r["ok"]) / success
        if success
        else 0.0
    )
    throughput = success / total if total > 0 else 0.0

    stats = Table(show_header=False, box=None, padding=(0, 2))
    stats.add_column(style="cyan", justify="right")
    stats.add_column(style="white")

    stats.add_row("Total time", f"{total:.1f}s")
    stats.add_row("Success", f"[green]{success}[/green] / {runs}  ({success_rate:.0f}%)")
    if failed:
        stats.add_row("Failed", f"[red]{failed}[/red]")
    if success:
        stats.add_row("Avg per code", f"{avg_duration:.1f}s")
        stats.add_row("Throughput", f"{throughput:.2f} codes/sec")

    title_color = "green" if not failed else ("yellow" if success else "red")
    console.print(
        Panel(
            stats,
            title=f"[bold {title_color}]Summary[/bold {title_color}]",
            border_style=title_color,
            padding=(0, 1),
        )
    )

    if success:
        console.print(f"[dim]→ {success} codes saved to[/dim] [cyan]{CODES_FILE}[/cyan]")
    if failed:
        console.print(f"[dim]→ {failed} errors logged to[/dim] [yellow]{ERROR_LOG_FILE}[/yellow]")
    console.print(
        f"[dim]→ Need more keys / want to support the project?[/dim] "
        f"[bold green]{CONTACT_URL}[/bold green]"
    )


def run_single_quiet(run_id, domains, capsolver_api_key, capsolver_timeout, proxy_config):
    """Quiet variant of run_single: no prints, only returns a result tuple.

    Returns: (ok: bool, code_url: str|None, email: str|None, error: str|None)
    """
    if license_is_revoked():
        return False, None, None, "License revoked"

    email = generate_email(random.choice(domains))
    run_proxy = _proxy_with_unique_session(proxy_config, run_id)

    try:
        token = solve_recaptcha_v2(
            api_key=capsolver_api_key,
            website_url=RECAPTCHA_SITE_URL,
            website_key=RECAPTCHA_SITE_KEY,
            timeout=capsolver_timeout,
            proxy_config=run_proxy,
        )
    except CapSolverError as exc:
        return False, None, email, f"CapSolver: {exc}"
    except requests.RequestException as exc:
        return False, None, email, f"CapSolver request error: {exc}"

    try:
        result = submit_to_upstream(
            email=email,
            captcha_token=token,
            proxy_config=run_proxy,
        )
    except requests.RequestException as exc:
        return False, None, email, f"Upstream submit error: {exc}"

    if not result.get("success"):
        msg = result.get("message") or str(result)
        return False, None, email, f"Upstream rejected: {msg}"

    code = extract_code(result.get("code"))
    if not code:
        return False, None, email, f"Code not found in response: {result}"

    full_url = f"chatgpt.com/up/{code}"
    try:
        save_code(full_url)
    except OSError as exc:
        return False, None, email, f"Save failed: {exc}"

    return True, full_url, email, None


def _proxy_with_unique_session(proxy_config, run_id):
    """Add a unique sticky-session id per run unless the user already pinned one.

    DataImpulse uses the `user__sid.<id>` username pattern. If
    DATAIMPULSE_SESSION is set in the env, we leave the username alone.
    """
    if not proxy_config:
        return None
    if (os.getenv("DATAIMPULSE_SESSION") or "").strip():
        return proxy_config

    base_username = proxy_config["username"]
    session_token = f"r{run_id}_{int(time.time() * 1000) % 1000000}"
    new_username = base_username + "__sid." + session_token
    return {**proxy_config, "username": new_username}


def parse_args():
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} - pure HTTP code grabber (no browser)."
    )
    parser.add_argument(
        "-n", "--runs",
        type=int,
        default=1,
        help="How many codes to grab (default: 1).",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=1,
        help="Parallel worker threads (default: 1). Capped to --runs.",
    )
    return parser.parse_args()


def submit_to_upstream(email, captcha_token, proxy_config=None):
    """POST to the upstream endpoint and return the parsed JSON response.

    The endpoint expects multipart/form-data with three fields:
      - assignOpenAICode = "true"
      - email           = the generated email
      - captchaToken    = the gRecaptchaResponse token from CapSolver
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-MX,es;q=0.9",
        "Origin": RECAPTCHA_REFERER_ORIGIN,
        "Referer": RECAPTCHA_SITE_URL,
    }

    files = {
        "assignOpenAICode": (None, "true"),
        "email": (None, email),
        "captchaToken": (None, captcha_token),
    }

    session = _get_proxied_session(proxy_config)
    res = session.post(
        SUBMIT_URL,
        headers=headers,
        files=files,
        timeout=30,
    )
    res.raise_for_status()
    try:
        return res.json()
    except json.JSONDecodeError as exc:
        raise requests.RequestException(
            f"Upstream response is not JSON: {res.text[:300]}"
        ) from exc


# Code pattern: 8-32 alphanumeric uppercase chars. The server returns
# "chatgpt.com/up/<CODE>", we keep just the trailing segment.
_RAW_CODE_PATTERN = re.compile(r"([A-Z0-9]{8,32})$")


def extract_code(raw):
    """Pull the code out of the upstream response's `code` field.

    Example raw: "chatgpt.com/up/SGQCW6X7QQTKDWC4" -> "SGQCW6X7QQTKDWC4".
    """
    if not raw:
        return None
    raw = raw.strip().rstrip("/")
    last = raw.rsplit("/", 1)[-1]
    match = _RAW_CODE_PATTERN.match(last.upper())
    if match:
        return match.group(1)
    match = _RAW_CODE_PATTERN.search(raw.upper())
    return match.group(1) if match else None


def save_code(code):
    with _FILE_LOCK, open(CODES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{code}\n")


# === CapSolver ===

class CapSolverError(Exception):
    pass


def solve_recaptcha_v2(api_key, website_url, website_key, timeout=180, poll_interval=1, proxy_config=None):
    """Create a ReCaptchaV2 task on CapSolver and return the gRecaptchaResponse.

    Note: requests to api.capsolver.com always go direct (never proxied) so
    they don't burn proxy bandwidth. proxy_config is forwarded as a task
    parameter so CapSolver itself egresses through that proxy.
    """
    if proxy_config:
        task = {
            "type": "ReCaptchaV2Task",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "proxy": "http:{host}:{port}:{user}:{password}".format(
                host=proxy_config["host"],
                port=proxy_config["port"],
                user=proxy_config["username"],
                password=proxy_config["password"],
            ),
        }
    else:
        task = {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }

    create_payload = {"clientKey": api_key, "task": task}

    session = _get_direct_session()
    res = session.post(CAPSOLVER_CREATE_TASK_URL, json=create_payload, timeout=30)
    res.raise_for_status()
    data = res.json()

    if data.get("errorId"):
        raise CapSolverError(
            f"createTask failed: {data.get('errorCode')} - {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise CapSolverError(f"createTask returned no taskId: {data}")

    # Initial delay before the first poll. CapSolver docs say >= 1s. We pick
    # 1.5s as a compromise: tasks rarely finish under 1.5s anyway, and longer
    # delays just waste wall time.
    time.sleep(1.5)

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = session.post(
            CAPSOLVER_GET_RESULT_URL,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        )
        result.raise_for_status()
        result_data = result.json()

        if result_data.get("errorId"):
            raise CapSolverError(
                f"getTaskResult failed: {result_data.get('errorCode')} - {result_data.get('errorDescription')}"
            )

        status = result_data.get("status")
        if status == "ready":
            token = (result_data.get("solution") or {}).get("gRecaptchaResponse")
            if not token:
                raise CapSolverError(f"No gRecaptchaResponse in solution: {result_data}")
            return token
        if status == "failed":
            raise CapSolverError(f"Task failed: {result_data}")
        time.sleep(poll_interval)

    raise CapSolverError(f"Timed out after {timeout}s waiting for CapSolver")


# === Proxy ===

def build_proxy_config():
    """Build the DataImpulse proxy config dict from .env. Returns None if disabled."""
    enabled = (os.getenv("PROXY_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None

    username = (os.getenv("DATAIMPULSE_USERNAME") or "").strip()
    password = (os.getenv("DATAIMPULSE_PASSWORD") or "").strip()
    host = (os.getenv("DATAIMPULSE_HOST") or DATAIMPULSE_DEFAULT_HOST).strip()
    port_raw = (os.getenv("DATAIMPULSE_PORT") or str(DATAIMPULSE_DEFAULT_PORT)).strip()

    if not username or not password:
        log_warn("PROXY_ENABLED=true but DATAIMPULSE_USERNAME/PASSWORD is empty. Proxy disabled.")
        return None

    try:
        port = int(port_raw)
    except ValueError:
        log_warn(f"DATAIMPULSE_PORT is not a number: {port_raw}. Falling back to {DATAIMPULSE_DEFAULT_PORT}.")
        port = DATAIMPULSE_DEFAULT_PORT

    country = (os.getenv("DATAIMPULSE_COUNTRY") or "").strip().lower()
    session_id = (os.getenv("DATAIMPULSE_SESSION") or "").strip()

    extras = []
    if country:
        extras.append(f"cr.{country}")
    if session_id:
        extras.append(f"sid.{session_id}")
    if extras:
        username = username + "__" + "__".join(extras)

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "server": f"http://{host}:{port}",
    }


def _requests_proxies(proxy_config):
    """Convert proxy_config to a dict suitable for requests' `proxies=` kwarg."""
    if not proxy_config:
        return None
    auth_url = "http://{user}:{password}@{host}:{port}".format(
        user=proxy_config["username"],
        password=proxy_config["password"],
        host=proxy_config["host"],
        port=proxy_config["port"],
    )
    return {"http": auth_url, "https": auth_url}


# === Helpers ===

def parse_domains(value):
    if not value:
        return []
    return [domain.strip().lstrip("@") for domain in value.split(",") if domain.strip()]


def generate_email(domain):
    alphabet = string.ascii_lowercase + string.digits
    local_part = "user" + "".join(random.choice(alphabet) for _ in range(12))
    return f"{local_part}@{domain}"


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
    except Exception as exc:
        log_error(f"Unhandled exception: {exc}", exc=exc)
        sys.exit(1)
