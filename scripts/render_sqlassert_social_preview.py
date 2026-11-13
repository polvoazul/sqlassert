"""Render the sqlassert GitHub social preview using a verified CLI example."""

import argparse
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont


CLI = Path(__file__).resolve().parents[1] / ".venv/bin/sqlassert"
MONO = "/System/Library/Fonts/Menlo.ttc"
SANS = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
BG, PANEL, BORDER = "#0b1119", "#111b28", "#2b4055"
WHITE, MUTED, TEAL = "#edf5fc", "#9bb0c4", "#67e8cd"
YELLOW, PINK = "#ffc979", "#ff9eaa"


def capture(directory, *args, expected_code=0):
    result = subprocess.run([CLI, *args], cwd=directory, text=True, capture_output=True)
    if result.returncode != expected_code:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout + result.stderr


def text(draw, position, value, size, color=WHITE, font=MONO):
    draw.text(position, value, font=ImageFont.truetype(font, size), fill=color, anchor="lt")


def terminal(draw, box, title):
    x, y, right, bottom = box
    draw.rounded_rectangle(box, radius=15, fill=PANEL, outline=BORDER, width=2)
    draw.line((x + 1, y + 48, right - 1, y + 48), fill=BORDER)
    for index, color in enumerate((PINK, YELLOW, TEAL)):
        cx = x + 22 + index * 19
        draw.ellipse((cx, y + 19, cx + 8, y + 27), fill=color)
    text(draw, (x + 89, y + 15), title, 16, MUTED)


def render(directory):
    marker = "/**ASSERT MANY TO ONE**/ JOIN refunds r ON r.order_id = o.id"
    query = (directory / "fan-trap.sql").read_text()
    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / "refund-check.sql").write_text(
            query.replace("JOIN refunds r ON r.order_id = o.id", marker)
        )
        failure = capture(scratch, "check", "--explain", "refund-check.sql", expected_code=1)
        for expected in ("error unproved-many-to-one-join", "Unique-set columns are not covered"):
            if expected not in failure:
                raise RuntimeError(f"Expected {expected!r} in check output:\n{failure}")

    image = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((64, 63, 126, 70), radius=3, fill=TEAL)
    text(draw, (64, 96), "sqlassert", 98, font=SANS)
    text(draw, (68, 216), "Simplify and catch SQL bugs before they ship", 44, font=SANS)
    terminal(draw, (64, 305, 1215, 594), "sqlassert  /  refund-check.sql")
    select = next(line for line in query.splitlines() if line.startswith("SELECT "))
    source = next(line for line in query.splitlines() if line.startswith("FROM "))
    text(draw, (96, 361), select, 23)
    text(draw, (96, 388), source, 23)
    text(draw, (96, 415), marker, 23, TEAL)
    text(draw, (96, 455), "$ sqlassert check --explain refund-check.sql", 24)
    text(draw, (96, 497), "Error: Couldn’t prove MANY TO ONE", 29, PINK, SANS)
    text(draw, (96, 544), "The join uses refunds.order_id, but only refunds.id is known to be unique.", 23, WHITE, SANS)
    image.save(directory / "social-preview.png", optimize=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing fan-trap.sql and receiving the preview")
    render(parser.parse_args().directory.resolve())
