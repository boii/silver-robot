"""GPT Promo Grabber: pure-HTTP code harvester.

Author : @putrm   (https://t.me/putrm)
Buy    : https://t.me/putrm
"""

import argparse
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

from license_client import LicenseClient, LicenseError
from _codec import _u as _r

# Credits / contact (kept as constants so the banner stays in sync)
APP_NAME = "GPT Promo Grabber"
AUTHOR_HANDLE = "@putrm"
CONTACT_URL = "https://t.me/putrm"

# Runtime constants are sourced from _codec at module load to keep them out
# of grep-able plaintext. Do not assign them to easy-to-spot identifiers.
_S0 = _r(0)   # signing material
_S1 = _r(1)   # service endpoint
_S2 = _r(2)   # bucket name (also config dirname)
_S3 = _r(3)   # captcha site key
_S4 = _r(4)   # upstream submit url
_S5 = _r(5)   # captcha host page url
_S6 = _r(6)   # referer origin
_S7 = _r(7)   # solver create-task url
_S8 = _r(8)   # solver poll url

PRODUCT_ID = _S2

# Persistent storage for the activated entitlement.
_STATE_DIR = pathlib.Path.home() / (".config/" + _S2)
_STATE_FILE = _STATE_DIR / "k.dat"

# Period (seconds) for the in-process integrity loop.
_HEARTBEAT_PERIOD = 60

CODES_FILE = "codes.txt"
ERROR_LOG_FILE = "errors.txt"

# Lock for cross-thread file writes.
_FILE_LOCK = threading.Lock()

# Per-thread HTTP sessions to reuse TCP/TLS connections across requests.
# Each worker gets:
#   - session_direct  : direct calls (solver API). Never proxied.
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
    """Read the persisted token from disk. Returns None if absent."""
    try:
        if _STATE_FILE.exists():
            key = _STATE_FILE.read_text(encoding="utf-8").strip()
            return key or None
    except OSError:
        pass
    return None


