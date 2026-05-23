import argparse
import os
import random
import re
import string
import sys
import time
import traceback

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.bbva.mx/chatgpt.html#content-textandimage_1905761525"

CAPSOLVER_CREATE_TASK_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"

CODES_FILE = "codes.txt"
ERROR_LOG_FILE = "errors.txt"

# Default DataImpulse gateway (https://dataimpulse.com)
DATAIMPULSE_DEFAULT_HOST = "gw.dataimpulse.com"
DATAIMPULSE_DEFAULT_PORT = 823


def log_error(message, exc=None):
    """Print error ke stderr dan append ke ERROR_LOG_FILE dengan timestamp."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(f"ERROR: {message}", file=sys.stderr)

    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if exc is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                f.write(tb)
                if not tb.endswith("\n"):
                    f.write("\n")
    except OSError as write_exc:
        print(f"WARN: gagal menulis error log: {write_exc}", file=sys.stderr)


def log_warn(message):
    """Print warning ke stderr dan append ke ERROR_LOG_FILE sebagai WARN."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] WARN {message}"
    print(f"WARN: {message}", file=sys.stderr)
    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    args = parse_args()
    load_dotenv()
    domains = parse_domains(os.getenv("EMAIL_DOMAINS"))
    capsolver_api_key = (os.getenv("CAPSOLVER_API_KEY") or "").strip()
    capsolver_timeout = int(os.getenv("CAPSOLVER_TIMEOUT", "180"))

    if not domains:
        log_error("EMAIL_DOMAINS belum diatur. Buat file .env berisi: EMAIL_DOMAINS=example.com,example.org")
        sys.exit(1)

    if not capsolver_api_key:
        log_error("CAPSOLVER_API_KEY belum diatur di file .env")
        sys.exit(1)

    proxy_config = build_proxy_config()  # bisa None
    if proxy_config:
        print(f"Proxy aktif: {proxy_config['server']} (user={proxy_config['username']})")

    email = generate_email(random.choice(domains))

    headless = not args.show_browser

    with sync_playwright() as p:
        launch_kwargs = {"headless": headless}
        if proxy_config:
            launch_kwargs["proxy"] = {
                "server": proxy_config["server"],
                "username": proxy_config["username"],
                "password": proxy_config["password"],
            }

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-MX",
            viewport={"width": 1280, "height": 800},
        )

        # Bandwidth saver: blokir resource yang tidak dibutuhkan untuk
        # flow (gambar, font, media), serta domain analytics/tracking yang
        # umum ada di halaman BBVA tapi tidak terkait form & captcha.
        _install_bandwidth_blocker(context)

        page = context.new_page()
        t0 = time.time()
        try:
            # commit = sudah dapat respons HTML, lebih cepat dari domcontentloaded.
            page.goto(URL, wait_until="commit", timeout=60000)
        except Exception as exc:
            log_error(f"Gagal membuka halaman {URL}: {exc}", exc=exc)
            browser.close()
            sys.exit(1)

        email_input, email_frame = find_email_input(page, total_timeout=20)
        if email_input is None:
            log_error("Kolom email tidak ditemukan. Halaman mungkin berubah atau form belum termuat.")
            browser.close()
            sys.exit(1)

        try:
            email_input.fill(email)
        except Exception as exc:
            log_error(f"Gagal mengisi email: {exc}", exc=exc)
            browser.close()
            sys.exit(1)
        print(f"Email berhasil diisi: {email}")

        sitekey, captcha_url = find_recaptcha_sitekey(page)
        if not sitekey:
            log_error("reCAPTCHA sitekey tidak ditemukan di halaman.")
            browser.close()
            sys.exit(1)

        # Kalau form ada di iframe, prioritaskan URL iframe-nya supaya
        # domain validation di CapSolver lolos.
        if email_frame is not None and email_frame.url:
            captcha_url = email_frame.url
        elif not captcha_url:
            captcha_url = page.url

        print(f"Sitekey reCAPTCHA ditemukan: {sitekey}")
        print(f"websiteURL untuk CapSolver: {captcha_url}")
        print("Mengirim task ke CapSolver...")

        try:
            token = solve_recaptcha_v2(
                api_key=capsolver_api_key,
                website_url=captcha_url,
                website_key=sitekey,
                timeout=capsolver_timeout,
                proxy_config=proxy_config,
            )
        except CapSolverError as exc:
            log_error(f"CapSolver: {exc}", exc=exc)
            browser.close()
            sys.exit(1)
        except requests.RequestException as exc:
            log_error(f"Gagal request ke CapSolver: {exc}", exc=exc)
            browser.close()
            sys.exit(1)

        print("Token diterima dari CapSolver. Menyuntikkan ke halaman...")
        try:
            inject_recaptcha_token(page, token)
        except Exception as exc:
            log_error(f"Gagal menyuntikkan token reCAPTCHA: {exc}", exc=exc)
            browser.close()
            sys.exit(1)
        print("Token telah disuntikkan. reCAPTCHA seharusnya sudah selesai.")

        if not click_submit_button(email_frame or page):
            log_warn("Tombol submit tidak ditemukan otomatis.")

        code = wait_for_code(email_frame or page, timeout=60)
        if code:
            try:
                save_code(email, code)
            except OSError as exc:
                log_error(f"Gagal menyimpan kode ke {CODES_FILE}: {exc}", exc=exc)
            else:
                print(f"Kode berhasil didapat: {code}")
                print(f"Kode disimpan ke {CODES_FILE}")
        else:
            log_warn("Kode tidak terdeteksi dalam batas waktu.")

        browser.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Auto-fill form BBVA + solve reCAPTCHA via CapSolver."
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Tampilkan jendela browser (default: headless / tidak terlihat).",
    )
    return parser.parse_args()


