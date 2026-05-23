"""Inspector: jalankan flow normal sekali, rekam semua XHR/fetch request +
response (terutama yang berisi kode hasil) ke `network.json`.

Tujuan: cari tahu endpoint yang harus dipanggil supaya bisa skip browser.

Pakai env yang sama dengan main.py (.env). Output:
  - network.json    : daftar request POST/PUT/PATCH dan response dari halaman
                       BBVA + form (skip request ke domain Google reCAPTCHA).
  - network_full.txt: log singkat semua request termasuk yang dilakukan iframe.
"""
import json
import os
import sys
import time

from main_browser import (
    URL,
    build_proxy_config,
    click_submit_button,
    find_email_input,
    find_recaptcha_sitekey,
    generate_email,
    inject_recaptcha_token,
    parse_domains,
    solve_recaptcha_v2,
    wait_for_code,
    _install_bandwidth_blocker,
)
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "network.json"
SUMMARY_FILE = "network_full.txt"

INTERESTING_METHODS = {"POST", "PUT", "PATCH"}
SKIP_HOST_KEYWORDS = (
    "google.com/recaptcha",
    "gstatic.com/recaptcha",
    "recaptcha.net",
)


def main():
    load_dotenv()
    domains = parse_domains(os.getenv("EMAIL_DOMAINS"))
    capsolver_api_key = (os.getenv("CAPSOLVER_API_KEY") or "").strip()
    if not domains or not capsolver_api_key:
        print("ERROR: EMAIL_DOMAINS dan CAPSOLVER_API_KEY wajib di .env")
        sys.exit(1)

    proxy_config = build_proxy_config()
    email = generate_email(domains[0])

    captured = []          # request POST/PUT/PATCH yang relevan
    summary_lines = []     # log singkat semua request

    def _on_request(request):
        try:
            line = f"{request.method} {request.resource_type} {request.url}"
            summary_lines.append(line)
        except Exception:
            pass

    def _on_response(response):
        try:
            req = response.request
            method = req.method
            url = req.url
            if method not in INTERESTING_METHODS:
                return
            if any(k in url for k in SKIP_HOST_KEYWORDS):
                return

            entry = {
                "method": method,
                "url": url,
                "status": response.status,
                "request_headers": dict(req.headers),
                "request_post_data": req.post_data,
                "response_headers": dict(response.headers),
            }
            # Coba ambil body, max 200KB
            try:
                body = response.body()
                if body:
                    text = body.decode("utf-8", errors="replace")
                    if len(text) > 200_000:
                        text = text[:200_000] + "...[truncated]"
                    entry["response_body"] = text
            except Exception as e:
                entry["response_body_error"] = str(e)

            captured.append(entry)
        except Exception as e:
            captured.append({"_error": str(e), "url": getattr(req, "url", None)})

    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
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
        _install_bandwidth_blocker(context)

        # Pasang listener ke context (akan otomatis ke semua page & frame)
        context.on("request", _on_request)
        context.on("response", _on_response)

        page = context.new_page()
        page.goto(URL, wait_until="commit", timeout=60000)

        email_input, email_frame = find_email_input(page, total_timeout=20)
        if email_input is None:
            print("ERROR: email input tidak ditemukan")
            browser.close()
            sys.exit(1)
        email_input.fill(email)
        print(f"Email diisi: {email}")

        sitekey, captcha_url = find_recaptcha_sitekey(page)
        if not sitekey:
            print("ERROR: sitekey tidak ditemukan")
            browser.close()
            sys.exit(1)
        if email_frame is not None and email_frame.url:
            captcha_url = email_frame.url
        print(f"Sitekey: {sitekey}")
        print(f"Captcha URL: {captcha_url}")

        token = solve_recaptcha_v2(
            api_key=capsolver_api_key,
            website_url=captcha_url,
            website_key=sitekey,
            timeout=int(os.getenv("CAPSOLVER_TIMEOUT", "180")),
            proxy_config=proxy_config,
        )
        print("Token CapSolver diterima")

        inject_recaptcha_token(page, token)

        # Tandai dengan timestamp untuk memudahkan filtering log
        marker = time.time()
        print(f"--- MARKER {marker} (sebelum submit) ---")
        for entry in captured:
            entry.setdefault("phase", "before_submit")

        click_submit_button(email_frame or page)
        print("Tombol submit diklik. Menunggu kode...")

        code = wait_for_code(email_frame or page, timeout=60)
        print(f"Kode hasil: {code}")

        # Tandai semua entri yang muncul setelah marker
        for entry in captured:
            if "phase" not in entry:
                entry["phase"] = "after_submit"

        # Tunggu sebentar supaya semua response tertangkap
        time.sleep(2)
        browser.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(captured, f, indent=2, ensure_ascii=False)

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    print(f"\n[OK] Disimpan {len(captured)} request POST/PUT/PATCH ke {OUTPUT_FILE}")
    print(f"[OK] Log lengkap semua request ke {SUMMARY_FILE}")
    print("\nKirim isi network.json ke saya untuk lanjut ke step 2.")


if __name__ == "__main__":
    main()
