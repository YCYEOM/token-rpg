"""앱 아이콘(macos/AppIcon.icns)을 그린다. 손으로 다시 돌릴 때만 쓴다 — Pillow 가 필요하고 CI 는 결과물만 쓴다.
사용: python3 scripts/make-icon.py   (macOS: sips · iconutil)
그림: 어두운 판 위의 육각 금색 토큰 보석, 가운데 검 문양을 새겼다. 두 배(2048)로 그려 줄인다 — 가장자리가 매끄럽다."""
import math, os, subprocess, tempfile
from PIL import Image, ImageDraw, ImageFilter, ImageChops

S = 2048
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vgrad(top, bot):
    g = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(g)
    for y in range(S):
        d.line([(0, y), (S, y)], fill=lerp(top, bot, y / (S - 1)) + (255,))
    return g


def masked(layer, mask):
    out = layer.copy()
    out.putalpha(ImageChops.multiply(layer.split()[3], mask))
    return out


def solid(rgb):
    return Image.new("RGBA", (S, S), rgb + (255,))


def draw():
    # 판: macOS 아이콘 격자(1024 안에 824 몸통)를 두 배로
    inset, rad = 200, 370
    plate = Image.new("L", (S, S), 0)
    ImageDraw.Draw(plate).rounded_rectangle([inset, inset, S - inset, S - inset], rad, fill=255)
    bg = vgrad((24, 30, 48), (10, 12, 20))
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse([S // 2 - 700, S // 2 - 700, S // 2 + 700, S // 2 + 700], fill=90)
    bg = Image.alpha_composite(bg, masked(solid((255, 196, 90)), glow.filter(ImageFilter.GaussianBlur(315))))
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, (0, 0), plate)
    edge = Image.new("L", (S, S), 0)
    ImageDraw.Draw(edge).rounded_rectangle([inset, inset, S - inset, S - inset], rad, outline=40, width=6)
    fade = vgrad((255, 255, 255), (0, 0, 0)).convert("L")          # 위쪽 테두리만 빛난다
    img = Image.alpha_composite(img, masked(solid((255, 255, 255)), ImageChops.multiply(edge, fade)))

    # 육각 보석: 바깥 여섯 면은 빛 방향(왼쪽 위)에 따라 밝기가 다르고, 안쪽 면은 세로 그라데이션
    cx, cy, R = S // 2, S // 2 + 20, 600
    hexpt = lambda r: [(cx + r * math.cos(math.radians(90 + 60 * i)), cy - r * math.sin(math.radians(90 + 60 * i))) for i in range(6)]
    outer, inner = hexpt(R), hexpt(R * 0.58)
    hexm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hexm).polygon(outer, fill=255)
    sh = Image.new("L", (S, S), 0)
    sh.paste(hexm, (0, 40))
    img = Image.alpha_composite(img, masked(Image.new("RGBA", (S, S), (0, 0, 0, 170)), sh.filter(ImageFilter.GaussianBlur(50))))
    shades = [(255, 232, 160), (240, 196, 92), (196, 138, 38), (160, 104, 24), (206, 148, 48), (250, 214, 120)]
    d = ImageDraw.Draw(img)
    for i in range(6):
        j = (i + 1) % 6
        d.polygon([outer[i], outer[j], inner[j], inner[i]], fill=shades[i] + (255,))
    face = Image.new("L", (S, S), 0)
    ImageDraw.Draw(face).polygon(inner, fill=255)
    img = Image.alpha_composite(img, masked(vgrad((255, 214, 110), (214, 152, 50)), face))

    # 검 문양 (음각): 어두운 홈 + 아래쪽 가장자리에 밝은 빛 한 줄
    eng = Image.new("L", (S, S), 0)
    e = ImageDraw.Draw(eng)
    e.polygon([(cx - 34, cy + 150), (cx - 34, cy - 170), (cx, cy - 280), (cx + 34, cy - 170), (cx + 34, cy + 150)], fill=255)
    e.rounded_rectangle([cx - 150, cy + 140, cx + 150, cy + 186], 23, fill=255)
    e.rounded_rectangle([cx - 24, cy + 180, cx + 24, cy + 290], 12, fill=255)
    e.ellipse([cx - 40, cy + 280, cx + 40, cy + 360], fill=255)
    lo = Image.new("L", (S, S), 0)
    lo.paste(eng, (0, 8))
    img = Image.alpha_composite(img, masked(solid((255, 245, 210)), ImageChops.subtract(lo, eng)))
    img = Image.alpha_composite(img, masked(solid((120, 74, 14)), eng))
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    out = os.path.join(ROOT, "macos")
    png = os.path.join(out, "AppIcon.png")
    draw().save(png)
    with tempfile.TemporaryDirectory() as d:
        ics = os.path.join(d, "AppIcon.iconset")
        os.makedirs(ics)
        for size in (16, 32, 128, 256, 512):
            for k in (1, 2):
                px = size * k
                name = f"icon_{size}x{size}{'@2x' if k == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), png, "--out", os.path.join(ics, name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", ics, "-o", os.path.join(out, "AppIcon.icns")], check=True)
    print("macos/AppIcon.png · macos/AppIcon.icns")


if __name__ == "__main__":
    main()