# Resource type yang langsung diblokir (tidak dipakai flow & boros bandwidth)
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "imageset"}

# Domain pihak ketiga yang umum ada di halaman BBVA tapi tidak terkait
# form/captcha: analytics, tag manager, optimization, ads, A/B test, dsb.
# Resource dari domain ini diblokir agar bandwidth proxy hemat.
_BLOCKED_HOST_KEYWORDS = (
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "googleadservices.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com/tr",
    "connect.facebook",
    "adobedtm.com",
    "demdex.net",
    "omtrdc.net",
    "everesttech.net",
    "criteo",
    "hotjar",
    "clarity.ms",
    "linkedin.com/li",
    "tiktok.com",
    "snap.licdn",
    "newrelic",
    "nr-data",
    "optimizely",
    "segment.com",
    "amplitude",
    "mixpanel",
    "branch.io",
    "appsflyer",
    "qualtrics",
    "tealium",
    "salesforce",  # marketing cloud tracking pixel; form Salesforce di-load lewat domain lain
    "youtube.com",
    "vimeo.com",
    "twitter.com",
)


def _install_bandwidth_blocker(context):
    """Pasang route handler untuk meminimalkan bandwidth.

    Memblokir:
      - Resource type: image, media, font, imageset.
      - Stylesheet (kita tidak butuh CSS untuk fill form / inject token).
      - Request ke domain analytics / tracking / ads.

    TIDAK memblokir: dokumen utama, script (form butuh JS), XHR/fetch (form
    butuh API call), dan domain reCAPTCHA (google.com/recaptcha).
    """

    def _handler(route):
        try:
            req = route.request
            rtype = req.resource_type
            url = req.url

            # Whitelist domain reCAPTCHA & DataImpulse dulu (jangan diblokir).
            if "recaptcha" in url or "google.com/recaptcha" in url or "gstatic.com/recaptcha" in url:
                route.continue_()
                return

            # Blokir berdasar tipe resource
            if rtype in _BLOCKED_RESOURCE_TYPES:
                route.abort()
                return

            # CSS pun tidak dipakai flow, abort untuk hemat bandwidth.
            if rtype == "stylesheet":
                route.abort()
                return

            # Blokir domain analytics/tracking
            host = url.lower()
            for keyword in _BLOCKED_HOST_KEYWORDS:
                if keyword in host:
                    route.abort()
                    return

            route.continue_()
        except Exception:
            # Jangan biarkan exception di handler memblokir request.
            try:
                route.continue_()
            except Exception:
                pass

    context.route("**/*", _handler)


