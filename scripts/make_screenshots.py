"""
Regenerate the README screenshots from the offline demo.

  docs/comps_sheet.png  - the Comps sheet of a freshly written workbook, rendered through
                          LibreOffice (xlsx -> PDF) and pypdfium2 (PDF page -> PNG)
  docs/app.png          - the Streamlit app after an offline run, captured with Playwright

Requirements beyond requirements.txt:  pip install pypdfium2 playwright   (+ a Chromium
binary: `playwright install chromium`, or point CHROME_PATH at an existing one), and
LibreOffice (`soffice`) on PATH.

Usage:  python scripts/make_screenshots.py [--skip-app] [--skip-excel]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS = PROJECT_ROOT / "docs"
sys.path.insert(0, str(PROJECT_ROOT))


def excel_screenshot(out: Path) -> None:
    from PIL import Image, ImageChops
    import pypdfium2 as pdfium

    from compsai.pipeline import run_pipeline

    with tempfile.TemporaryDirectory(prefix="compsai-shot-") as tmp:
        tmp_path = Path(tmp)
        result = run_pipeline(["FIXA", "FIXB", "FIXC"], peer_set_name="demo", target="FIXA",
                              offline=True, with_commentary=True, out_dir=tmp_path)
        xlsx = result.xlsx_path
        env = dict(os.environ, SAL_USE_VCLPLUGIN="svp")
        profile = tmp_path / "lo_profile"
        subprocess.run(
            ["soffice", "--headless", "--norestore", f"-env:UserInstallation={profile.as_uri()}",
             "--convert-to", "pdf", "--outdir", str(tmp_path), str(xlsx)],
            check=True, env=env, capture_output=True, timeout=180,
        )
        pdf = pdfium.PdfDocument(str(xlsx.with_suffix(".pdf")))
        img = pdf[0].render(scale=2.5).to_pil().convert("RGB")  # page 1 = the Comps sheet
        bbox = ImageChops.difference(img, Image.new("RGB", img.size, (255, 255, 255))).getbbox()
        if bbox:
            pad = 30
            img = img.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                            min(img.width, bbox[2] + pad), min(img.height, bbox[3] + pad)))
        img.save(out, optimize=True)
        print(f"wrote {out} {img.size}")


def app_screenshot(out: Path, port: int = 8765) -> None:
    from playwright.sync_api import sync_playwright

    env = dict(os.environ, COMPSAI_OFFLINE="", BROWSER="none")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT_ROOT / "app" / "streamlit_app.py"),
         "--server.headless", "true", "--server.port", str(port), "--browser.gatherUsageStats", "false"],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(4)
        with sync_playwright() as p:
            chrome = os.environ.get("CHROME_PATH")
            browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1100})
            page.goto(f"http://localhost:{port}", wait_until="networkidle", timeout=60_000)
            page.get_by_label("Peer tickers (comma-separated)").fill("FIXA, FIXB, FIXC")
            page.get_by_label("Target ticker (gets the football field)").fill("FIXA")
            page.locator("label", has_text="Offline demo (bundled").first.click()
            page.get_by_role("button", name="Run").click()
            page.get_by_role("button", name="Download Excel comps sheet").wait_for(timeout=120_000)
            time.sleep(1.5)  # let the Altair chart finish rendering
            page.screenshot(path=str(out), full_page=True)
            browser.close()
        print(f"wrote {out}")
    finally:
        proc.terminate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-app", action="store_true")
    parser.add_argument("--skip-excel", action="store_true")
    args = parser.parse_args()
    DOCS.mkdir(exist_ok=True)
    if not args.skip_excel:
        if shutil.which("soffice") is None:
            print("soffice not found; skipping the Excel screenshot")
        else:
            excel_screenshot(DOCS / "comps_sheet.png")
    if not args.skip_app:
        app_screenshot(DOCS / "app.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
