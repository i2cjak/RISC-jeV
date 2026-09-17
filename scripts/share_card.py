"""Generate the static social preview from the site's own font and timing motif."""
from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
font = TTFont(root / "static/fonts/berkeley-mono-variable.woff2")
font.flavor = None
buffer = BytesIO()
font.save(buffer)
font_data = buffer.getvalue()


def face(size):
    return ImageFont.truetype(BytesIO(font_data), size)


image = Image.new("RGB", (1200, 630), "#080808")
draw = ImageDraw.Draw(image)
draw.text((60, 35), "Jev / SERV", font=face(24), fill="white")
large = face(124)
small = face(47)
draw.text((55, 295), "RISC-", font=large, fill="white", anchor="ls")
cursor = 55 + draw.textlength("RISC-", font=large)
draw.text((cursor, 295), "je", font=small, fill="white", anchor="ls")
cursor += draw.textlength("je", font=small) + 4
draw.text((cursor, 295), "V", font=large, fill="white", anchor="ls")
draw.text((63, 355), "Simulated RISC-V CPU", font=face(24), fill="#bcbcbc")
for row, label in enumerate(("CLK", "FETCH", "SERIAL")):
    top = 465 + row * 43
    draw.text((63, top), label, font=face(14), fill="#999999")
    points = []
    for step in range(48):
        level = step % 2 if row == 0 else (step % 13 == 0 if row == 1 else step % 20 < 16)
        x = 162 + step * 20
        y = top + (0 if level else 18)
        if points:
            points.append((x, points[-1][1]))
        points.extend(((x, y), (x + 20, y)))
    draw.line(points, fill="#dddddd", width=2)
image.save(root / "static/share.png", optimize=True)
