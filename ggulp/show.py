r'''The run screen: the wolfgon, one word per step, and one skull with every quant eaten so far.

    look       Q4_K_M  4.9 GB
    savor      fits, 61 GB free
    gulp
    digesting  ████████████████████████████  100%  Q4_K_M
    burrp

          .-"""""-.
        .'         '.     Q4_K_M done
       /  .-.   .-.  \
      |  (   ) (   )  |  - BF16 | Q8_0 | Q6_K | Q5_1 | Q5_K_M | Q5_K_S
      |   '-'   '-'   |    Q5_0 | Q4_1 | Q4_K_M
       \      ^      /
        '. |_|_|_| .'
          '-------'

In a terminal it redraws in place. Without one (docker logs) it prints one line per step.
Everything also goes into the meal's log.'''

from __future__ import annotations

import os
import sys
import time

from rich.console import Group
from rich.live import Live
from rich.text import Text

from forge import art, ui

from . import talk

SKULL = [
    '      .-"""""-.      ',
    "    .'         '.    ",
    "   /  .-.   .-.  \\   ",
    "  |  (   ) (   )  |  ",
    "  |   '-'   '-'   |  ",
    "   \\      ^      /   ",
    "    '. |_|_|_| .'    ",
    "      '-------'      ",
]
SKULL_W = len(SKULL[0])
SAYS_ROW, LIST_ROW = 1, 3  # "Q4_K_M done" next to the forehead, the eaten list next to the eyes
BAR = 28      # width of the progress bar
WORD = 10     # width of the step word column


