"""ggulp - the GGULPING command.

  ggulp                 the setup screen (in tmux session "ggulp", so a dropped ssh never stops a meal)
  ggulp run             no questions: eat GGULP_MODEL using the GGULP_* settings
  ggulp menu            every quant this llama.cpp can make
  ggulp logs [-f]       the latest meal's log
  ggulp attach          back to a meal that's running
  ggulp selftest        tiny model, CPU only, no upload: checks the whole pipeline works
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.markup import escape

from forge import ui

from . import __version__, hub, llama, menu, talk
from .config import Job
from .gulp import Gulp, GulpError, Report
from .menu import gb


def say(text: str) -> None:
    ui.console.print(text, markup=False, highlight=False)


def shout(text: str) -> None:
    ui.console.print(f"[bold {ui.ERR}]! {escape(text)}[/]")


def banner() -> None:
    from rich.align import Align
    from rich.text import Text

    from forge import art

    if ui.console.width >= art.width() + 2:
        art.print_centered(ui.console)
    ui.console.print(Align.center(Text(talk.TITLE, style=f"bold {ui.PURPLE}")))
    ui.console.print(Align.center(Text(f"llama.cpp {llama.version()} · ggulp {__version__}", style=ui.DIM)))
    ui.console.print()


def print_report(r: Report) -> None:
    rows = [(q, gb(s), "uploaded" if q in r.uploaded else "on this pod") for q, s in r.made]
    rows += [(q, "-", f"skipped: {why}") for q, why in r.skipped.items()]
    rows += [(q, "-", "failed") for q in r.failed]
    ui.table(f"{r.name} · {talk.ALL_GULPED} · {r.minutes:.0f} min", ["quant", "size", ""], rows)
    if r.repo_url:
        say(r.repo_url)
    if r.readme:
        say(r.readme)
    if r.kept_in:
        say(f"still on this pod: {r.kept_in}")


def cmd_setup(args) -> int:
    wrap = (
        not args.here
        and not os.environ.get("TMUX")
        and os.environ.get("GGULP_SETUP_TMUX", "1") != "0"
        and shutil.which("tmux")
        and sys.stdin.isatty()
    )
    if wrap:
        os.execvp("tmux", ["tmux", "new-session", "-A", "-s", "ggulp", "ggulp setup --here"])
    from . import screen

    return screen.run_setup(Job.from_env())


def cmd_run(args) -> int:
    job = Job.from_env()
    if not job.model:
        shout("set GGULP_MODEL (a Hugging Face model, a model folder or a .gguf)")
        return 2
    say(talk.SAVORING)
    tasted = hub.taste(job.model, job.hf_token)
    job.name = job.name or tasted.short_name
    if job.hf_token and not job.namespace:
        from forge import upload

        job.namespace = upload.identity(job.hf_token).user
    banner()
    report = Gulp(job, tasted).run()
    print_report(report)
    return 1 if report.failed else 0


def cmd_menu(args) -> int:
    have = set(llama.available_types())
    rows = []
    for q in menu.BUFFET + [q for q in menu.EXTRAS if q not in menu.BUFFET]:
        where = "buffet" if q in menu.BUFFET else "extra"
        if q in menu.SNACK:
            where += " + snack"
        rows.append((q, f"{menu.BPW.get(q, 0):g}", "yes" if q in menu.NEEDS_IMATRIX else "", where,
                     "yes" if q in have or q == "BF16" else "no"))
    ui.table(f"llama.cpp {llama.version()}", ["quant", "≈ bits/weight", "needs imatrix", "menu", "here"], rows)
    return 0


def _latest_log() -> Path | None:
    home = Job.from_env().home
    logs = sorted(home.glob("*/logs/gulp-*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def cmd_logs(args) -> int:
    log = _latest_log()
    if not log:
        say("nothing eaten yet")
        return 1
    os.execvp("tail", ["tail", "-n", "200"] + (["-F"] if args.follow else []) + [str(log)])
    return 0


def cmd_attach(args) -> int:
    if subprocess.call(["tmux", "has-session", "-t", "ggulp"], stderr=subprocess.DEVNULL) != 0:
        say("nothing is running (no tmux session 'ggulp')")
        return 1
    os.execvp("tmux", ["tmux", "attach", "-t", "ggulp"])
    return 0


def cmd_selftest(args) -> int:
    """A whole meal on a tiny model, CPU only, nothing uploaded. Splitting is forced on to test it."""
    home = Path(tempfile.mkdtemp(prefix="ggulp-selftest-"))
    quants = ["Q8_0", "Q4_K_M", "IQ4_XS", "Q2_K"] + (["IQ2_XXS"] if args.imatrix else [])
    job = Job(model=args.model, name="selftest", namespace=None, mode="smart", menu="custom", custom=quants,
              imatrix="on" if args.imatrix else "off", home=home, reserve_gb=0, split_gb=0.1, imatrix_chunks=8)
    say(f"selftest: {args.model}  {' '.join(quants)}")
    failures = []
    try:
        report = Gulp(job, live=False).run()
        print_report(report)
        for q in quants:
            files = llama.pieces(job.workdir / "out", f"selftest-{q}")
            if not files:
                failures.append(f"{q}: no file ({report.failed.get(q) or report.skipped.get(q) or '?'})")
                continue
            for f in files:
                with open(f, "rb") as fh:
                    if fh.read(4) != b"GGUF":
                        failures.append(f"{f.name}: not a GGUF")
        if not any(len(llama.pieces(job.workdir / "out", f"selftest-{q}")) > 1 for q in quants):
            failures.append("nothing got split into pieces (the split step was not exercised)")
        if args.imatrix and not report.imatrix:
            failures.append("no importance matrix was made")
    except GulpError as exc:
        failures.append(str(exc))
    finally:
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
    if failures:
        for f in failures:
            shout(f)
        return 1
    say("selftest passed: the whole meal works")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ggulp", description="GGULPING: every llama.cpp GGUF of a model, up to the Hub.")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("setup", help="the setup screen")
    sp.add_argument("--here", action="store_true", help="run in this terminal instead of tmux session 'ggulp'")
    sub.add_parser("run", help="eat GGULP_MODEL with the GGULP_* settings, no questions")
    sub.add_parser("menu", help="every quant this llama.cpp can make")
    lp = sub.add_parser("logs", help="the latest meal's log")
    lp.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("attach", help="back to a meal that's running")
    tp = sub.add_parser("selftest", help="a whole meal on a tiny model (CPU, no upload)")
    tp.add_argument("--model", default="HuggingFaceTB/SmolLM2-135M")
    tp.add_argument("--imatrix", action="store_true", help="also test the importance matrix (downloads wikitext)")
    tp.add_argument("--keep", action="store_true", help="keep the files")
    sub.add_parser("banner", help="the wolfgon")
    args = p.parse_args(argv)
    try:
        if args.cmd is None:
            if ui.interactive():
                args.here = False
                return cmd_setup(args)
            return cmd_run(args)
        return {
            "setup": cmd_setup, "run": cmd_run, "menu": cmd_menu, "logs": cmd_logs, "attach": cmd_attach,
            "selftest": cmd_selftest, "banner": lambda a: (banner(), 0)[1],
        }[args.cmd](args)
    except ui.CmdExit:
        return 0
    except (GulpError, ValueError) as exc:
        shout(f"{talk.SICK}: {exc}")
        return 1
    except KeyboardInterrupt:
        say("stopped. run it again with the same name and I carry on")
        return 130