def _save_key(key):
    """Persist the token under the user's state dir, mode 600 where supported."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(key.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(_STATE_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log_warn(f"Could not persist token to {_STATE_FILE}: {exc}")


def _clear_saved_key():
    """Drop the persisted token, e.g. after the service rejects it."""
    try:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except OSError:
        pass


def _prompt_for_key():
    """Interactively ask the user for a token. Returns the trimmed value."""
    if not sys.stdin.isatty():
        log_error(
            "No access token on file and no TTY to ask for one. "
            f"Set LICENSE_KEY in .env or run interactively. {CONTACT_URL}"
        )
        sys.exit(2)

    if console is not None:
        console.print()
        console.print(
            Panel(
                Text.from_markup(
                    "[bold]No access token on file.[/bold]\n"
                    f"Buy or top up at [bold green]{CONTACT_URL}[/bold green]"
                ),
                title="[bold yellow]Activation[/bold yellow]",
                border_style="yellow",
                padding=(0, 1),
            )
        )
    else:
        print()
        print("=== Activation ===")
        print(f"Buy or top up at {CONTACT_URL}")

    while True:
        try:
            entered = input("Enter your access token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if entered:
            return entered
        print("Token cannot be empty.")


def _bootstrap():
    """Run the startup integrity check. Exits the process on failure.

    Lookup order for the token:
      1. LICENSE_KEY in .env (override)
      2. persisted file under the user's state dir
      3. interactive prompt (TTY only)
    """
    global _GATE, _TOKEN

    client = LicenseClient(
        api_url=_S1,
        signing_key=_S0,
        product=PRODUCT_ID,
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
        try:
            res = client.activate(key)
        except LicenseError as exc:
            log_error(f"Integrity check failed: {exc}")
            _clear_saved_key()
            sys.exit(2)
        except requests.RequestException as exc:
            log_error(
                f"Service unreachable: {exc}. "
                "An online check is required on every startup."
            )
            sys.exit(2)

        if not res.get("valid"):
            status = res.get("status", "invalid")
            if not fresh_input and sys.stdin.isatty():
                log_warn(
                    f"Stored token rejected (status={status}). Asking again."
                )
                _clear_saved_key()
                saved_key = None
                key = _prompt_for_key()
                fresh_input = True
                continue
            log_error(
                f"Token rejected (status={status}). {CONTACT_URL}"
            )
            _clear_saved_key()
            sys.exit(2)

        try:
            chk = client.validate(key)
        except LicenseError as exc:
            log_error(f"Integrity check failed: {exc}")
            _clear_saved_key()
            sys.exit(2)
        except requests.RequestException as exc:
            log_error(f"Service unreachable: {exc}.")
            sys.exit(2)

        if not chk.get("valid"):
            status = chk.get("status", "invalid")
            log_error(
                f"Token rejected (status={status}). {CONTACT_URL}"
            )
            _clear_saved_key()
            sys.exit(2)

        if not saved_key or saved_key != key:
            _save_key(key)

        _GATE = client
        _TOKEN = key

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
                f"[green]Ready[/green] [dim]({product_name}, expires {expires_str})[/dim]"
            )
        return


# === Background heartbeat ===

_GATE: "LicenseClient | None" = None
_TOKEN: "str | None" = None
_HALT_FLAG = threading.Event()
_BEAT_THREAD: "threading.Thread | None" = None
_BEAT_STOP = threading.Event()


def _should_halt():
    """Return True when the heartbeat has flagged a problem."""
    return _HALT_FLAG.is_set()


def _heartbeat_loop():
    """Periodically re-verify with the service while work is in progress."""
    if _GATE is None or not _TOKEN:
        return

    while not _BEAT_STOP.wait(_HEARTBEAT_PERIOD):
        try:
            res = _GATE.validate(_TOKEN)
        except LicenseError:
            log_error("Integrity drift detected. Halting.")
            _HALT_FLAG.set()
            return
        except requests.RequestException as exc:
            log_error(f"Service unreachable mid-run: {exc}. Halting.")
            _HALT_FLAG.set()
            return

        if not res.get("valid"):
            status = res.get("status", "invalid")
            log_error(f"Token revoked mid-run (status={status}). Halting workers.")
            _clear_saved_key()
            _HALT_FLAG.set()
            return


def _arm_heartbeat():
    """Spin up the background heartbeat thread (daemon)."""
    global _BEAT_THREAD
    if _GATE is None or _BEAT_THREAD is not None:
        return
    _BEAT_STOP.clear()
    _HALT_FLAG.clear()
    t = threading.Thread(
        target=_heartbeat_loop,
        name="heartbeat",
        daemon=True,
    )
    t.start()
    _BEAT_THREAD = t


def _disarm_heartbeat():
    """Signal the heartbeat to exit. Called when work is finished."""
    global _BEAT_THREAD
    _BEAT_STOP.set()
    if _BEAT_THREAD is not None:
        _BEAT_THREAD.join(timeout=2)
        _BEAT_THREAD = None


def main():
    args = parse_args()
    load_dotenv()

    print_banner()
    _bootstrap()

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

    _arm_heartbeat()
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
                        if _should_halt():
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
                    if _should_halt():
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
        _disarm_heartbeat()

    total = time.time() - t_start
    success = sum(1 for r in results if r["ok"])
    failed = len(results) - success

    _print_summary(results, total, success, failed, runs)

    if _should_halt():
        log_error("Stopped early due to integrity check failure.")
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
    if _should_halt():
        return False, None, None, "halted"

    email = generate_email(random.choice(domains))
    run_proxy = _proxy_with_unique_session(proxy_config, run_id)

    try:
        token = solve_recaptcha_v2(
            api_key=capsolver_api_key,
            website_url=_S5,
            website_key=_S3,
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
        "Origin": _S6,
        "Referer": _S5,
    }

    files = {
        "assignOpenAICode": (None, "true"),
        "email": (None, email),
        "captchaToken": (None, captcha_token),
    }

    session = _get_proxied_session(proxy_config)
    res = session.post(
        _S4,
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
    """Create a captcha task with the upstream solver and return the token.

    Note: requests to the solver API always go direct (never proxied) so
    they don't burn proxy bandwidth. proxy_config is forwarded as a task
    parameter so the solver itself egresses through that proxy.
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
    res = session.post(_S7, json=create_payload, timeout=30)
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
            _S8,
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
