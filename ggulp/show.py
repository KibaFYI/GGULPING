r"""The run screen: the wolfgon, one word per step, and a pile of skulls (one per quant).

    look       Q4_K_M  4.9 GB
    savor      fits, 61 GB free
    gulp
    digesting  ████████████░░░░░░░░░░░░░░░░   43%  Q4_K_M
    burrp

      _____
     /     \
    | () () |   Q4_K_M
     \  ^  /    done
      |||||

     .-.       .-.       .-.
    (o o)     (o o)     (o o)
     |=|       |=|       |=|
     BF16      Q8_0      Q6_K

In a terminal it redraws in place. Without one (docker logs) it prints one line per step.
Everything also goes into the meal's log."""

from __future__ import annotations

import os
import sys

from rich.console import Group
from rich.live import Live
from rich.text import Text

from forge import art, ui

from . import talk

BIG_SKULL = ["  _____  ", " /     \\ ", "| () () |", " \\  ^  / ", "  |||||  "]
SMALL_SKULL = [" .-. ", "(o o)", " |=| "]
COL = 10      # width of one skull in the pile
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
        self.bar = (word, min(done, total), total, label)
        quarter = int(4 * min(done, total) / total)
        if quarter != self._printed_quarter:  # the log / docker logs get 0 25 50 75 100 %
            self._printed_quarter = quarter
            self._out(f"{word:<{WORD}} {25 * quarter:>3}%  {label}".rstrip())

    def end_bar(self) -> None:
        self.bar = None
        self._printed_quarter = -1

    def burp(self, quant: str) -> None:
        if self.steps and self.steps[-1][0] == talk.GULP:  # keep the finished bar on screen
            self.steps.append((talk.DIGEST, "█" * BAR + f"  100%  {quant}"))
        self.end_bar()
        self.steps.append((talk.BURP, ""))
        self.skull = quant
        self.pile.append(quant)
        self.done += 1
        lines = [f"{talk.BURP}"] + [
            row + ("   " + quant if i == 2 else "   " + talk.DONE if i == 3 else "") for i, row in enumerate(BIG_SKULL)
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
            body.append(line)

        if self.skull:
            body.append(Text(""))
            for i, row in enumerate(BIG_SKULL):
                line = Text(pad + "  ", no_wrap=True, overflow="crop")
                line.append(row, style=f"bold {ui.HOT}")
                if i == 2:
                    line.append(f"   {self.skull}", style=f"bold {ui.HOT}")
                elif i == 3:
                    line.append(f"   {talk.DONE}", style=ui.PURPLE)
                body.append(line)

        notes = [Text(pad + n, style=ui.DIM, no_wrap=True, overflow="crop") for n in self.notes]
        per_row = max(1, block // COL)
        rows = [self.pile[i:i + per_row] for i in range(0, len(self.pile), per_row)]
        used = len(body) + len(notes) + 2
        art_h = art.height() + 1
        show_art = w >= art.width() + 2 and h >= used + 4 * min(len(rows), 1) + art_h
        room_for_rows = max(0, (h - used - (art_h if show_art else 0)) // 4)
        if rows:
            body.append(Text(""))
            for row in rows[-room_for_rows:] if room_for_rows else []:
                for r in range(len(SMALL_SKULL) + 1):
                    line = Text(pad, no_wrap=True, overflow="crop")
                    for q in row:
                        cell = SMALL_SKULL[r] if r < len(SMALL_SKULL) else q
                        line.append(cell.center(COL)[:COL], style=f"bold {ui.HOT}" if r < len(SMALL_SKULL) else ui.PURPLE)
                    body.append(line)
        if notes:
            body.append(Text(""))
            body += notes

        if show_art:
            top = [art.render_line(line, max(0, (w - art.width()) // 2)) for line in art.lines()]
            return Group(*top, Text(""), *body)
        return Group(*body)
