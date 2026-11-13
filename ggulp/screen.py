"""The setup screen: the wolfgon in the middle, as few questions as possible, then the meal.

HF token -> model -> name -> where it goes (you, an org, or typed) -> smart or dump.
Then one summary, Enter starts, and when it's done it's back to the start. cmd-exit leaves.
Everything else (which quants, imatrix, Discord, disk cap) comes from GGULP_* settings."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from forge import art, hardware, ui, upload

from . import hub, menu, talk
from .config import Job
from .gulp import Gulp, GulpError, Report
from .menu import gb

DISMISSED = Path(os.environ.get("GGULP_SETUP_DISMISSED", "/tmp/ggulp-setup-dismissed"))
SKIP = "__skip__"
OTHER = "__other__"
NS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
SAVED = ("model", "name", "namespace", "mode", "private")


def _mask(secret: str | None) -> str:
    if not secret:
        return "-"
    return secret[:6] + "..." + secret[-4:] if len(secret) > 12 else "..."


def _short(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc))[:180] or type(exc).__name__


@dataclass
class Answers:
    hf_token: str | None = None
    hf_user: str | None = None
    hf_orgs: list[str] = field(default_factory=list)
    model: str | None = None
    tasted: hub.Taste | None = None
    name: str | None = None
    namespace: str | None = None
    mode: str = "smart"
    private: bool = True

    @classmethod
    def initial(cls, job: Job) -> "Answers":
        a = cls(hf_token=job.hf_token, model=job.model or None, name=job.name or None, namespace=job.namespace,
                mode=job.mode, private=job.private)
        try:
            saved = json.loads((job.home / "setup.json").read_text())
            defaults = cls()
            for k in SAVED:
                if k in saved and getattr(a, k) == getattr(defaults, k):
                    setattr(a, k, saved[k])
        except (OSError, ValueError):
            pass
        return a

    def save(self, home: Path) -> None:
        """Remember the non-secret answers for next time (the token never touches the disk)."""
        try:
            home.mkdir(parents=True, exist_ok=True)
            (home / "setup.json").write_text(json.dumps({k: getattr(self, k) for k in SAVED}, indent=2))
        except OSError:
            pass

    @property
    def repo_id(self) -> str | None:
        if not self.hf_token or not self.name or self.namespace in (None, SKIP):
            return None
        return f"{self.namespace}/{self.name}-GGUF"


# ── the screen ────────────────────────────────────────────────────────────────────

class Screen:
    def __init__(self) -> None:
        self.status = ""
        self._last: tuple | None = None
        if hasattr(signal, "SIGWINCH"):
            try:
                signal.signal(signal.SIGWINCH, self._on_resize)
            except (ValueError, OSError):
                pass

    @property
    def panel_width(self) -> int:
        return max(40, min(76, ui.console.width - 4))

    def prompt_prefix(self) -> str:
        pad = max(0, (ui.console.width - self.panel_width) // 2 + 2)
        return " " * pad + f"[bold {ui.HOT}]>[/] "

    def _on_resize(self, *_args) -> None:
        if self._last:
            self.draw(*self._last)
            ui.console.print(self.prompt_prefix(), end="")

    def deactivate(self) -> None:
        self._last = None

    def draw(self, step: str, title: str, body, error: str | None = None) -> None:
        self._last = (step, title, body, error)
        c = ui.console
        w, h = c.size
        parts = [Text.from_markup("\n".join(body)) if isinstance(body, list) else body]
        if error:
            parts.append(Text.from_markup(f"\n[bold {ui.ERR}]! {escape(error)}[/]"))
        panel = Panel(
            Group(*parts),
            title=f"[bold {ui.HOT}] {escape(title)} [/]",
            subtitle=f"[{ui.DIM}] {escape(step)} [/]" if step else None,
            border_style=ui.DEEP,
            width=self.panel_width,
            padding=(1, 2),
        )
        panel_h = len(c.render_lines(panel, c.options.update(width=self.panel_width), pad=False))
        show_art = w >= art.width() + 2
        total = (art.height() + 1 if show_art else 1) + 2 + panel_h + 2
        c.clear()
        top = max(0, (h - total) // 2)
        if top:
            c.print("\n" * (top - 1))
        if show_art:
            art.print_centered(c, w)
        c.print()
        c.print(Align.center(Text(talk.TITLE, style=f"bold {ui.PURPLE}")))
        if self.status:
            c.print(Align.center(Text(self.status, style=ui.DIM)))
        c.print(Align.center(panel))
        c.print(Align.center(Text(talk.EXIT_HINT, style=f"italic {ui.DIM}")))

    def ask(self, step: str, title: str, body, error: str | None = None, password: bool = False) -> str:
        self.draw(step, title, body, error)
        return ui.read_line(self.prompt_prefix(), password=password).strip()

    def pick(self, step: str, title: str, intro: list[str], options: list[tuple[str, str]], default: int = 0) -> str:
        error = None
        while True:
            body = list(intro) + [""] + [
                f"  [{ui.HOT}]{'>' if i == default else ' '}[/] [bold {ui.DEEP}]{i + 1}[/]  {label}"
                for i, (_, label) in enumerate(options)
            ] + ["", f"[{ui.DIM}]number, or Enter for {default + 1}[/]"]
            raw = self.ask(step, title, body, error)
            if not raw:
                return options[default][0]
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            error = f"pick 1-{len(options)}"


# ── the questions ─────────────────────────────────────────────────────────────────

def step_hf(screen: Screen, a: Answers, step: str) -> None:
    error = None
    while True:
        body = [
            f"Your [bold {ui.HOT}]Hugging Face token[/] (write access).",
            f"[{ui.DIM}]huggingface.co/settings/tokens · skip = keep everything on this pod[/]",
        ]
        if a.hf_token:
            who = f" ({escape(a.hf_user)})" if a.hf_user else ""
            body += ["", f"saved: {_mask(a.hf_token)}{who}  [{ui.DIM}]Enter keeps it[/]"]
        value = screen.ask(step, talk.ASK_TOKEN, body, error, password=True) or (a.hf_token or "")
        if value.lower() == "skip":
            a.hf_token, a.hf_user, a.hf_orgs, a.namespace = None, None, [], SKIP
            return
        if not value:
            error = "I need a token (or type skip)"
            continue
        try:
            ident = upload.identity(value)
        except Exception as exc:  # noqa: BLE001
            error = f"Hugging Face said no: {_short(exc)}"
            continue
        if ident.token_role and ident.token_role not in ("write", "fineGrained", "admin"):
            error = f"that token is '{ident.token_role}', uploading needs a write token"
            continue
        a.hf_token, a.hf_user, a.hf_orgs = value, ident.user, ident.orgs
        os.environ["HF_TOKEN"] = value
        return


def step_model(screen: Screen, a: Answers, step: str) -> None:
    error = None
    while True:
        body = [
            "A Hugging Face model, a model folder on this pod, or a .gguf.",
            f"[{ui.DIM}]e.g. Qwen/Qwen3-8B[/]",
        ]
        if a.model:
            body += ["", f"current: [bold {ui.HOT}]{escape(a.model)}[/]  [{ui.DIM}]Enter keeps it[/]"]
        value = screen.ask(step, talk.ASK_MODEL, body, error) or (a.model or "")
        if not value:
            error = "I'm hungry. give me a model"
            continue
        ui.console.print(f"  [{ui.DIM}]{talk.SAVORING}[/]")
        try:
            tasted = hub.taste(value, a.hf_token)
        except ValueError as exc:
            error = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001
            error = f"couldn't get a taste: {_short(exc)}"
            continue
        old_default = a.tasted.short_name if a.tasted else None
        a.model, a.tasted = value, tasted
        if not a.name or a.name == old_default:
            a.name = tasted.short_name
        return


def step_name(screen: Screen, a: Answers, step: str) -> None:
    error = None
    while True:
        default = a.name or (a.tasted.short_name if a.tasted else "model")
        body = [
            f"It goes up as [bold {ui.HOT}]{escape(default)}-GGUF[/].",
            "",
            f"[{ui.DIM}]Enter keeps[/] [bold {ui.HOT}]{escape(default)}[/]",
        ]
        value = screen.ask(step, talk.ASK_NAME, body, error) or default
        if value.lower().endswith("-gguf"):
            value = value[:-5]
        if not NAME_RE.match(value):
            error = "letters, numbers, . _ - only"
            continue
        a.name = value
        return


def step_place(screen: Screen, a: Answers, step: str) -> None:
    """Always asked: your account, any of your orgs, or type one in."""
    if not a.hf_token:
        a.namespace = SKIP
        return
    opts = [(a.hf_user, f"{escape(a.hf_user or '')}  [{ui.DIM}](you)[/]")]
    opts += [(o, f"{escape(o)}  [{ui.DIM}](org)[/]") for o in a.hf_orgs]
    if a.namespace and a.namespace not in (SKIP, OTHER) and a.namespace not in [v for v, _ in opts]:
        opts.append((a.namespace, f"{escape(a.namespace)}  [{ui.DIM}](typed last time)[/]"))
    opts.append((OTHER, f"[{ui.DIM}]type another name or org[/]"))
    values = [v for v, _ in opts]
    default = values.index(a.namespace) if a.namespace in values else 0
    intro = [f"It goes up as [bold {ui.HOT}]<here>/{escape(a.name or '')}-GGUF[/]."]
    choice = screen.pick(step, talk.ASK_PLACE, intro, opts, default)
    if choice != OTHER:
        a.namespace = choice
        return
    error = None
    while True:
        value = screen.ask(step, talk.ASK_PLACE, [
            "Type the account or org name.",
            f"[{ui.DIM}]your token needs write access to it[/]",
        ], error).strip().strip("/")
        if not value:
            error = "type a name (or cmd-exit)"
            continue
        if not NS_RE.match(value):
            error = "letters, numbers, . _ - only"
            continue
        a.namespace = value
        return


def step_mode(screen: Screen, a: Answers, step: str) -> None:
    opts = [
        ("smart", f"{talk.MODE_SMART}  [{ui.DIM}]{talk.MODE_SMART_HINT}[/]"),
        ("dump", f"{talk.MODE_DUMP}  [{ui.DIM}]{talk.MODE_DUMP_HINT}[/]"),
    ]
    keys = [k for k, _ in opts]
    a.mode = screen.pick(step, talk.ASK_MODE, [], opts, keys.index(a.mode) if a.mode in keys else 0)


STEPS = [step_hf, step_model, step_name, step_place, step_mode]


def _summary(a: Answers, job: Job, gpus: int) -> Group:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style=f"bold {ui.DEEP}", no_wrap=True)
    t.add_column(style=f"bold {ui.HOT}", no_wrap=True)
    t.add_column(style=ui.PURPLE, overflow="fold")
    rows = [
        ("1", "token", a.hf_user or "skipped, stays on this pod"),
        ("2", "model", a.model or "-"),
        ("3", "name", a.name or "-"),
        ("4", "goes to", a.repo_id or "this pod only"),
        ("5", "mode", talk.MODE_SMART if a.mode == "smart" else talk.MODE_DUMP),
        ("p", "visibility", "private" if a.private else "public"),
    ]
    for key, label, value in rows:
        t.add_row(key, label, escape(str(value)))
    quants = menu.resolve(job.menu, job.custom, bool(a.tasted and a.tasted.is_moe))
    imatrix = {"auto": "with imatrix" if gpus else "no imatrix (no GPU)", "on": "with imatrix", "off": "no imatrix"}[job.imatrix]
    size = f"  ·  {a.tasted.label()}" if a.tasted else ""
    return Group(
        t,
        Text.from_markup(f"\n[{ui.DIM}]{len(quants)} GGUFs, {imatrix}{escape(size)}[/]"),
        Text.from_markup(f"\n[{ui.HOT}]Enter[/] start  ·  [{ui.HOT}]1-5 p[/] change"),
    )


def collect(screen: Screen, a: Answers, job: Job, gpus: int) -> None:
    total = len(STEPS)
    for i, fn in enumerate(STEPS, start=1):
        fn(screen, a, f"{i}/{total}")
    error = None
    while True:
        raw = screen.ask("", talk.READY, _summary(a, job, gpus), error).lower()
        error = None
        if raw == "":
            if not a.tasted or not a.name:
                error = "I still need a model and a name"
                continue
            return
        if raw == "p":
            a.private = not a.private
        elif raw.isdigit() and 1 <= int(raw) <= total:
            STEPS[int(raw) - 1](screen, a, f"{raw}/{total}")
        else:
            error = "Enter, 1-5 or p"


# ── the meal ──────────────────────────────────────────────────────────────────────

def execute(screen: Screen, a: Answers, base: Job) -> Report:
    import dataclasses

    job = dataclasses.replace(base)
    job.model, job.name = a.model or "", a.name or ""
    job.namespace = None if a.namespace in (None, SKIP) else a.namespace
    job.mode, job.private, job.hf_token = a.mode, a.private, a.hf_token
    a.save(base.home)
    screen.deactivate()
    return Gulp(job, a.tasted).run()


def _wait_or_enter(seconds: int) -> None:
    if os.name == "posix" and sys.stdin.isatty():
        import select

        ready, _, _ = select.select([sys.stdin], [], [], seconds)
        if ready and sys.stdin.readline().strip().lower() == ui.EXIT_WORD:
            raise ui.CmdExit()
        return
    ui.read_line("")


def finish_screen(screen: Screen, r: Report) -> None:
    body = [
        f"[bold {ui.HOT}]{escape(r.name)}: {talk.ALL_GULPED}[/]",
        "",
        f"{len(r.made)} GGUFs, {gb(sum(s for _, s in r.made))}, {len(r.uploaded)} on HF, {r.minutes:.0f} min",
    ]
    if r.repo_url:
        body.append(escape(r.repo_url))
    if r.skipped:
        body += ["", f"[{ui.DIM}]skipped: {escape(', '.join(r.skipped))[:400]}[/]"]
    if r.failed:
        body += ["", f"[bold {ui.ERR}]came back up: {escape(', '.join(r.failed))}[/]"]
    if r.kept_in:
        body += ["", f"[{ui.DIM}]still on this pod: {escape(str(r.kept_in))}[/]"]
    body += ["", f"[{ui.DIM}]back to the start in 60 s · Enter now · cmd-exit to leave[/]"]
    screen.draw("", talk.DONE, body)
    ui.console.print(screen.prompt_prefix(), end="")
    _wait_or_enter(60)


def error_screen(screen: Screen, title: str, message: str, log: Path | None) -> None:
    tail = "\n".join(message.strip().splitlines()[-14:])[-1400:]
    body = [
        f"[bold {ui.ERR}]{escape(tail) or 'unknown error'}[/]",
        "",
        *([f"[{ui.DIM}]log: {escape(str(log))}[/]"] if log else []),
        f"[{ui.HOT}]Enter[/] back to the start  ·  [{ui.DIM}]cmd-exit to leave[/]",
    ]
    screen.draw("", title, body)
    ui.read_line(screen.prompt_prefix())


def leave() -> int:
    try:
        DISMISSED.parent.mkdir(parents=True, exist_ok=True)
        DISMISSED.touch()
    except OSError:
        pass
    ui.console.clear()
    ui.console.print(f"[bold {ui.HOT}]{talk.BYE}[/] [dim]- type ggulp to bring me back[/dim]")
    return 0


def run_setup(job: Job) -> int:
    try:
        DISMISSED.unlink()
    except OSError:
        pass
    ui.set_yes(False)
    screen = Screen()
    a = Answers.initial(job)
    hw = hardware.detect(job.home if job.home.exists() else None)
    screen.status = hw.gpu_label
    while True:
        log = None
        try:
            collect(screen, a, job, hw.gpu_count)
            log = job.home / (a.name or "") / "logs" / "latest.log"
            report = execute(screen, a, job)
            screen.status = f"{hw.gpu_label} · last: {report.name} done"
            finish_screen(screen, report)
        except ui.CmdExit:
            return leave()
        except KeyboardInterrupt:
            screen.status = f"{hw.gpu_label} · last one stopped"
            try:
                error_screen(screen, "stopped", "you stopped it. start it again with the same name and I carry on", log)
            except (ui.CmdExit, KeyboardInterrupt):
                return leave()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, GulpError) else f"{type(exc).__name__}: {exc}"
            screen.status = f"{hw.gpu_label} · last one failed"
            try:
                error_screen(screen, talk.SICK, msg, log)
            except (ui.CmdExit, KeyboardInterrupt):
                return leave()
