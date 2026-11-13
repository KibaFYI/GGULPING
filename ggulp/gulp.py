"""The meal: download -> BF16 GGUF -> (imatrix) -> every quant -> Hub -> README.

Every quant goes: look -> savor -> gulp -> digesting -> burrp (a skull joins the pile).

smart mode: before every quant, its exact size (llama-quantize --dry-run) is checked against the
            free space. If it won't fit: "HMMMPH... I'm full", everything made so far is uploaded
            and deleted, "rested", and the cycle carries on. If a quant still runs out of disk,
            the same happens and it's tried once more.
dump mode:  every quant goes onto the disk first, then everything is uploaded at the end.
            Nothing is checked; if the disk fills up, quanting stops and what's done gets uploaded."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from forge import hardware

from . import hub, llama, menu, notify, talk
from .config import BARTOWSKI_CALIBRATION, GB, Job
from .menu import gb
from .show import Show

MIN_FREE = 1 * 1024**3  # less than this after a failure = the disk was full


class GulpError(RuntimeError):
    pass


@dataclass
class Dish:
    """One finished GGUF (maybe in several pieces) on its way to the Hub."""

    quant: str
    files: list[Path]
    size: int
    uploaded: bool = False
    is_base: bool = False  # the BF16 every quant is made from: stays on disk until the end


@dataclass
class Report:
    name: str
    repo_url: str | None = None
    made: list[tuple[str, int]] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    readme: str = ""
    kept_in: Path | None = None
    imatrix: bool = False
    minutes: float = 0.0


class State:
    """What's done, so a restarted pod carries on where it stopped."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("quants", {})

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    def quant(self, q: str) -> dict:
        return self.data["quants"].get(q, {})

    def mark(self, q: str, status: str, **extra) -> None:
        self.data["quants"][q] = {"status": status, "at": _dt.datetime.now().isoformat(timespec="seconds"), **extra}
        self.save()


