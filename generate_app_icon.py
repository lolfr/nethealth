"""
generate_app_icon.py — Génère icon.icns pour l'app Finder à partir du concept
"jauges verticales" utilisé dans la menubar.

Sort dans `icon.icns` (à la racine du projet) prêt à être référencé dans setup.py.
Lance simplement : `python3 generate_app_icon.py`.

Le design : squircle dark bleu-marine avec un dégradé vertical doux + 5 barres
de jauges côte à côte, hauteurs croissantes (20% → 100%), colorées selon
la palette viridis daltonien-safe (bleu profond → jaune doré). Un petit
triangle blanc au-dessus de la 5e barre rappelle le marqueur "route active".

Pourquoi pas une icône statique d'antenne ou d'onde radio ? Parce que l'icône
de l'app doit refléter l'identité visuelle qu'on voit en permanence dans la
menubar. Quelqu'un qui ouvre le Finder reconnaît immédiatement les 5 barres.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


PROJECT_DIR = Path(__file__).resolve().parent
MASTER_SIZE = 1024
ICONSET_DIR = PROJECT_DIR / "icon.iconset"
ICNS_PATH = PROJECT_DIR / "icon.icns"


def _interpolate_color(health: float) -> tuple[int, int, int]:
    """Même palette que network_health.py : bleu profond → turquoise → jaune."""
    health = max(0.0, min(1.0, health))
    if health < 0.5:
        t = health * 2
        r = int(50 + (60 - 50) * t)
        g = int(40 + (170 - 40) * t)
        b = int(120 + (170 - 120) * t)
    else:
        t = (health - 0.5) * 2
        r = int(60 + (240 - 60) * t)
        g = int(170 + (210 - 170) * t)
        b = int(170 + (80 - 170) * t)
    return (r, g, b)


def _vertical_gradient(size: int, top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    """Image RGBA size×size remplie d'un dégradé vertical."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    return img


def render_icon(size: int = MASTER_SIZE) -> Image.Image:
    """Rend l'icône à la taille demandée."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Squircle background — corner radius ~ 22 % du côté, à l'œil macOS-like.
    radius = int(size * 0.225)
    grad = _vertical_gradient(size, (32, 42, 70), (14, 20, 40))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(grad, (0, 0), mask)

    # Léger éclat top : un highlight très ténu sur le tiers supérieur.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (-size * 0.25, -size * 0.6, size * 1.25, size * 0.5),
        fill=(255, 255, 255, 22),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size * 0.04))
    glow_masked = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_masked.paste(glow, (0, 0), mask)
    bg = Image.alpha_composite(bg, glow_masked)

    img = Image.alpha_composite(img, bg)

    # --- Jauges ---
    draw = ImageDraw.Draw(img)
    n = 5
    pad_x = int(size * 0.20)
    pad_top = int(size * 0.20)
    pad_bot = int(size * 0.22)
    band_h = size - pad_top - pad_bot
    base_y = size - pad_bot
    top_y = pad_top

    gap = int(size * 0.022)
    total_w = size - 2 * pad_x
    bar_w = (total_w - gap * (n - 1)) // n
    block_w = bar_w * n + gap * (n - 1)
    x0 = (size - block_w) // 2

    rail_color = (255, 255, 255, 32)
    bar_radius = max(2, int(bar_w * 0.22))

    # Rail de fond pour chaque barre
    for i in range(n):
        bx = x0 + i * (bar_w + gap)
        draw.rounded_rectangle(
            (bx, top_y, bx + bar_w, base_y),
            radius=bar_radius, fill=rail_color,
        )

    # Barres : hauteurs croissantes, couleurs viridis correspondantes
    for i in range(n):
        bx = x0 + i * (bar_w + gap)
        h = (i + 1) / n  # 0.2, 0.4, 0.6, 0.8, 1.0
        bar_h = int(band_h * h)
        y_top = base_y - bar_h
        color = _interpolate_color(h) + (255,)
        draw.rounded_rectangle(
            (bx, y_top, bx + bar_w, base_y),
            radius=bar_radius, fill=color,
        )

    # Marqueur "route active" : petit triangle blanc au-dessus de la 5e barre
    last_cx = x0 + (n - 1) * (bar_w + gap) + bar_w / 2
    tri_tip_y = top_y - int(size * 0.012)
    tri_half = int(size * 0.028)
    tri_h = int(size * 0.034)
    draw.polygon(
        [
            (last_cx - tri_half, tri_tip_y - tri_h),
            (last_cx + tri_half, tri_tip_y - tri_h),
            (last_cx, tri_tip_y),
        ],
        fill=(255, 255, 255, 255),
    )

    return img


def build_iconset(master: Image.Image, out_dir: Path) -> None:
    """Crée le dossier .iconset avec toutes les tailles requises par macOS."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    sizes = [16, 32, 128, 256, 512]
    for s in sizes:
        master.resize((s, s), Image.LANCZOS).save(out_dir / f"icon_{s}x{s}.png")
        master.resize((s * 2, s * 2), Image.LANCZOS).save(
            out_dir / f"icon_{s}x{s}@2x.png"
        )
    # Garde aussi un master 1024 nu, utile pour la prévisu et les Lo-fi tools.
    master.save(out_dir.parent / "icon_1024.png")


def build_icns(iconset_dir: Path, icns_path: Path) -> None:
    """Convertit le .iconset en .icns via iconutil (macOS only)."""
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )


def main() -> None:
    print(f"→ rendu master {MASTER_SIZE}×{MASTER_SIZE}…")
    master = render_icon(MASTER_SIZE)
    print(f"→ génération iconset dans {ICONSET_DIR}…")
    build_iconset(master, ICONSET_DIR)
    print(f"→ build icon.icns via iconutil…")
    build_icns(ICONSET_DIR, ICNS_PATH)
    print(f"OK → {ICNS_PATH}")
    print("Pour activer : décommenter `iconfile` dans setup.py + rebuild.")


if __name__ == "__main__":
    main()