def find_email_input(page, total_timeout=20):
    """Cari kolom email di halaman utama maupun di dalam semua iframe.

    Mengembalikan tuple (locator, frame) dimana frame=None jika di main page.
    Polling 250ms supaya cepat menangkap form dinamis.
    """
    selectors = [
        "input[type='email']",
        "input[placeholder*='Correo' i]",
        "input[placeholder*='correo' i]",
        "input[placeholder*='email' i]",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[name*='correo' i]",
        "input[id*='correo' i]",
    ]

    deadline = time.time() + total_timeout
    while time.time() < deadline:
        # Coba di main page lebih dulu
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible():
                    return locator, None
            except Exception:
                continue

        # Lalu cek setiap frame (iframe)
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                for selector in selectors:
                    try:
                        locator = frame.locator(selector).first
                        if locator.count() == 0:
                            continue
                        if locator.is_visible():
                            return locator, frame
                    except Exception:
                        continue
        except Exception:
            pass

        time.sleep(0.25)

    return None, None


def find_recaptcha_sitekey(page, total_timeout=15):
    """Cari sitekey reCAPTCHA dari halaman utama atau iframe-nya.

    Mengembalikan tuple (sitekey, host_url) dimana host_url adalah URL
    frame/halaman yang menjalankan widget reCAPTCHA. Untuk CapSolver kita
    butuh host_url yang benar supaya domain validation lolos.

    Polling 250ms supaya kita tidak menunggu lebih lama dari yang dibutuhkan.
    """
    from urllib.parse import urlparse, parse_qs

    deadline = time.time() + total_timeout
    while time.time() < deadline:
        # 1) Cari elemen [data-sitekey] di main page atau iframe child
        try:
            for frame in page.frames:
                try:
                    sitekey = frame.evaluate(
                        """() => {
                            const el = document.querySelector('[data-sitekey]');
                            return el ? el.getAttribute('data-sitekey') : null;
                        }"""
                    )
                    if sitekey:
                        return sitekey, frame.url or page.url
                except Exception:
                    continue
        except Exception:
            pass

        # 2) Parse dari URL iframe reCAPTCHA itu sendiri (parameter k=).
        try:
            for frame in page.frames:
                url = frame.url or ""
                if "recaptcha" in url and "k=" in url:
                    qs = parse_qs(urlparse(url).query)
                    if "k" in qs and qs["k"]:
                        parent = frame.parent_frame or page.main_frame
                        host_url = (parent.url if parent else None) or page.url
                        return qs["k"][0], host_url
        except Exception:
            pass

        time.sleep(0.25)

    return None, None


