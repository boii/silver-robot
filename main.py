"""Pure HTTP version: tanpa browser. Hanya panggilan requests + CapSolver.

Flow:
  1. Solve reCAPTCHA via CapSolver (sitekey & URL hardcoded).
  2. POST email + token ke endpoint BBVA Descuentos.
  3. Parse `code` dari response JSON, simpan ke codes.txt.

Bandwidth ~10-30 KB per run (turun dari ~1-3 MB versi browser).
Total waktu ~CapSolver time + 2-3 detik (sisa untuk HTTP overhead).

Versi browser tetap tersedia di main_browser.py kalau backend BBVA berubah.
"""

import argparse
import json
import os
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

# Rich UI (graceful fallback kalau library belum terinstall)
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
    from rich.live import Live
    from rich.align import Align
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

console = Console() if _HAS_RICH else None

# Konstanta dari hasil inspect_network.py
BBVA_SUBMIT_URL = "https://www.bbvadescuentos.mx/admin-site/php/_httprequest.php"
RECAPTCHA_SITE_URL = "https://www.bbvadescuentos.mx/develop/openai-3msc"
RECAPTCHA_SITE_KEY = "6LfG0tIsAAAAAINTtPyFHgumY1_U11qbAxQuzh7O"

CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

CODES_FILE = "codes.txt"
ERROR_LOG_FILE = "errors.txt"

# Lock untuk write file dari banyak thread sekaligus.
_FILE_LOCK = threading.Lock()

# Session HTTP per thread untuk reuse koneksi TCP/TLS antar request.
# Tiap worker thread punya 2 session:
#  - session_direct  : panggilan langsung (CapSolver API). Tidak lewat proxy.
#  - session_proxied : panggilan ke target site (BBVA). Lewat proxy DataImpulse.
# Tujuan: handshake TLS hanya 1x per host, sisanya reuse koneksi.
_THREAD_LOCAL = threading.local()


def _make_session():
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def _get_direct_session():
    """Session untuk panggilan CapSolver (tidak lewat proxy)."""
    sess = getattr(_THREAD_LOCAL, "session_direct", None)
    if sess is None:
        sess = _make_session()
        _THREAD_LOCAL.session_direct = sess
    return sess


def _get_proxied_session(proxy_config):
    """Session untuk panggilan target site (BBVA). Lewat proxy kalau diberikan.

    Kita cache session per (host, port, username) supaya kalau session ID
    proxy berubah (per run), kita bikin session baru. Tapi untuk run dari
    proxy/session yang sama, koneksi reused.
    """
    if not proxy_config:
        # Tanpa proxy, sama saja dengan direct session
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
        print(f"WARN: gagal menulis error log: {write_exc}", file=sys.stderr)


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
    """Cetak banner di awal program."""
    if console is None:
        print("=== BBVA Code Grabber ===")
        return

    banner = Text()
    banner.append("BBVA ", style="bold blue")
    banner.append("OpenAI ", style="bold cyan")
    banner.append("Code Grabber", style="bold white")
    banner.append("\nPure HTTP + multi-thread + DataImpulse + CapSolver", style="dim")
    console.print(Panel(Align.center(banner), border_style="blue", padding=(0, 2)))


def main():
    args = parse_args()
    load_dotenv()

    domains = parse_domains(os.getenv("EMAIL_DOMAINS"))
    capsolver_api_key = (os.getenv("CAPSOLVER_API_KEY") or "").strip()
    capsolver_timeout = int(os.getenv("CAPSOLVER_TIMEOUT", "180"))

    if not domains:
        log_error("EMAIL_DOMAINS belum diatur di .env")
        sys.exit(1)
    if not capsolver_api_key:
        log_error("CAPSOLVER_API_KEY belum diatur di .env")
        sys.exit(1)

    proxy_config = build_proxy_config()

    runs = max(1, args.runs)
    workers = max(1, min(args.workers, runs))

    # Tampilkan config table
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
    results = []  # list of dict per run: {run_id, ok, code, email, duration, error}

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
                "Memproses",
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
                # Update field counter di progress bar.
                if ok:
                    progress_obj.update(task, ok=progress_obj.tasks[task].fields["ok"] + 1)
                else:
                    progress_obj.update(task, fail=progress_obj.tasks[task].fields["fail"] + 1)
                progress_obj.advance(task)

            if workers == 1:
                for i in range(runs):
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
        # Fallback tanpa rich
        if workers == 1:
            for i in range(runs):
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

    total = time.time() - t_start
    success = sum(1 for r in results if r["ok"])
    failed = len(results) - success

    # Tampilkan summary
    _print_summary(results, total, success, failed, runs)

    if failed and not success:
        sys.exit(1)