def dir_bytes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Gulp:
    def __init__(self, job: Job, tasted: hub.Taste | None = None, live: bool | None = None):
        job.check()
        self.job = job
        self.tasted = tasted
        self.live = live
        self.work = job.workdir
        self.out = self.work / "out"
        self.logs = self.work / "logs"
        for d in (self.out, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.log = self.logs / f"gulp-{_dt.datetime.now():%Y%m%d-%H%M%S}.log"
        self.log.touch()
        latest = self.logs / "latest.log"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(self.log.name)
        except OSError:
            pass
        self.state = State(self.work / "state.json")
        self.report = Report(job.name, kept_in=None)
        self.dishes: list[Dish] = []
        self.extras: list[Path] = []  # the imatrix file, uploaded with the last burp
        self.hf = None
        self.remote: set[str] = set()
        self.base: list[Path] = []
        self.base_is_ours = True
        self.portions: dict[str, tuple[int | None, bool]] = {}
        self.burst = False
        self.notify = notify.Discord(job.webhook, job.name)
        self.threads = job.threads or os.cpu_count() or 4
        self.show = Show(job.name, 0, self.log, live=False)  # replaced by the real one in run()

    # ── space ──────────────────────────────────────────────────────────────────────

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.work).free

    def room(self) -> int:
        """Bytes I may still fill: free space, capped by the stomach size, minus the reserve."""
        free = self.free_bytes()
        if self.job.stomach_gb:
            free = min(free, int(self.job.stomach_gb * GB) - dir_bytes(self.work))
        return max(0, free - int(self.job.reserve_gb * GB))

    def need_for(self, size: int) -> int:
        """Making a quant takes its size; one that gets split needs room for both copies for a moment."""
        return size * 2 if size > self.job.split_gb * GB else size

    # ── the meal ───────────────────────────────────────────────────────────────────

    def run(self) -> Report:
        t0 = time.time()
        j = self.job
        try:
            from huggingface_hub.utils import disable_progress_bars

            disable_progress_bars()  # the run screen draws its own
        except ImportError:
            pass
        try:
            if not self.tasted:
                self.tasted = hub.taste(j.model, j.hf_token)
            todo = menu.resolve(j.menu, j.custom, self.tasted.is_moe, set(llama.available_types()) or None)
            if not todo:
                raise GulpError("the menu is empty - pick at least one quant")
            if j.namespace and j.hf_token:
                self.hf = hub.api(j.hf_token)
                self.report.repo_url = hub.ensure_repo(self.hf, j.repo_id, j.private)
                self.remote = hub.remote_files(self.hf, j.repo_id)
            with Show(j.name, len(todo), self.log, live=self.live) as show:
                self.show = show
                show.step(talk.LOOK, f"{self.tasted.ref}  {self.tasted.label()}")
                if not self.hf:
                    show.note("no Hugging Face token - everything stays on this pod")
                self._log(f"llama.cpp {llama.version()} · {j.mode} mode · menu: {' '.join(todo)}")
                self._make_base()
                self._read_portions(todo)
                imat = self._imatrix(todo)
                for q in todo:
                    if q == "BF16" or self.burst:
                        continue
                    self._cook(q, imat)
                if self.burst:
                    made = [m for m, _ in self.report.made]
                    for q in todo:
                        if q not in self.report.failed and q not in made and q != "BF16":
                            self.report.skipped.setdefault(q, "the disk filled up before I got to it")
                self._burp(final=True)
                self._readme()
                self._clean_up()
                show.fresh()
                show.step(talk.ALL_GULPED, f"{len(self.report.made)} GGUFs" +
                          (f"  {self.report.repo_url}" if self.report.repo_url else ""))
                if show.live:
                    time.sleep(2)
        except Exception as exc:
            self.notify.send(f"{j.name}: {talk.SICK}", f"```\n{str(exc)[-1500:]}\n```", ok=False)
            raise
        finally:
            try:
                from huggingface_hub.utils import enable_progress_bars

                enable_progress_bars()
            except ImportError:
                pass
        self.report.minutes = (time.time() - t0) / 60
        self._ping_done()
        return self.report

    def _log(self, text: str) -> None:
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")

    # ── step 1: one big BF16 GGUF ─────────────────────────────────────────────────

    def _make_base(self) -> None:
        j, t, show = self.job, self.tasted, self.show
        assert t is not None
        stem = f"{j.name}-BF16"
        if t.kind == "gguf":
            path = Path(t.ref)
            self.base = [path] if "-00001-of-" not in path.name else sorted(path.parent.glob(path.name.replace("-00001-of-", "-*-of-")))
            self.base_is_ours = False
            show.step(talk.BASE, f"already a GGUF: {path.name}")
            self._add_base_dish()
            return
        done = self.state.data.get("base") or {}
        existing = llama.pieces(self.out, stem)
        if done.get("status") == "done" and existing and all(p.exists() for p in existing):
            self.base = existing
            show.step(talk.BASE, f"BF16 from last time  {gb(sum(p.stat().st_size for p in existing))}")
            self._add_base_dish()
            return
        if self._all_on_hub_already():
            return
        for old in existing:  # half-written from a crash
            old.unlink()

        src = Path(t.ref) if t.kind == "local" else self.work / "src"
        if t.kind == "hf":
            need = t.download_bytes + t.weight_bytes  # the download and the BF16 sit side by side for a moment
            if self.room() < need:
                msg = f"the first bite needs {gb(need)} (download + BF16), there's only {gb(self.room())}"
                if j.mode == "smart":
                    raise GulpError(msg + " - give me a bigger disk or volume")
                show.note(msg + " - dump mode eats anyway")
            show.step(talk.FETCH, f"{t.ref}  {gb(t.download_bytes)}")
            self._download_with_bar(t, src)
        readme = src / "README.md"
        if readme.exists():
            shutil.copyfile(readme, self.work / "README.md")
        if (src / "config.json").exists():
            shutil.copyfile(src / "config.json", self.work / "config.json")

        show.step(talk.BASE, "BF16 GGUF")
        rc = llama.convert(src, self.out / f"{stem}.gguf", j.split_gb, self.log, mistral=t.mistral_format,
                           progress=lambda d, n: show.progress(talk.BASE, d, n, "BF16"))
        show.end_bar()
        if rc != 0:
            raise GulpError(f"turning it into a GGUF failed (exit {rc}):\n{llama.tail(self.log)}")
        self.base = llama.pieces(self.out, stem)
        if not self.base:
            raise GulpError(f"the converter finished but no {stem}.gguf showed up")
        self.state.data["base"] = {"status": "done", "files": [p.name for p in self.base]}
        self.state.save()
        if t.kind == "hf":
            shutil.rmtree(src, ignore_errors=True)
        self._add_base_dish(burp=True)

    def _download_with_bar(self, t: hub.Taste, src: Path) -> None:
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(1.0):
                if src.exists():
                    have = dir_bytes(src)
                    self.show.progress(talk.FETCH, have, t.download_bytes or 1, f"{gb(have)} / {gb(t.download_bytes)}")

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            hub.download(t, src, self.job.hf_token)
        finally:
            stop.set()
            watcher.join()
            self.show.end_bar()

    def _add_base_dish(self, burp: bool = False) -> None:
        todo = menu.resolve(self.job.menu, self.job.custom, bool(self.tasted and self.tasted.is_moe))
        if "BF16" not in todo:
            return
        size = sum(p.stat().st_size for p in self.base)
        dish = Dish("BF16", list(self.base), size, is_base=True)
        if self._on_hub("BF16"):
            dish.uploaded = True
        self.dishes.append(dish)
        self.report.made.append(("BF16", size))
        if burp:
            self.show.burp("BF16")

    def _all_on_hub_already(self) -> bool:
        todo = menu.resolve(self.job.menu, self.job.custom, bool(self.tasted and self.tasted.is_moe))
        if self.remote and all(self._on_hub(q) for q in todo):
            self.show.note("every quant is already on Hugging Face - nothing left to eat")
            return True
        return False

    def _on_hub(self, q: str) -> bool:
        stem = f"{self.job.name}-{q}"
        if f"{stem}.gguf" in self.remote:
            return True
        parts = [f for f in self.remote if re.fullmatch(re.escape(stem) + r"-\d{5}-of-(\d{5})\.gguf", f)]
        if not parts:
            return False
        total = int(re.search(r"-of-(\d{5})", parts[0]).group(1))
        return len(parts) == total

    # ── step 2: exact portion sizes ───────────────────────────────────────────────

    def _read_portions(self, todo: list[str]) -> None:
        if not self.base:
            return
        show = self.show
        base_bytes = sum(p.stat().st_size for p in self.base)
        rows, total = [], 0
        for i, q in enumerate(todo):
            show.progress(talk.LOOK, i, len(todo), "reading the menu")
            if q == "BF16":
                size, needs, note = base_bytes, False, "on HF" if self._on_hub("BF16") else "made"
            elif self._on_hub(q):
                size, needs, note = None, False, "already on HF"
            else:
                size, needs = llama.dry_run(self.base[0], q, base_bytes)
                note = ""
                if size is None:
                    size = menu.estimate(q, self.tasted.params if self.tasted else None, base_bytes)
                    note = "about"
                if needs or (size is None and q in menu.NEEDS_IMATRIX):
                    needs = True
                    note = (note + ", " if note else "") + "needs an imatrix"
                total += size or 0
            self.portions[q] = (size, needs)
            rows.append(f"  {q:<10} {gb(size):>10}  {note}")
        show.end_bar()
        self._log("the menu:\n" + "\n".join(rows))
        room = self.room()
        show.fresh()
        show.step(talk.LOOK, f"{len(todo)} quants, {gb(total)} to make, {gb(room)} free")
        if self.job.mode == "dump" and total > room:
            show.note(f"the buffet ({gb(total)}) is bigger than the disk ({gb(room)}) - dump mode eats anyway")
        elif self.job.mode == "smart":
            biggest = max((self.need_for(s) for q, (s, _) in self.portions.items() if q != "BF16" and s), default=0)
            if biggest > room + self._uploadable_bytes():
                show.note(f"the biggest bite needs {gb(biggest)} - quants that never fit get skipped")

    def _uploadable_bytes(self) -> int:
        """What a burp could free (the BF16 has to stay until the end)."""
        return sum(d.size for d in self.dishes if not d.is_base) if self.hf else 0

    # ── step 3: importance matrix ─────────────────────────────────────────────────

    def _imatrix(self, todo: list[str]) -> Path | None:
        j, show = self.job, self.show
        out = self.work / f"{j.name}.imatrix"
        if j.imatrix == "off":
            return None
        wants = [q for q in todo if q != "BF16" and not self._on_hub(q)]
        if not wants:
            return None
        if (self.state.data.get("imatrix") or {}).get("status") == "done" and out.exists():
            show.step(talk.TASTE, "imatrix from last time")
            self._remember_imatrix(out)
            return out
        hw = hardware.detect(self.work)
        if j.imatrix == "auto" and not hw.gpus:
            show.note("no GPU, so no imatrix - the quants that need one get skipped")
            return None
        if not self.base:
            return None
        source, source_bytes = self.base[0], sum(p.stat().st_size for p in self.base)
        if "Q8_0" in wants:  # half the size of BF16, nearly identical: taste that instead
            dish = self._cook("Q8_0", None)
            if dish and dish.files:
                source, source_bytes = dish.files[0], dish.size
        ngl = self._layers_on_gpu(source_bytes, hw)
        if ngl == 0 and source_bytes > 30 * GB:
            show.note(f"{gb(source_bytes)} is too big to taste without a GPU - no imatrix")
            return None
        calib = self._calibration()
        if calib is None:
            return None
        show.fresh()
        where = "on the GPU" if ngl >= 999 else f"{ngl} layers on the GPU" if ngl else "on the CPU"
        show.step(talk.TASTE, f"imatrix from {source.name}, {where}")
        rc = llama.imatrix(source, calib, out, ngl, j.imatrix_chunks, self.threads, bool(hw.gpus), self.log,
                           progress=lambda d, n: show.progress(talk.TASTE, d, n, "chunks"))
        show.end_bar()
        if rc != 0 or not out.exists():
            show.note(f"the imatrix failed (exit {rc}) - carrying on without it")
            return None
        self.state.data["imatrix"] = {"status": "done"}
        self.state.save()
        show.step(talk.TASTE, talk.DONE)
        self._remember_imatrix(out)
        return out

    def _remember_imatrix(self, path: Path) -> None:
        self.report.imatrix = True
        if self.job.upload_imatrix and f"{self.job.name}.imatrix" not in self.remote:
            self.extras.append(path)

    def _layers_on_gpu(self, model_bytes: int, hw) -> int:
        if not hw.gpus:
            return 0
        vram = hw.total_vram_gb * GB * 0.9
        if model_bytes * 1.15 <= vram:
            return 999
        layers = self.tasted.layers if self.tasted and self.tasted.layers else None
        try:
            layers = layers or json.loads((self.work / "config.json").read_text()).get("num_hidden_layers")
        except (OSError, ValueError):
            pass
        if not layers:
            return 0
        return max(0, int(layers * vram / (model_bytes * 1.15)))

    def _calibration(self) -> Path | None:
        """bartowski's calibration_datav5 (or GGULP_IMATRIX_DATA); wikitext-2 if neither can be had."""
        dest = self.work / "calibration.txt"
        if dest.exists() and dest.stat().st_size > 1000:
            return dest
        src = self.job.imatrix_data or BARTOWSKI_CALIBRATION
        try:
            if Path(src).exists():
                shutil.copyfile(src, dest)
                return dest
            if src.startswith(("http://", "https://")):
                import requests

                resp = requests.get(src, timeout=120)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                return dest
        except Exception as exc:  # noqa: BLE001
            self.show.note(f"couldn't get {src.rsplit('/', 1)[-1]} ({type(exc).__name__}) - using wikitext-2")
        try:
            from huggingface_hub import hf_hub_download
            import pyarrow.parquet as pq

            path = hf_hub_download("Salesforce/wikitext", "wikitext-2-raw-v1/train-00000-of-00001.parquet",
                                   repo_type="dataset", token=self.job.hf_token)
            lines, size = [], 0
            for text in pq.read_table(path, columns=["text"]).column("text").to_pylist():
                if text and text.strip():
                    lines.append(text.strip())
                    size += len(text)
                    if size > 1_700_000:
                        break
            dest.write_text("\n".join(lines), encoding="utf-8")
            return dest
        except Exception as exc:  # noqa: BLE001
            self.show.note(f"no calibration text ({type(exc).__name__}) - no imatrix")
            return None

    # ── step 4: every quant ───────────────────────────────────────────────────────

    def _cook(self, q: str, imat: Path | None, retry: bool = False) -> Dish | None:
        j, show = self.job, self.show
        if any(d.quant == q for d in self.dishes):
            return next(d for d in self.dishes if d.quant == q)
        if self._on_hub(q):
            self.report.skipped[q] = "already on the Hub"
            return None
        size, needs = self.portions.get(q, (None, False))
        if needs and not imat:
            self.report.skipped[q] = "needs an importance matrix"
            show.note(f"{talk.SPAT} {q}: it needs an imatrix")
            return None
        stem = f"{j.name}-{q}"
        made = llama.pieces(self.out, stem)
        if self.state.quant(q).get("status") == "made" and made:
            return self._served(q, made)
        for old in made:
            old.unlink()
        size = size or menu.estimate(q, None, sum(p.stat().st_size for p in self.base)) or 0
        need = self.need_for(size)

        if not retry:
            show.fresh()
        show.step(talk.LOOK, f"{q}  {gb(size)}")
        if j.mode == "smart":
            if self.room() < need:
                show.step(talk.SAVOR, "too big for now")
                self._burp()
                show.step(talk.LOOK, f"{q}  {gb(size)}")
                if self.room() < need:
                    why = f"needs {gb(need)}, only {gb(self.room())} even after resting"
                    self.report.skipped[q] = why
                    show.note(f"{talk.SPAT} {q}: {why}")
                    return None
            show.step(talk.SAVOR, f"fits, {gb(self.room())} free")
        else:
            show.step(talk.SAVOR)
        show.step(talk.GULP)

        final = self.out / f"{stem}.gguf"
        part = self.out / f"{stem}.gguf.part"
        part.unlink(missing_ok=True)
        rc = llama.quantize(self.base[0], part, q, imat, self.threads, self.log,
                            progress=lambda d, n: show.progress(talk.DIGEST, d, n, q))
        show.end_bar()
        if rc != 0:
            part.unlink(missing_ok=True)
            full = self.free_bytes() < MIN_FREE or re.search(r"No space left|ENOSPC|failed to write", llama.tail(self.log, 40))
            if full and j.mode == "smart" and not retry and self.hf:
                show.note(f"ran out of disk in the middle of {q} - resting and trying again")
                self._burp()
                return self._cook(q, imat, retry=True)
            if full and j.mode == "dump":
                self.burst = True
                self.report.failed[q] = "the disk filled up"
                show.note(f"the disk filled up during {q} - no more quants, uploading what's done")
                return None
            self.report.failed[q] = llama.tail(self.log, 12)
            show.note(f"{q} came back up (exit {rc}) - see the log")
            return None
        part.rename(final)
        files = [final]
        if final.stat().st_size > j.split_gb * GB:
            for old in llama.split_parts(self.out, stem):
                old.unlink()
            show.step(talk.DIGEST, f"cutting {q} into pieces for HF")
            rc = llama.split(final, self.out / stem, j.split_gb, self.log)
            parts = llama.split_parts(self.out, stem)
            if rc == 0 and parts:
                final.unlink()
                files = parts
            else:
                for p in parts:
                    p.unlink()
                self.report.failed[q] = f"made, but cutting it into {llama.size_arg(j.split_gb)}B pieces failed"
                show.note(f"{q}: {self.report.failed[q]}")
                if j.mode == "smart":
                    final.unlink()
                return None
        self.state.mark(q, "made", files=[p.name for p in files])
        return self._served(q, files)

    def _served(self, q: str, files: list[Path]) -> Dish:
        size = sum(p.stat().st_size for p in files)
        dish = Dish(q, files, size)
        self.dishes.append(dish)
        self.report.made.append((q, size))
        self.show.burp(q)
        return dish

    # ── step 5: up to the Hub ─────────────────────────────────────────────────────

    def _burp(self, final: bool = False) -> None:
        """HMMMPH... I'm full: upload everything made so far, then rest."""
        j, show = self.job, self.show
        if not self.hf:
            return
        if j.mode == "dump" and not final:
            return
        todo = [d for d in self.dishes if not d.uploaded and d.files]
        extras = [p for p in self.extras if p.exists()] if final else []
        if not todo and not extras:
            return
        show.full()
        n = len(todo) + len(extras)
        absorbed = []
        for i, d in enumerate(todo):
            show.progress(talk.ABSORB, i, n, f"{d.quant}  {gb(d.size)}")
            hub.push(self.hf, j.repo_id, d.files, f"Add {j.name}-{d.quant}", on_retry=show.note)
            d.uploaded = True
            absorbed.append(d.quant)
            self.report.uploaded.append(d.quant)
            self.state.mark(d.quant, "uploaded", files=[p.name for p in d.files])
            if j.mode == "smart" and (not d.is_base or final):
                for p in d.files:
                    p.unlink(missing_ok=True)
                d.files = []
        for k, p in enumerate(extras):
            show.progress(talk.ABSORB, len(todo) + k, n, "imatrix")
            hub.push(self.hf, j.repo_id, [p], f"Add the importance matrix for {j.name}", on_retry=show.note)
            self.extras.remove(p)
        show.progress(talk.ABSORB, n, n, f"{n} on HF")
        show.end_bar()
        if not final:
            show.rested(absorbed)
        else:
            show.absorbed += len(absorbed)
            show.pile = []

    def _readme(self) -> None:
        readme = self.work / "README.md"
        if not self.hf:
            return
        if not readme.exists():
            self.report.readme = talk.NO_README
            self.show.note(talk.NO_README)
            return
        hub.push_readme(self.hf, self.job.repo_id, readme, self.tasted.ref if self.tasted else self.job.model)
        self.report.readme = talk.README_COPIED
        self.show.note(talk.README_COPIED)

    def _clean_up(self) -> None:
        j = self.job
        keep_base = any(d.is_base for d in self.dishes) and (j.mode == "dump" or not self.hf)
        if self.base_is_ours and not keep_base:
            for p in self.base:
                p.unlink(missing_ok=True)
        if j.mode == "smart" and self.hf:
            for p in (self.work / "calibration.txt", self.work / f"{j.name}.imatrix"):
                p.unlink(missing_ok=True)
        if list(self.out.glob("*.gguf")):
            self.report.kept_in = self.out

    def _ping_done(self) -> None:
        r = self.report
        lines = [f"{len(r.made)} GGUFs made, {len(r.uploaded)} uploaded, {r.minutes:.0f} min"]
        if r.repo_url:
            lines.append(r.repo_url)
        if r.skipped:
            lines.append("skipped: " + ", ".join(r.skipped))
        if r.failed:
            lines.append("failed: " + ", ".join(r.failed))
        self.notify.send(f"{r.name}: {talk.ALL_GULPED}", "\n".join(lines), ok=not r.failed)
