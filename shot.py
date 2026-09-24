"""
Veb-sahifa skrinshoti.

Bot ichidan:  await take(url, out, full=False, mobile=False)
Terminal/Claude'dan:  python shot.py URL chiqish.png [--full] [--mobile] [--wait 3000]

Avval Playwright + kompyuterdagi Edge/Chrome (qo'shimcha brauzer yuklab olinmaydi),
bo'lmasa Edge/Chrome'ning o'z headless --screenshot rejimi ishlatiladi.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DESKTOP = (1440, 900)
MOBILE = (390, 844)

_BROWSER_CANDIDATES = [
    os.getenv("BROWSER_PATH", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    shutil.which("msedge") or "",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def find_browser() -> str | None:
    for p in _BROWSER_CANDIDATES:
        if p and Path(p).is_file():
            return p
    return None


async def _take_playwright(url: str, out: Path, full: bool, mobile: bool, wait_ms: int) -> None:
    from playwright.async_api import async_playwright

    w, h = MOBILE if mobile else DESKTOP
    async with async_playwright() as p:
        browser = None
        errors = []
        for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {"executable_path": find_browser()}, {}):
            if "executable_path" in kw and not kw["executable_path"]:
                continue
            try:
                browser = await p.chromium.launch(headless=True, **kw)
                break
            except Exception as e:  # keyingi variantni sinaymiz
                errors.append(str(e).splitlines()[0])
        if browser is None:
            raise RuntimeError("Brauzer topilmadi: " + " | ".join(errors))
        try:
            ctx = await browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=2 if mobile else 1,
                is_mobile=mobile,
                has_touch=mobile,
                ignore_https_errors=True,
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="load", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # xarita/websocket doim tarmoqda bo'lishi mumkin
            await page.wait_for_timeout(wait_ms)
            await page.screenshot(path=str(out), full_page=full)
        finally:
            await browser.close()


async def _take_cli(url: str, out: Path, full: bool, mobile: bool, wait_ms: int) -> None:
    exe = find_browser()
    if not exe:
        raise RuntimeError("Edge/Chrome topilmadi. .env da BROWSER_PATH ni ko'rsating.")
    w, h = MOBILE if mobile else DESKTOP
    if full:
        h = 3000
    with tempfile.TemporaryDirectory() as prof:
        args = [
            exe, "--headless=new", "--hide-scrollbars", "--no-first-run", "--ignore-certificate-errors",
            f"--user-data-dir={prof}", f"--window-size={w},{h}",
            f"--virtual-time-budget={max(wait_ms, 5000)}", f"--screenshot={out}", url,
        ]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, **kw
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except TimeoutError:
            proc.kill()
            raise RuntimeError("Brauzer 90 soniyada javob bermadi")
    if not out.is_file():
        raise RuntimeError("Skrinshot olinmadi: " + (err or b"").decode("utf-8", "replace")[-300:])


async def take(url: str, out: Path | str, full: bool = False, mobile: bool = False, wait_ms: int = 2500) -> Path:
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _take_playwright(url, out, full, mobile, wait_ms)
    except ImportError:
        await _take_cli(url, out, full, mobile, wait_ms)
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if len(args) < 1:
        print("Foydalanish: python shot.py URL [chiqish.png] [--full] [--mobile] [--wait=3000]")
        sys.exit(2)
    url = args[0]
    out = args[1] if len(args) > 1 else ".tg_outbox/screenshot.png"
    wait = next((int(f.split("=", 1)[1]) for f in flags if f.startswith("--wait=")), 2500)
    path = asyncio.run(take(url, out, "--full" in flags, "--mobile" in flags, wait))
    print(f"OK: {path}")


if __name__ == "__main__":
    main()