def _print_run_line(r):
    """[Deprecated] tidak dipakai lagi sejak progress bar saja yang ditampilkan."""
    return


def _print_summary(results, total, success, failed, runs):
    """Cetak summary table di akhir."""
    if console is None:
        print(f"\nSelesai dalam {total:.1f}s: {success}/{runs} sukses ({success / max(1, runs) * 100:.0f}%)")
        return

    console.print()

    # Stats panel
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

    # File output info
    if success:
        console.print(f"[dim]→ {success} kode disimpan ke[/dim] [cyan]{CODES_FILE}[/cyan]")
    if failed:
        console.print(f"[dim]→ {failed} error tercatat di[/dim] [yellow]{ERROR_LOG_FILE}[/yellow]")


def run_single_quiet(run_id, domains, capsolver_api_key, capsolver_timeout, proxy_config):
    """Versi quiet dari run_single: tidak print, hanya return tuple hasil.

    Returns: (ok: bool, code_url: str|None, email: str|None, error: str|None)
    """
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
        result = submit_to_bbva(
            email=email,
            captcha_token=token,
            proxy_config=run_proxy,
        )
    except requests.RequestException as exc:
        return False, None, email, f"BBVA submit error: {exc}"

    if not result.get("success"):
        msg = result.get("message") or str(result)
        return False, None, email, f"BBVA tolak: {msg}"

    code = extract_code(result.get("code"))
    if not code:
        return False, None, email, f"Kode tidak ditemukan di response: {result}"

    full_url = f"chatgpt.com/up/{code}"
    try:
        save_code(full_url)
    except OSError as exc:
        return False, None, email, f"Gagal simpan: {exc}"

    return True, full_url, email, None


def run_single(run_id, domains, capsolver_api_key, capsolver_timeout, proxy_config):
    """Wrapper backward-compatible. Mengembalikan boolean."""
    ok, _code, _email, _err = run_single_quiet(
        run_id, domains, capsolver_api_key, capsolver_timeout, proxy_config
    )
    return ok


def _proxy_with_unique_session(proxy_config, run_id):
    """Tambahkan session id unik per run kalau user belum set manual.

    DataImpulse pakai username pattern `user__sid.<id>`. Kalau user sudah
    set DATAIMPULSE_SESSION, hormati config-nya.
    """
    if not proxy_config:
        return None
    if (os.getenv("DATAIMPULSE_SESSION") or "").strip():
        return proxy_config

    base_username = proxy_config["username"]
    session_token = f"r{run_id}_{int(time.time() * 1000) % 1000000}"
    # Kalau username sudah ada `__` (artinya sudah append country), tinggal
    # tambah `__sid.xxx`. Kalau belum, tambah dengan separator yang sama.
    new_username = base_username + "__sid." + session_token
    return {**proxy_config, "username": new_username}


def parse_args():
    parser = argparse.ArgumentParser(
        description="BBVA OpenAI code grabber (pure HTTP, tanpa browser)."
    )
    parser.add_argument(
        "-n", "--runs",
        type=int,
        default=1,
        help="Jumlah kode yang ingin diambil (default: 1).",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=1,
        help="Jumlah thread paralel (default: 1). Otomatis dibatasi ke jumlah runs.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Diabaikan di versi HTTP. Pakai main_browser.py untuk mode browser.",
    )
    return parser.parse_args()


def elapsed(t0):
    return f"{time.time() - t0:5.1f}s"