def inject_recaptcha_token(page, token):
    """Suntikkan token g-recaptcha-response dan panggil callback reCAPTCHA.

    Dijalankan di setiap frame (main page + iframe child) supaya menyentuh
    frame manapun yang me-load recaptcha api.js. Walk dibatasi depth + visited
    set supaya tidak hang oleh referensi sirkular di ___grecaptcha_cfg.
    """
    js = r"""
    (token) => {
      // 1) Isi semua textarea g-recaptcha-response
      document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea#g-recaptcha-response, textarea[id^="g-recaptcha-response"]'
      ).forEach((el) => {
        try {
          el.style.display = 'block';
          el.value = token;
          el.innerHTML = token;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        } catch (e) {}
      });

      // 2) Panggil callback reCAPTCHA secara terkontrol
      try {
        const cfg = window.___grecaptcha_cfg;
        if (!cfg || !cfg.clients) return;

        const seen = new WeakSet();
        const MAX_DEPTH = 6;

        const walk = (obj, depth) => {
          if (!obj || typeof obj !== 'object') return;
          if (depth > MAX_DEPTH) return;
          if (seen.has(obj)) return;
          seen.add(obj);

          for (const key of Object.keys(obj)) {
            let value;
            try { value = obj[key]; } catch (e) { continue; }
            if (typeof value === 'function') {
              if (
                key === 'callback' ||
                key === 'verifyCallback' ||
                (value.length === 1 && /callback/i.test(key))
              ) {
                try { value(token); } catch (e) {}
              }
            } else if (value && typeof value === 'object') {
              walk(value, depth + 1);
            }
          }
        };

        for (const cid of Object.keys(cfg.clients)) {
          walk(cfg.clients[cid], 0);
        }
      } catch (e) {}
    }
    """

    # Jalankan di main page
    try:
        page.evaluate(js, token)
    except Exception:
        pass

    # Jalankan juga di setiap iframe (kecuali frame internal recaptcha)
    try:
        frames = list(page.frames) if hasattr(page, "frames") else []
    except Exception:
        frames = []

    for frame in frames:
        try:
            url = frame.url or ""
            if "recaptcha" in url:
                continue
            frame.evaluate(js, token)
        except Exception:
            continue


class CapSolverError(Exception):
    pass


def click_submit_button(page):
    """Coba klik tombol submit form. Mengembalikan True jika berhasil klik."""
    selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Enviar')",
        "button:has-text('Suscribir')",
        "button:has-text('Suscribirme')",
        "button:has-text('Continuar')",
        "button:has-text('Aceptar')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
            locator.click(timeout=2000)
            print(f"Tombol submit diklik via selector: {selector}")
            return True
        except Exception:
            continue
    return False


# Pola kode BBVA: 8-32 karakter alfanumerik uppercase. Kode contoh: 5EJJKG8MKK5X57SX
_CODE_PATTERN = re.compile(r"\b([A-Z0-9]{8,32})\b")

# Kata yang harus diabaikan kalau hasil match cuma kata umum
_CODE_BLACKLIST = {"CODE", "CODIGO", "CÓDIGO", "TOKEN", "BBVA", "OPENAI", "CHATGPT", "COPIAR"}


