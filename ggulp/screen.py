"""The setup screen: the wolfgon in the middle, one question at a time, then the meal.

HF token -> model -> name -> shelf (where it uploads) -> menu -> mode (smart / dump) -> Discord.
Then a summary to change anything, then it runs, then back to the start. cmd-exit leaves."""

from __future__ import annotations

import json
import os
import re
import shutil
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
from forge.notify import valid_webhook

from . import hub, menu, talk
from .config import GB, Job
from .gulp import Gulp, GulpError, Report
from .menu import gb

DISMISSED = Path(os.environ.get("GGULP_SETUP_DISMISSED", "/tmp/ggulp-setup-dismissed"))
SKIP = "__skip__"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
SAVED = ("model", "name", "namespace", "menu", "custom", "mode", "stomach_gb", "imatrix", "private")


def _mask(secret: str | None) -> str:
    if not secret:
        return "-"
    return secret[:6] + "…" + secret[-4:] if len(secret) > 12 else "…"


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
    menu: str = "buffet"
    custom: list[str] = field(default_factory=list)
    mode: str = "smart"
    stomach_gb: float | None = None
    imatrix: str = "auto"
    private: bool = True
    webhook: str | None = None

    @classmethod
    def initial(cls, job: Job) -> "Answers":
        a = cls(hf_token=job.hf_token, model=job.model or None, name=job.name or None, namespace=job.namespace,
                menu=job.menu, custom=job.custom, mode=job.mode, stomach_gb=job.stomach_gb, imatrix=job.imatrix,
                private=job.private, webhook=job.webhook)
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
        """Remember the non-secret answers for next time (tokens and webhooks never touch the disk)."""
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

    def quants(self) -> list[str]:
        return menu.resolve(self.menu, self.custom, bool(self.tasted and self.tasted.is_moe))

    def menu_bytes(self) -> int | None:
        if not self.tasted:
            return None
        params = self.tasted.params
        base = self.tasted.weight_bytes
        sizes = [menu.estimate(q, params, base) for q in self.quants()]
        if "BF16" not in self.quants():
            sizes.append(menu.estimate("BF16", params, base))  # made anyway: every quant comes from it
        return sum(s or 0 for s in sizes)

    def menu_label(self) -> str:
        q = self.quants()
        if self.menu == "buffet":
            return f"{talk.MENU_BUFFET} · {len(q)} GGUFs"
        if self.menu == "snack":
            return f"{talk.MENU_SNACK} · {' '.join(q)}"
        return f"{talk.MENU_CUSTOM} · {' '.join(q) or '-'}"


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
        return max(40, min(84, ui.console.width - 4))

    def prompt_prefix(self) -> str:
        pad = max(0, (ui.console.width - self.panel_width) // 2 + 2)
        return " " * pad + f"[bold {ui.HOT}]❯[/] "

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
            parts.append(Text.from_markup(f"\n[bold {ui.ERR}]✖ {escape(error)}[/]"))
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
        else:
            c.print(Align.center(Text("🐺 GGULPING", style=f"bold {ui.HOT}")))
        c.print()
        c.print(Align.center(Text(talk.TITLE, style=f"bold {ui.PURPLE}")))
        if self.status:
            c.print(Align.center(Text(self.status, style=ui.DIM)))
        c.print(Align.center(panel))
        c.print(Align.center(Text(talk.EXIT_HINT, style=f"italic {ui.DIM}")))

    def ask(self, step: str, title: str, body, error: str | None = None, password: bool = False) -> str:
        self.draw(step, title, body, error)
        return ui.read_line(self.prompt_prefix(), password=password).strip()

    def pick(self, step: str, title: str, intro: list[str], options: list[tuple[str, str]], default: int = 0,
             error: str | None = None) -> str:
        while True:
            body = list(intro) + [""] + [
                f"  [{ui.HOT}]{'❯' if i == default else ' '}[/] [bold {ui.DEEP}]{i + 1}[/]  {label}"
                for i, (_, label) in enumerate(options)
            ] + ["", f"[{ui.DIM}]number, or Enter for {default + 1}[/]"]
            raw = self.ask(step, title, body, error)
            if not raw:
                return options[default][0]
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            error = f"pick 1-{len(options)}"


# ── the questions ─────────────────────────────────────────────────────────────────

def step_hf(screen: Screen, a: Answers, step: str, job: Job) -> None:
    error = None
    while True:
        body = [
            f"Hand me your [bold {ui.HOT}]Hugging Face token[/] - it needs [b]write[/b] access so I can put the GGUFs on your shelf.",
            f"[{ui.DIM}]huggingface.co/settings/tokens · type skip to keep everything on this pod[/]",
        ]
        if a.hf_token:
            who = f" · [bold {ui.HOT}]{escape(a.hf_user)}[/]" if a.hf_user else ""
            body += ["", f"saved: {_mask(a.hf_token)}{who}  [{ui.DIM}](Enter keeps it)[/]"]
        value = screen.ask(step, talk.ASK_TOKEN, body, error, password=True) or (a.hf_token or "")
        if value.lower() == "skip":
            a.hf_token, a.hf_user, a.hf_orgs, a.namespace = None, None, [], SKIP
            return
        if not value:
            error = "I need a token (or type skip)"
            continue
        try:
            with ui.console.status("sniffing the token…"):
                ident = upload.identity(value)
        except Exception as exc:  # noqa: BLE001
            error = f"Hugging Face spat that token out: {_short(exc)}"
            continue
        if ident.token_role and ident.token_role not in ("write", "fineGrained", "admin"):
            error = f"that token is '{ident.token_role}' - uploading needs a write token (or type skip)"
            continue
        a.hf_token, a.hf_user, a.hf_orgs = value, ident.user, ident.orgs
        os.environ["HF_TOKEN"] = value
        return


def step_model(screen: Screen, a: Answers, step: str, job: Job) -> None:
    error = None
    while True:
        body = [
            f"Paste a [bold {ui.HOT}]Hugging Face model[/] (id or link), a model folder on this pod, or a .gguf.",
            "",
            f"[{ui.DIM}]e.g. Qwen/Qwen3-8B[/]",
            f"[{ui.DIM}]e.g. https://huggingface.co/google/gemma-3-12b-it[/]",
            f"[{ui.DIM}]e.g. /workspace/forge/outputs/<run>/merged  (fresh out of The Trainer)[/]",
        ]
        if a.model:
            body += ["", f"current: [bold {ui.HOT}]{escape(a.model)}[/]  [{ui.DIM}](Enter keeps it)[/]"]
        value = screen.ask(step, talk.ASK_MODEL, body, error) or (a.model or "")
        if not value:
            error = "I'm hungry - give me a model"
            continue
        try:
            with ui.console.status(talk.SAVORING):
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


def step_name(screen: Screen, a: Answers, step: str, job: Job) -> None:
    error = None
    while True:
        default = a.name or (a.tasted.short_name if a.tasted else "model")
        body = [
            f"The repo becomes [bold {ui.HOT}]<shelf>/{escape(default)}-GGUF[/],",
            f"the files [bold {ui.HOT}]{escape(default)}-Q4_K_M.gguf[/], [bold {ui.HOT}]{escape(default)}-Q8_0.gguf[/], …",
            "",
            f"suggested: [bold {ui.HOT}]{escape(default)}[/]  [{ui.DIM}](Enter keeps it)[/]",
        ]
        value = screen.ask(step, talk.ASK_NAME, body, error) or default
        if value.lower().endswith("-gguf"):
            value = value[:-5]
        if not NAME_RE.match(value):
            error = "letters, numbers, . _ - only (max 91)"
            continue
        a.name = value
        return


def step_place(screen: Screen, a: Answers, step: str, job: Job) -> None:
    if not a.hf_token:
        a.namespace = SKIP
        return
    opts = []
    if a.hf_user:
        opts.append((a.hf_user, f"{escape(a.hf_user)}  [{ui.DIM}](you)[/]"))
    opts += [(o, f"{escape(o)}  [{ui.DIM}](org)[/]") for o in a.hf_orgs]
    opts.append((SKIP, f"[{ui.DIM}]keep it on this pod (no upload)[/]"))
    values = [v for v, _ in opts]
    default = values.index(a.namespace) if a.namespace in values else 0
    a.namespace = screen.pick(step, talk.ASK_PLACE, [
        f"Your GGUFs go to [bold {ui.HOT}]<shelf>/{escape(a.name or '')}-GGUF[/].",
        f"[{ui.DIM}]your account and every org your token can write to:[/]",
    ], opts, default)


def step_menu(screen: Screen, a: Answers, step: str, job: Job) -> None:
    params, base = (a.tasted.params, a.tasted.weight_bytes) if a.tasted else (None, None)
    moe = bool(a.tasted and a.tasted.is_moe)

    def total(name: str) -> str:
        qs = menu.resolve(name, [], moe)
        return gb(sum(menu.estimate(q, params, base) or 0 for q in qs)) if a.tasted else "?"

    buffet = menu.resolve("buffet", [], moe)
    opts = [
        ("buffet", f"{talk.MENU_BUFFET}  [{ui.DIM}]every quant: {len(buffet)} GGUFs, BF16 down to IQ1_S · ~{total('buffet')}[/]"),
        ("snack", f"{talk.MENU_SNACK}  [{ui.DIM}]{' '.join(menu.SNACK)} · ~{total('snack')}[/]"),
        ("custom", f"{talk.MENU_CUSTOM}  [{ui.DIM}]type the ones you want[/]"),
    ]
    keys = [k for k, _ in opts]
    choice = screen.pick(step, talk.ASK_MENU, ["Which GGUFs should I make?"], opts, keys.index(a.menu) if a.menu in keys else 0)
    if choice != "custom":
        a.menu = choice
        return
    error = None
    while True:
        body = [
            "Type the quants you want, separated by spaces or commas.",
            "",
            f"[{ui.DIM}]{' '.join(menu.BUFFET)}[/]",
            f"[{ui.DIM}]also: F16 MXFP4_MOE (for MoE models)[/]",
        ]
        if a.custom:
            body += ["", f"current: [bold {ui.HOT}]{escape(' '.join(a.custom))}[/]  [{ui.DIM}](Enter keeps it)[/]"]
        raw = screen.ask(step, talk.ASK_PICK, body, error) or " ".join(a.custom)
        picked, unknown = menu.parse_custom(raw)
        if unknown:
            error = f"not on any menu I know: {' '.join(unknown)}"
            continue
        if not picked:
            error = "pick at least one"
            continue
        a.menu, a.custom = "custom", picked
        return


def _free_here(job: Job) -> int:
    job.home.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(job.home).free


def step_mode(screen: Screen, a: Answers, step: str, job: Job) -> None:
    free = _free_here(job)
    need = a.menu_bytes()
    dump_note = f"needs ~{gb(need)} free, you have {gb(free)}" if need else f"you have {gb(free)} free"
    tight = need is not None and need > free
    opts = [
        ("smart", f"{talk.MODE_SMART}  [{ui.DIM}]{talk.MODE_SMART_HINT}[/]"),
        ("dump", f"{talk.MODE_DUMP}  [{ui.DIM}]{talk.MODE_DUMP_HINT} · {dump_note}[/]"
                 + (f"  [bold {ui.ERR}]won't fit![/]" if tight else "")),
    ]
    keys = [k for k, _ in opts]
    a.mode = screen.pick(step, talk.ASK_MODE, [f"[{ui.DIM}]{gb(free)} free on this pod right now[/]"], opts,
                         keys.index(a.mode) if a.mode in keys else 0)
    if a.mode != "smart":
        return
    error = None
    while True:
        body = [
            "How many GB am I allowed to fill on this pod?",
            f"[{ui.DIM}]handy on volumes you pay for per GB · Enter = all the free space ({gb(free)})[/]",
        ]
        if a.stomach_gb:
            body += ["", f"current: [bold {ui.HOT}]{a.stomach_gb:g} GB[/]  [{ui.DIM}](type all for all the free space)[/]"]
        raw = screen.ask(step, talk.ASK_STOMACH, body, error).lower().removesuffix("gb").strip()
        if not raw:
            return
        if raw in ("all", "max", "none"):
            a.stomach_gb = None
            return
        try:
            value = float(raw)
        except ValueError:
            error = "a number of GB, like 300"
            continue
        if value < 5:
            error = "that's not a stomach, that's a crumb - at least 5 GB"
            continue
        a.stomach_gb = value
        return


def step_webhook(screen: Screen, a: Answers, step: str, job: Job) -> None:
    error = None
    while True:
        body = [
            f"A [bold {ui.HOT}]Discord webhook[/] pings you when everything's gulped (or if something comes back up).",
            f"[{ui.DIM}]Server settings -> Integrations -> Webhooks -> copy URL · Enter to skip · none to turn off[/]",
        ]
        if a.webhook:
            body += ["", f"saved: {_mask(a.webhook)}  [{ui.DIM}](Enter keeps it)[/]"]
        value = screen.ask(step, talk.ASK_WEBHOOK, body, error, password=True)
        if not value:
            return
        if value.lower() in ("none", "off", "skip"):
            a.webhook = None
            return
        if not valid_webhook(value):
            error = "that doesn't look like https://discord.com/api/webhooks/…"
            continue
        a.webhook = value
        return


STEPS = [
    ("Hugging Face", step_hf),
    ("model", step_model),
    ("name", step_name),
    ("shelf", step_place),
    ("menu", step_menu),
    ("mode", step_mode),
    ("Discord", step_webhook),
]


def _imatrix_label(a: Answers, gpus: int) -> str:
    if a.imatrix == "auto":
        return "auto · on (GPU found)" if gpus else "auto · off (no GPU) - the IQ1/IQ2 quants get skipped"
    return a.imatrix


def _summary_table(a: Answers, gpus: int) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style=f"bold {ui.DEEP}", no_wrap=True)
    t.add_column(style=f"bold {ui.HOT}", no_wrap=True)
    t.add_column(style=ui.PURPLE, overflow="fold")
    mode = talk.MODE_SMART if a.mode == "smart" else talk.MODE_DUMP
    if a.mode == "smart":
        mode += f" · stomach: {f'{a.stomach_gb:g} GB' if a.stomach_gb else 'all free space'}"
    rows = [
        ("1", "Hugging Face", f"{a.hf_user} ✔" if a.hf_user else "skipped (stays on this pod)"),
        ("2", "model", f"{a.model or '-'}" + (f"  ({a.tasted.label()})" if a.tasted else "")),
        ("3", "name", a.name or "-"),
        ("4", "shelf", a.repo_id or "this pod only"),
        ("5", "menu", a.menu_label()),
        ("6", "mode", mode),
        ("7", "Discord", "on ✔" if a.webhook else "off"),
        ("i", "imatrix", _imatrix_label(a, gpus)),
        ("p", "visibility", "private" if a.private else "public"),
    ]
    for key, label, value in rows:
        t.add_row(key, label, escape(str(value)))
    return t