def submit_to_bbva(email, captcha_token, proxy_config=None):
    """POST ke endpoint BBVA dan kembalikan dict response.

    Endpoint pakai multipart/form-data dengan tiga field:
      - assignOpenAICode = "true"
      - email           = email user
      - captchaToken    = token gRecaptchaResponse dari CapSolver
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-MX,es;q=0.9",
        "Origin": "https://www.bbvadescuentos.mx",
        "Referer": RECAPTCHA_SITE_URL,
    }

    files = {
        "assignOpenAICode": (None, "true"),
        "email": (None, email),
        "captchaToken": (None, captcha_token),
    }

    session = _get_proxied_session(proxy_config)
    res = session.post(
        BBVA_SUBMIT_URL,
        headers=headers,
        files=files,
        timeout=30,
    )
    res.raise_for_status()
    try:
        return res.json()
    except json.JSONDecodeError as exc:
        raise requests.RequestException(
            f"Response BBVA bukan JSON: {res.text[:300]}"
        ) from exc


# Pola kode BBVA (16 karakter alfanumerik uppercase). Response dari BBVA
# berbentuk "chatgpt.com/up/<CODE>" — kita ambil bagian setelah slash terakhir.
_RAW_CODE_PATTERN = re.compile(r"([A-Z0-9]{8,32})$")


def extract_code(raw):
    """Ekstrak kode dari nilai field `code` response BBVA.

    Contoh raw: "chatgpt.com/up/SGQCW6X7QQTKDWC4" -> "SGQCW6X7QQTKDWC4".
    """
    if not raw:
        return None
    raw = raw.strip().rstrip("/")
    # Ambil segmen terakhir setelah slash
    last = raw.rsplit("/", 1)[-1]
    match = _RAW_CODE_PATTERN.match(last.upper())
    if match:
        return match.group(1)
    # Kalau format berubah, jatuh ke regex umum
    match = _RAW_CODE_PATTERN.search(raw.upper())
    return match.group(1) if match else None


def save_code(code):
    with _FILE_LOCK, open(CODES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{code}\n")


# === CapSolver ===

class CapSolverError(Exception):
    pass


def solve_recaptcha_v2(api_key, website_url, website_key, timeout=180, poll_interval=1, proxy_config=None):
    """Buat task ReCaptchaV2 di CapSolver dan kembalikan token gRecaptchaResponse.

    Catatan: panggilan ke api.capsolver.com selalu langsung (tidak lewat proxy)
    supaya tidak menghabiskan bandwidth proxy. proxy_config hanya dipakai
    sebagai parameter task — CapSolver yang akan keluar lewat proxy itu.
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
            f"createTask gagal: {data.get('errorCode')} - {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise CapSolverError(f"createTask tidak mengembalikan taskId: {data}")

    # Initial delay sebelum poll pertama. Docs CapSolver bilang minimum 1s.
    # Kita pakai 1.5s sebagai kompromi: jarang banget task selesai <1.5s,
    # jadi tidak banyak ekstra poll. Sebelumnya 4s = sering buang waktu.
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
                f"getTaskResult gagal: {result_data.get('errorCode')} - {result_data.get('errorDescription')}"
            )

        status = result_data.get("status")
        if status == "ready":
            token = (result_data.get("solution") or {}).get("gRecaptchaResponse")
            if not token:
                raise CapSolverError(f"Tidak ada gRecaptchaResponse di solusi: {result_data}")
            return token
        if status == "failed":
            raise CapSolverError(f"Task gagal diselesaikan: {result_data}")
        time.sleep(poll_interval)

    raise CapSolverError(f"Timeout {timeout}s menunggu hasil CapSolver")


# === Proxy ===

def build_proxy_config():
    """Build dict konfigurasi proxy DataImpulse dari env. Returns None kalau off."""
    enabled = (os.getenv("PROXY_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None

    username = (os.getenv("DATAIMPULSE_USERNAME") or "").strip()
    password = (os.getenv("DATAIMPULSE_PASSWORD") or "").strip()
    host = (os.getenv("DATAIMPULSE_HOST") or DATAIMPULSE_DEFAULT_HOST).strip()
    port_raw = (os.getenv("DATAIMPULSE_PORT") or str(DATAIMPULSE_DEFAULT_PORT)).strip()

    if not username or not password:
        log_warn("PROXY_ENABLED=true tapi DATAIMPULSE_USERNAME/PASSWORD kosong. Proxy diabaikan.")
        return None

    try:
        port = int(port_raw)
    except ValueError:
        log_warn(f"DATAIMPULSE_PORT tidak valid: {port_raw}. Pakai default {DATAIMPULSE_DEFAULT_PORT}.")
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
    """Konversi proxy_config jadi dict untuk parameter `proxies` di requests."""
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
        print("\nDibatalkan oleh user.")
        sys.exit(130)
    except Exception as exc:
        log_error(f"Unhandled exception: {exc}", exc=exc)
        sys.exit(1)