def wait_for_code(page, timeout=60):
    """Tunggu kode muncul di halaman setelah form disubmit.

    Strategi (urut prioritas):
      1. Cari semua `<input>` yang value-nya cocok pola kode (di main page + iframe).
      2. Cari elemen dengan id/class/data-testid mengandung 'code'/'codigo'.
      3. Fallback: scan body text dengan regex pola kode.

    Polling 250ms supaya kode tertangkap nyaris seketika setelah render.
    """
    deadline = time.time() + timeout

    def _scan_inputs(scope):
        try:
            inputs = scope.locator("input").all()
        except Exception:
            return None
        for inp in inputs:
            try:
                if not inp.is_visible():
                    continue
                value = (inp.input_value() or "").strip()
                if not value:
                    continue
                # Skip kalau kelihatan email
                if "@" in value:
                    continue
                code = _validate_code_candidate(value)
                if code:
                    return code
            except Exception:
                continue
        return None

    body_check_interval = 1.0  # detik
    last_body_check = 0.0

    while time.time() < deadline:
        # 1) Scan semua input di main + iframes
        code = _scan_inputs(page)
        if code:
            return code
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                code = _scan_inputs(frame)
                if code:
                    return code
        except Exception:
            pass

        # 2) Cari elemen dengan id/class spesifik
        for selector in (
            "[id*='code' i]",
            "[id*='codigo' i]",
            "[class*='code' i]",
            "[class*='codigo' i]",
            "[data-testid*='code' i]",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if not locator.is_visible():
                    continue
                text = (locator.inner_text() or "").strip()
                if not text:
                    try:
                        text = (locator.input_value() or "").strip()
                    except Exception:
                        text = ""
                code = extract_code(text)
                if code:
                    return code
            except Exception:
                continue

        # 3) Fallback: scan body text (mahal, jadi cuma tiap 1 detik)
        if time.time() - last_body_check >= body_check_interval:
            last_body_check = time.time()
            try:
                body_text = page.locator("body").inner_text(timeout=1000)
                code = extract_code(body_text, prefer_context=True)
                if code:
                    return code
            except Exception:
                pass

        time.sleep(0.25)

    return None


def _validate_code_candidate(value):
    """Cek apakah `value` cocok dengan pola kode dan bukan kata blacklist."""
    if not value:
        return None
    candidate = value.strip()
    match = _CODE_PATTERN.fullmatch(candidate)
    if match and candidate.upper() not in _CODE_BLACKLIST:
        return candidate
    m = _CODE_PATTERN.search(candidate)
    if m and m.group(1).upper() not in _CODE_BLACKLIST:
        return m.group(1)
    return None


def extract_code(text, prefer_context=False):
    """Ekstrak kode dari teks. Jika prefer_context=True, prioritaskan kode yang
    ada di dekat kata 'codigo' atau 'code'."""
    if not text:
        return None

    if prefer_context:
        lowered = text.lower()
        for keyword in ("código", "codigo", "tu código", "code", "copia este"):
            idx = lowered.find(keyword)
            if idx == -1:
                continue
            snippet = text[idx : idx + 300]
            for match in _CODE_PATTERN.finditer(snippet):
                candidate = match.group(1)
                if candidate.upper() not in _CODE_BLACKLIST:
                    return candidate

    for match in _CODE_PATTERN.finditer(text):
        candidate = match.group(1)
        if candidate.upper() not in _CODE_BLACKLIST:
            return candidate
    return None


def save_code(email, code):
    """Simpan kode ke CODES_FILE, satu kode per baris."""
    with open(CODES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{code}\n")


def solve_recaptcha_v2(api_key, website_url, website_key, timeout=180, poll_interval=1, proxy_config=None):
    """Buat task ReCaptchaV2 di CapSolver dan kembalikan token.

    Kalau `proxy_config` diberikan (dict dengan host/port/username/password),
    pakai task type `ReCaptchaV2Task` dan kirim string proxy supaya CapSolver
    menyelesaikan captcha lewat proxy yang sama dengan browser kita.
    Kalau tidak, fallback ke `ReCaptchaV2TaskProxyLess`.

    Initial delay 4 detik (rekomendasi docs CapSolver) lalu poll setiap
    `poll_interval` detik.
    """
    if proxy_config:
        task = {
            "type": "ReCaptchaV2Task",
            "websiteURL": website_url,
            "websiteKey": website_key,
            # Format CapSolver: "http:host:port:user:pass"
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

    res = requests.post(CAPSOLVER_CREATE_TASK_URL, json=create_payload, timeout=30)
    res.raise_for_status()
    data = res.json()

    if data.get("errorId"):
        raise CapSolverError(
            f"createTask gagal: {data.get('errorCode')} - {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise CapSolverError(f"createTask tidak mengembalikan taskId: {data}")

    print(f"CapSolver taskId: {task_id}")

    # Initial delay sebelum poll pertama (rekomendasi docs CapSolver).
    time.sleep(4)

    deadline = time.time() + timeout
    # Reuse koneksi HTTP supaya overhead lebih kecil
    session = requests.Session()
    try:
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
            # status == "processing" -> ulangi polling
            time.sleep(poll_interval)
    finally:
        session.close()

    raise CapSolverError(f"Timeout {timeout}s menunggu hasil CapSolver")


def parse_domains(value):
    if not value:
        return []
    return [domain.strip().lstrip("@") for domain in value.split(",") if domain.strip()]


def build_proxy_config():
    """Build dict konfigurasi proxy DataImpulse dari env variables.

    Mengembalikan None kalau proxy tidak diaktifkan.
    Output dict: {host, port, username, password, server (url)}.
    """
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

    # Optional: target country / session params (DataImpulse mendukung lewat
    # username, contoh user__cr.us untuk negara US, user__sid.abc untuk session)
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