class Show:
    def __init__(self, name: str, total: int, log_path=None, live: bool | None = None):
        self.name, self.total = name, total
        if live is None:
            live = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.live = live
        self.log_path = log_path
        self.done = 0
        self.absorbed = 0
        self.steps: list[tuple[str, str]] = []
        self.bar: tuple[str, int, int, str] | None = None
        self.skull: str | None = None
        self.pile: list[str] = []
        self.notes: list[str] = []
        self._live: Live | None = None
        self._printed_quarter = -1
        self._bar_word: str | None = None
        self._bar_t0 = 0.0
        self._bar_done = 0
        self.eta = ""

    # ── start / stop ───────────────────────────────────────────────────────────────

    def __enter__(self) -> "Show":
        if self.live:
            self._live = Live(console=ui.console, get_renderable=self.render, screen=True,
                              refresh_per_second=6, transient=True)
            self._live.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def _out(self, text: str, plain_lines: list[str] | None = None) -> None:
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[ggulp] {text}\n")
            except OSError:
                pass
        if not self.live:
            for line in plain_lines if plain_lines is not None else [text]:
                ui.console.print(line, markup=False, highlight=False)

    # ── what the meal calls ────────────────────────────────────────────────────────

    def fresh(self) -> None:
        """A new cycle: clear the step lines (the pile stays)."""
        self.steps, self.bar, self.skull = [], None, None

    def step(self, word: str, detail: str = "") -> None:
        self.steps.append((word, detail))
        self._out(f"{word:<{WORD}} {detail}".rstrip())

    def progress(self, word: str, done: int, total: int, label: str = "") -> None:
        total = max(total, 1)
        done = min(done, total)
        now = time.monotonic()
        if word != self._bar_word or done < self._bar_done:  # a new bar: start its clock
            self._bar_word, self._bar_t0 = word, now
        self._bar_done = done
        self.eta = time_left(now - self._bar_t0, done, total)
        self.bar = (word, done, total, label)
        quarter = int(4 * done / total)
        if quarter != self._printed_quarter:  # the log / docker logs get 0 25 50 75 100 %
            self._printed_quarter = quarter
            self._out(f"{word:<{WORD}} {25 * quarter:>3}%  {label}  {self.eta}".rstrip())

    def end_bar(self) -> None:
        self.bar = None
        self._printed_quarter = -1
        self._bar_word, self._bar_done, self.eta = None, 0, ""

    def burp(self, quant: str) -> None:
        if self.steps and self.steps[-1][0] == talk.GULP:  # keep the finished bar on screen
            self.steps.append((talk.DIGEST, "█" * BAR + f"  100%  {quant}"))
        self.end_bar()
        self.steps.append((talk.BURP, ""))
        self.skull = quant
        self.pile.append(quant)
        self.done += 1
        lines = [f"{talk.BURP}"] + [
            row.rstrip() + (f"   {quant} {talk.DONE}" if i == SAYS_ROW else "") for i, row in enumerate(SKULL)
        ]
        self._out(f"{talk.BURP:<{WORD}} {quant} {talk.DONE}", lines)

    def full(self) -> None:
        self.fresh()
        self.steps.append((talk.FULL, ""))
        self._out(talk.FULL)

    def rested(self, absorbed: list[str]) -> None:
        self.absorbed += len(absorbed)
        self.pile = [q for q in self.pile if q not in absorbed]
        self.fresh()
        self.step(talk.RESTED)

    def note(self, text: str) -> None:
        self.notes = (self.notes + [text])[-3:]
        self._out(text)

    # ── drawing ─────────────────────────────────────────────────────────────────────

    def render(self) -> Group:
        w, h = ui.console.size
        block = min(max(40, w - 4), 76)
        pad = " " * max(0, (w - block) // 2)
        body: list[Text] = []

        title = f"{talk.TITLE}   {self.name}   {self.done}/{self.total}"
        if self.absorbed:
            title += f"   {self.absorbed} on HF"
        body.append(Text(title.center(w)[:w], style=f"bold {ui.PURPLE}", no_wrap=True))
        body.append(Text(""))

        for word, detail in self.steps:
            line = Text(pad, no_wrap=True, overflow="crop")
            if word == talk.FULL:
                line.append(word, style=f"bold {ui.HOT}")
            else:
                line.append(f"{word:<{WORD}} ", style=f"bold {ui.HOT}")
                line.append(detail, style=ui.PURPLE)
            body.append(line)
        if self.bar:
            word, done, total, label = self.bar
            filled = int(BAR * done / total)
            line = Text(pad, no_wrap=True, overflow="crop")
            line.append(f"{word:<{WORD}} ", style=f"bold {ui.HOT}")
            line.append("█" * filled, style=ui.HOT)
            line.append("░" * (BAR - filled), style=ui.DEEP)
            line.append(f"  {int(100 * done / total):>3}%  {label}", style=ui.PURPLE)
            if self.eta:
                line.append(f"  {self.eta}", style=ui.DIM)
            body.append(line)

        notes = [Text(pad + n, style=ui.DIM, no_wrap=True, overflow="crop") for n in self.notes]
        if self.pile or self.skull:  # the one skull: says what just went down, lists everything eaten so far
            body.append(Text(""))
            listed = pipe_lines(self.pile, max(20, block - SKULL_W - 6))
            for r in range(max(len(SKULL), LIST_ROW + len(listed))):
                line = Text(pad, no_wrap=True, overflow="crop")
                line.append_text(skull_row(r))
                if r == SAYS_ROW and self.skull:
                    line.append(f"  {self.skull} {talk.DONE}", style=f"bold {ui.HOT}")
                k = r - LIST_ROW
                if 0 <= k < len(listed):
                    line.append(" - " if k == 0 else "   ", style=f"bold {ui.HOT}")
                    line.append(listed[k], style=ui.PURPLE)
                body.append(line)
        if notes:
            body.append(Text(""))
            body += notes

        art_h = art.height() + 1
        show_art = w >= art.width() + 2 and h >= len(body) + art_h
        if show_art:
            top = [art.render_line(line, max(0, (w - art.width()) // 2)) for line in art.lines()]
            return Group(*top, Text(""), *body)
        return Group(*body)


def time_left(elapsed: float, done: int, total: int) -> str:
    """'about 12 min left' from how fast the bar has moved so far."""
    if done <= 0 or done >= total or elapsed < 3:
        return ""
    seconds = elapsed * (total - done) / done
    if seconds < 60:
        return "under a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    return f"about {minutes // 60} h {minutes % 60} min left"


def skull_row(r: int) -> Text:
    """One row of the skull: bone pale, eye sockets and nose hot, teeth purple."""
    row = SKULL[r] if r < len(SKULL) else " " * SKULL_W
    text = Text(no_wrap=True)
    for c, ch in enumerate(row):
        if 2 <= r <= 4 and (5 <= c <= 9 or 11 <= c <= 15) or (r == 5 and c == 10):
            style = f"bold {ui.HOT}"
        elif r == 6 and 7 <= c <= 13:
            style = ui.PURPLE
        else:
            style = f"bold {ui.SOFT}"
        text.append(ch, style=style)
    return text


def pipe_lines(items: list[str], width: int) -> list[str]:
    """['BF16', 'Q8_0', ...] -> ['BF16 | Q8_0 | ...', 'Q4_K_M | ...'], each line at most `width` wide."""
    lines, cur = [], ""
    for item in items:
        nxt = f"{cur} | {item}" if cur else item
        if cur and len(nxt) > width:
            lines.append(cur)
            cur = item
        else:
            cur = nxt
    if cur:
        lines.append(cur)
    return lines