def collect(screen: Screen, a: Answers, job: Job, gpus: int) -> None:
    total = len(STEPS)
    for i, (_, fn) in enumerate(STEPS, start=1):
        fn(screen, a, f"step {i}/{total}", job)
    error = None
    while True:
        body = Group(
            _summary_table(a, gpus),
            Text.from_markup(f"\n[{ui.HOT}]Enter[/] dig in  ·  [{ui.HOT}]1-{total} i p[/] change something"),
        )
        raw = screen.ask("ready", talk.READY, body, error).lower()
        error = None
        if raw == "":
            if not a.tasted or not a.name:
                error = "I still need a model and a name"
                continue
            return
        if raw == "p":
            a.private = not a.private
        elif raw == "i":
            a.imatrix = {"auto": "on", "on": "off", "off": "auto"}[a.imatrix if a.imatrix in ("auto", "on", "off") else "off"]
        elif raw.isdigit() and 1 <= int(raw) <= total:
            STEPS[int(raw) - 1][1](screen, a, f"step {raw}/{total}", job)
        else:
            error = f"Enter, 1-{total}, i or p"


# ── the meal ──────────────────────────────────────────────────────────────────────

def _header(title: str) -> None:
    ui.console.clear()
    ui.console.print(Align.center(Text(talk.TITLE, style=f"bold {ui.PURPLE}")))
    ui.console.print(Align.center(Text(title, style=ui.DIM)))


