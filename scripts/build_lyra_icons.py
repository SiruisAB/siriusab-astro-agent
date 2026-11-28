"""Build local application icons from the credited Lyra photograph (Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


ASSETS = Path(__file__).resolve().parents[1] / "workbench_ui" / "assets"
CROP = (362, 22, 477, 137)
STARS = [(407, 52), (409, 71), (405, 90), (437, 117), (440, 101)]


def build():
    size = 1024
    scale = size / (CROP[2] - CROP[0])
    with Image.open(ASSETS / "lyra-original.jpg") as source:
        image = source.convert("RGB").crop(CROP).resize((size, size), Image.Resampling.LANCZOS)
    image = ImageEnhance.Brightness(image).enhance(1.75).convert("RGBA")
    points = [((x - CROP[0]) * scale, (y - CROP[1]) * scale) for x, y in STARS]
    lines = [points[0], points[1], points[2], points[3], points[4], points[1]]
    glow = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(glow)
    draw.line(lines, fill=(154, 171, 255, 165), width=14, joint="curve")
    for index, (x, y) in enumerate(points):
        radius = 23 if index == 0 else 12
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(173, 189, 255, 230))
    image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(15)))
    detail = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(detail)
    draw.line(lines, fill=(183, 191, 255, 240), width=6, joint="curve")
    for index, (x, y) in enumerate(points):
        radius = 10 if index == 0 else 6
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(245, 246, 255, 255))
    x, y = points[0]
    draw.line((x - 22, y, x + 22, y), fill=(226, 233, 255, 245), width=3)
    draw.line((x, y - 22, x, y + 22), fill=(226, 233, 255, 245), width=3)
    image = Image.alpha_composite(image, detail)
    thumbnails = {}
    for pixels in (16, 24, 32, 48, 64, 128, 180, 192, 256, 512):
        preview = image.resize((pixels * 4, pixels * 4), Image.Resampling.LANCZOS)
        if pixels <= 64:
            # Keep the constellation recognizable in a 16px favicon or 38px sidebar.
            detail = ImageDraw.Draw(preview)
            positions = [(x * pixels * 4 / size, y * pixels * 4 / size) for x, y in points]
            path = [positions[0], positions[1], positions[2], positions[3], positions[4], positions[1]]
            detail.line(path, fill=(181, 190, 255, 255), width=3, joint="curve")
            for index, (x, y) in enumerate(positions):
                radius = 5 if index == 0 else 3
                detail.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(246, 248, 255, 255))
        thumbnails[pixels] = preview.resize((pixels, pixels), Image.Resampling.LANCZOS)
    for pixels in (32, 64, 180, 192, 512):
        thumbnails[pixels].save(ASSETS / f"lyra-{pixels}.png", optimize=True)
    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    thumbnails[256].save(ASSETS / "lyra.ico", sizes=[(p, p) for p in ico_sizes],
                         append_images=[thumbnails[p] for p in ico_sizes[:-1]])


if __name__ == "__main__":
    build()
