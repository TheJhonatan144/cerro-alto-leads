import sys
from playwright.sync_api import sync_playwright
from pathlib import Path
AQUI = Path(__file__).resolve().parent
out = AQUI / "capturas"; out.mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium") if Path("/opt/pw-browsers/chromium").is_file() else p.chromium.launch()
    for nombre, w, h, esquema in [("escritorio_claro", 1280, 900, "light"), ("escritorio_oscuro", 1280, 900, "dark"), ("movil_claro", 375, 800, "light")]:
        ctx = b.new_context(viewport={"width": w, "height": h}, color_scheme=esquema, device_scale_factor=1 if w > 500 else 2)
        pg = ctx.new_page(); errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto((AQUI / "index.html").as_uri()); pg.wait_for_timeout(700)
        ancho = pg.evaluate("document.documentElement.scrollWidth")
        print(nombre, "scrollWidth", ancho, "errores:", errs)
        pg.screenshot(path=str(out / f"{nombre}.png"), full_page=True)
        ctx.close()
    b.close()
