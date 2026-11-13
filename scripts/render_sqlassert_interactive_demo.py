"""Build a standalone HTML walkthrough using output from the local sqlassert CLI."""

import argparse
from html import escape
from pathlib import Path
import re
import subprocess
import tempfile


CLI = Path(__file__).resolve().parents[1] / ".venv/bin/sqlassert"
STEPS = (
    ("Fan Trap", "fan-trap.sql", (7, 8), "Multiple refunds can make the same payment count more than once."),
    ("INNER JOIN", "inner-join.sql", (9,), "Every order has a customer, so the outer join can be simplified."),
    ("Redundant Join", "redundant-join.sql", (6,), "The coupon contributes no columns and cannot multiply orders."),
    ("Annotate", "inner-join.sql", (9,), "Save the proof as a marker, then check it in CI."),
)

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sqlassert interactive demo</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; background: #0b1119; color: #edf5fc; }
body { max-width: 80rem; margin: 0 auto; padding: clamp(1rem, 4vw, 3rem); }
h1 { margin: 0 0 .4rem; font-size: 2.6rem; }
header p { margin: 0 0 1.8rem; color: #b0c1d3; }
nav, footer { display: flex; flex-wrap: wrap; gap: .6rem; }
footer { justify-content: space-between; margin-top: 1.15rem; }
button { font: inherit; cursor: pointer; color: inherit; background: #162435; border: 1px solid #405771;
  border-radius: .45rem; padding: .6rem .9rem; }
button[aria-pressed="true"] { color: #07221b; background: #67e8cd; border-color: #67e8cd; }
button:focus-visible { outline: 3px solid #ffc979; outline-offset: 3px; }
button:disabled { opacity: .45; cursor: default; }
.columns { display: grid; grid-template-columns: 1.1fr 1fr; gap: 1rem; }
.panel { min-width: 0; border: 1px solid #2b4055; border-radius: .65rem; background: #111b28; overflow: hidden; }
h2 { font-size: 1.5rem; margin-bottom: .4rem; }
section > p { margin-top: 0; color: #b0c1d3; }
h3 { margin: 0; padding: .75rem 1rem; border-bottom: 1px solid #2b4055; color: #b0c1d3; font: 500 .95rem system-ui; }
pre { margin: 0; padding: .85rem 0; white-space: pre-wrap; overflow-wrap: anywhere;
  font: .92rem/1.65 ui-monospace, Menlo, monospace; tab-size: 4; }
.line { display: grid; grid-template-columns: 2ch 1fr; gap: .85rem; padding: 0 .9rem; }
.number { color: #9bb0c4; text-align: right; user-select: none; }
mark { color: #ffdb9e; background: #3d3022; border-left: 3px solid #ffc979; padding-left: calc(.9rem - 3px) !important; }
.output { padding: .85rem 1rem; }
.prompt, .added { color: #67e8cd; }
.removed { color: #ff9eaa; }
@media (max-width: 52rem) { .columns { grid-template-columns: 1fr; } }
</style>
<header><h1>sqlassert</h1><p>Simplify and catch SQL bugs before they ship</p></header>
<nav aria-label="Choose an example">%%BUTTONS%%</nav>
<main>%%SLIDES%%</main>
<footer aria-label="Walkthrough controls">
<button id="previous" type="button">← Previous</button>
<button id="next" type="button">Next →</button>
</footer>
<script>
const buttons = [...document.querySelectorAll("nav button")];
const slides = [...document.querySelectorAll("main section")];
const previous = document.querySelector("#previous");
const next = document.querySelector("#next");
let current = 0;
function show(index, focusButton) {
  current = index;
  slides.forEach((slide, i) => { slide.hidden = i !== index; });
  buttons.forEach((button, i) => { button.setAttribute("aria-pressed", i === index); });
  previous.disabled = index === 0;
  next.disabled = index === slides.length - 1;
  if (focusButton) buttons[index].focus();
}
buttons.forEach((button, index) => button.addEventListener("click", () => show(index, false)));
previous.addEventListener("click", () => show(current - 1, current === 1));
next.addEventListener("click", () => show(current + 1, current === slides.length - 2));
show(0, false);
</script>
</html>
"""


def capture(directory, *args):
    return subprocess.run([CLI, *args], cwd=directory, check=True, text=True, capture_output=True).stdout


def slide(directory, index, title, filename, highlights, caption):
    query = (directory / filename).read_text()
    lines = []
    for number, line in enumerate(query.splitlines(), start=1):
        tag = "mark" if number in highlights else "span"
        lines.append(f'<{tag} class="line"><span class="number" aria-hidden="true">{number}</span>'
                     f'<span>{escape(line) or " "}</span></{tag}>')
    if title == "Annotate":
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / filename
            copy.write_text(query)
            capture(scratch, "suggest", "--annotate", filename)
            annotated = copy.read_text().splitlines()[-1]
            check = capture(scratch, "check", filename).strip()
        command = f"suggest --annotate {filename}"
        result = (f'<span class="prompt">$ tail -1 {filename}</span>\n{escape(annotated)}\n\n'
                  f'<span class="prompt">$ sqlassert check {filename}</span>\n{escape(check)}')
    else:
        command = f"suggest {filename}"
        output = capture(directory, "suggest", filename).splitlines()
        line = next(line for line in output if f"  {title.lower()}  " in line.lower())
        _, location, _, label, message = re.split(r" {2,}", line, maxsplit=4)
        result = f"{escape(label)} · {escape(location)}\n\n{escape(message)}"
        for change in (line.strip() for line in output if line.startswith("    ")):
            if change.startswith(("-", "+")) and not change.startswith(("---", "+++")):
                style = "removed" if change.startswith("-") else "added"
                result += f'\n<span class="{style}">{escape(change)}</span>'
    return (f'<section id="step-{index}" aria-labelledby="title-{index}"{" hidden" if index else ""}>'
            f'<h2 id="title-{index}">{escape(title)}</h2><p>{escape(caption)}</p>'
            f'<div class="columns"><div class="panel"><h3>{filename}</h3><pre>{"".join(lines)}</pre></div>'
            '<div class="panel"><h3>Terminal output excerpt</h3><pre class="output">'
            f'<span class="prompt">$ sqlassert {escape(command)}</span>\n\n{result}</pre></div></div></section>')


def render(directory):
    buttons = "".join(f'<button type="button" aria-controls="step-{i}" aria-pressed="{str(i == 0).lower()}">'
                      f'{escape(step[0])}</button>' for i, step in enumerate(STEPS))
    slides = "\n".join(slide(directory, i, *step) for i, step in enumerate(STEPS))
    (directory / "sqlassert-demo.html").write_text(PAGE.replace("%%BUTTONS%%", buttons).replace("%%SLIDES%%", slides))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing the demo SQL files")
    render(parser.parse_args().directory.resolve())
