from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(size: int) -> Image.Image:
    scale = size / 256
    image = Image.new("RGBA", (size, size), (7, 11, 20, 255))
    draw = ImageDraw.Draw(image)

    def box(coords, radius, fill, outline=None, width=1):
        draw.rounded_rectangle(
            tuple(int(value * scale) for value in coords),
            radius=int(radius * scale), fill=fill, outline=outline,
            width=max(1, int(width * scale)))

    box((18, 18, 238, 238), 50, (13, 21, 35, 255),
        (32, 212, 197, 255), 7)
    box((53, 92, 203, 190), 17, (20, 39, 53, 255),
        (71, 236, 219, 255), 8)
    box((44, 57, 210, 105), 10, (29, 76, 82, 255),
        (71, 236, 219, 255), 7)
    draw.polygon(
        [(int(50 * scale), int(57 * scale)),
         (int(88 * scale), int(57 * scale)),
         (int(66 * scale), int(101 * scale)),
         (int(44 * scale), int(101 * scale))],
        fill=(55, 231, 215, 255))
    draw.polygon(
        [(int(111 * scale), int(57 * scale)),
         (int(151 * scale), int(57 * scale)),
         (int(128 * scale), int(101 * scale)),
         (int(89 * scale), int(101 * scale))],
        fill=(55, 231, 215, 255))
    draw.polygon(
        [(int(174 * scale), int(57 * scale)),
         (int(210 * scale), int(57 * scale)),
         (int(210 * scale), int(101 * scale)),
         (int(151 * scale), int(101 * scale))],
        fill=(55, 231, 215, 255))
    draw.polygon(
        [(int(112 * scale), int(120 * scale)),
         (int(112 * scale), int(166 * scale)),
         (int(155 * scale), int(143 * scale))],
        fill=(244, 247, 251, 255))
    return image


target = Path(__file__).resolve().parents[1] / "assets" / "media-editor.ico"
target.parent.mkdir(parents=True, exist_ok=True)
make_icon(256).save(
    target, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                (48, 48), (64, 64), (128, 128), (256, 256)])