def execute(screen: Screen, a: Answers, base: Job) -> Report:
    import dataclasses

    job = dataclasses.replace(base)
    job.model, job.name = a.model or "", a.name or ""
    job.namespace = None if a.namespace in (None, SKIP) else a.namespace
    job.menu, job.custom, job.mode, job.stomach_gb = a.menu, list(a.custom), a.mode, a.stomach_gb
    job.imatrix, job.private, job.webhook, job.hf_token = a.imatrix, a.private, a.webhook, a.hf_token
    a.save(base.home)
    screen.deactivate()
    _header(f"{job.name} · {job.repo_id or 'this pod only'} · log {job.workdir / 'logs' / 'latest.log'}")
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
    total = sum(s for _, s in r.made)
    body = [
        f"[bold {ui.HOT}]✔ {escape(r.name)} is {talk.ALL_GULPED}[/]",
        "",
        f"{len(r.made)} GGUFs · {gb(total)} · {len(r.uploaded)} uploaded · {r.minutes:.0f} min"
        + (" · imatrix ✔" if r.imatrix else ""),
    ]
    if r.repo_url:
        body.append(f"🍽️  {escape(r.repo_url)}")
    if r.readme:
        body.append(f"[{ui.DIM}]{escape(r.readme)}[/]")
    if r.skipped:
        body += ["", f"[{ui.DIM}]left on the plate: {escape(', '.join(f'{q} ({why})' for q, why in r.skipped.items()))[:600]}[/]"]
    if r.failed:
        body += ["", f"[bold {ui.ERR}]came back up: {escape(', '.join(r.failed))}[/]"]
    if r.kept_in:
        body += ["", f"[{ui.DIM}]still on this pod: {escape(str(r.kept_in))}[/]"]
    body += ["", f"[{ui.DIM}]back to the start in 60 s · Enter now · cmd-exit to leave[/]"]
    screen.draw("finished", talk.ALL_GULPED.capitalize(), body)
    ui.console.print(screen.prompt_prefix(), end="")
    _wait_or_enter(60)


