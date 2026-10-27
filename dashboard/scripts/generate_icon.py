from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
DESKTOP.mkdir(exist_ok=True)

size = 512
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((12, 12, 500, 500), radius=116, fill="#0e4d3b")
draw.rounded_rectangle((30, 30, 482, 482), radius=100, outline="#2d6855", width=3)

# Three abstract candlesticks echo the dashboard's market-research purpose.
for x, top, bottom, body_top, body_bottom in [
    (158, 142, 358, 194, 298),
    (256, 112, 326, 154, 246),
    (354, 176, 394, 226, 336),
]:
    draw.line((x, top, x, bottom), fill="#a9c3b6", width=12)
    draw.rounded_rectangle((x - 32, body_top, x + 32, body_bottom), radius=12, fill="#f2f8f4")

draw.line((110, 390, 402, 390), fill="#5f8f7a", width=5)
image.save(DESKTOP / "icon.png")
image.save(DESKTOP / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(DESKTOP / "icon.png")