def error_screen(screen: Screen, title: str, message: str, log: Path | None) -> None:
    tail = "\n".join(message.strip().splitlines()[-14:])[-1400:]
    body = [
        f"[bold {ui.ERR}]{escape(tail) or 'unknown error'}[/]",
        "",
        *([f"[{ui.DIM}]the full log: {escape(str(log))}[/]"] if log else []),
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
    ui.console.print(f"[bold {ui.HOT}]🐺 {talk.BYE}[/] [dim]- type ggulp to bring me back[/dim]")
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
    gpus = hw.gpu_count
    screen.status = hw.gpu_label
    while True:
        log = None
        try:
            collect(screen, a, job, gpus)
            log = job.home / (a.name or "") / "logs" / "latest.log"
            report = execute(screen, a, job)
            screen.status = f"{hw.gpu_label} · last meal: {report.name} ✔"
            finish_screen(screen, report)
        except ui.CmdExit:
            return leave()
        except KeyboardInterrupt:
            screen.status = f"{hw.gpu_label} · last meal stopped"
            try:
                error_screen(screen, "stopped", "you stopped it with Ctrl+C - run it again and I carry on where I left off", log)
            except (ui.CmdExit, KeyboardInterrupt):
                return leave()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, GulpError) else f"{type(exc).__name__}: {exc}"
            screen.status = f"{hw.gpu_label} · last meal ✖"
            try:
                error_screen(screen, talk.SICK, msg, log)
            except (ui.CmdExit, KeyboardInterrupt):
                return leave()
