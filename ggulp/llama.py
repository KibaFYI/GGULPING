"""llama.cpp tools: convert_hf_to_gguf.py, llama-quantize, llama-imatrix, llama-gguf-split.

The image has two builds: /opt/llama.cpp/build (CPU only, works everywhere - quantize and split
don't use a GPU anyway) and /opt/llama.cpp/build-cuda (llama-imatrix on the GPUs).
Every tool can be swapped for tests with GGULP_<TOOL>_BIN."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

LLAMA = Path(os.environ.get("GGULP_LLAMA_DIR", "/opt/llama.cpp"))


def _cmd(env_name: str, default: list[str]) -> list[str]:
    override = os.environ.get(env_name)
    return shlex.split(override) if override else default


def quantize_cmd() -> list[str]:
    return _cmd("GGULP_QUANTIZE_BIN", [str(LLAMA / "build" / "bin" / "llama-quantize")])


def split_cmd() -> list[str]:
    return _cmd("GGULP_SPLIT_BIN", [str(LLAMA / "build" / "bin" / "llama-gguf-split")])


def imatrix_cmd(gpu: bool) -> list[str]:
    cuda = LLAMA / "build-cuda" / "bin" / "llama-imatrix"
    default = cuda if gpu and cuda.exists() else LLAMA / "build" / "bin" / "llama-imatrix"
    return _cmd("GGULP_IMATRIX_BIN", [str(default)])


def convert_cmd() -> list[str]:
    return _cmd("GGULP_CONVERT_BIN", [str(LLAMA / "venv" / "bin" / "python"), str(LLAMA / "convert_hf_to_gguf.py")])


def version() -> str:
    try:
        return (LLAMA / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


# ── running a tool ────────────────────────────────────────────────────────────────

def run(cmd: list, log_path: Path, on_text: Callable[[str], None] | None = None) -> int:
    """Run a tool, keep everything it prints in the log, feed it to on_text (progress bars)."""
    cmd = [str(c) for c in cmd]
    with open(log_path, "ab") as log:
        log.write(("\n$ " + " ".join(shlex.quote(c) for c in cmd) + "\n").encode())
        log.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            log.write(f"could not start {cmd[0]}: {exc}\n".encode())
            return 127
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read1(65536)  # type: ignore[attr-defined]
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                if on_text:
                    on_text(chunk.decode("utf-8", errors="replace"))
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise


def tail(log_path: Path, lines: int = 25) -> str:
    try:
        raw = log_path.read_bytes()[-60000:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    clean = [ln.split("\r")[-1].rstrip() for ln in raw.replace("\r\n", "\n").split("\n")]
    return "\n".join([ln for ln in clean if ln.strip()][-lines:])


Progress = Callable[[int, int], None]  # (done, total)


class Tracker:
    """Turns a tool's output into (done, total) for the progress bar."""

    _TENSOR = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")      # llama-quantize: [ 12/ 291] blk.0...
    _PCT = re.compile(r"(\d{1,3})%\|")                          # converter (tqdm): Writing:  45%|
    _CHUNK = re.compile(r"\[(\d+)\]\s*-?\d")                    # llama-imatrix: [12]5.4321,
    _CHUNKS = re.compile(r"computing over (\d+) chunks")         # llama-imatrix: how many there will be

    def __init__(self, kind: str, callback: Progress | None, total: int = 100):
        self.kind, self.callback, self.total = kind, callback, total
        self._buf = ""

    def feed(self, text: str) -> None:
        if not self.callback:
            return
        self._buf = (self._buf + text)[-4000:]
        if self.kind == "tensors":
            hits = self._TENSOR.findall(self._buf)
            if hits:
                done, total = (int(x) for x in hits[-1])
                self.callback(done, total)
        elif self.kind == "chunks":
            m = self._CHUNKS.search(self._buf)
            if m:
                self.total = int(m.group(1))
            hits = self._CHUNK.findall(self._buf)
            if hits:
                self.callback(max(int(h) for h in hits), self.total)
        else:
            hits = self._PCT.findall(self._buf)
            if hits:
                self.callback(min(100, int(hits[-1])), 100)


# ── the tools ─────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def available_types() -> frozenset[str]:
    """Quant types this llama-quantize knows (read from its --help)."""
    try:
        out = subprocess.run(quantize_cmd() + ["--help"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    text = (out.stdout or "") + (out.stderr or "")
    return frozenset(re.findall(r"^\s+(?:\d+\s+or\s+)?([A-Z][A-Z0-9_]+)\s+:", text, re.M))


def dry_run(base: Path, q: str, base_bytes: int, imatrix: Path | None = None) -> tuple[int | None, bool]:
    """(size in bytes, needs an imatrix) without making the file.

    --dry-run reports the tensor data of the original ("model size") and of the quant ("quant size").
    Everything else in the file (vocab, chat template, metadata) is the same in both, so it's
    measured once from the BF16 file and added on top."""
    cmd = quantize_cmd() + ["--dry-run"] + (["--imatrix", str(imatrix)] if imatrix else []) + [str(base), q]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    text = (out.stdout or "") + (out.stderr or "")
    quant = re.search(r"quant size\s*=\s*([\d.]+)\s*MiB", text)
    model = re.search(r"model size\s*=\s*([\d.]+)\s*MiB", text)
    needs = "will require an imatrix" in text or "requires an importance matrix" in text
    if not quant:
        return None, needs
    extra = max(0, base_bytes - int(float(model.group(1)) * 1024**2)) if model else 16 * 1024**2
    return int(float(quant.group(1)) * 1024**2) + extra + 1024**2, needs


def size_arg(gb: float) -> str:
    """45 -> '45G', 0.1 -> '100M' (llama.cpp counts in 1000s)."""
    return f"{int(gb)}G" if gb >= 1 and float(gb).is_integer() else f"{max(1, int(gb * 1000))}M"


def convert(src: Path, outfile: Path, split_gb: float, log: Path, mistral: bool = False,
            progress: Progress | None = None) -> int:
    cmd = convert_cmd() + [src, "--outtype", "bf16", "--outfile", outfile, "--split-max-size", size_arg(split_gb)]
    if mistral:
        cmd.append("--mistral-format")
    return run(cmd, log, Tracker("percent", progress).feed)


def quantize(base: Path, out: Path, q: str, imatrix: Path | None, threads: int, log: Path,
             progress: Progress | None = None) -> int:
    cmd = quantize_cmd() + (["--imatrix", imatrix] if imatrix else []) + [base, out, q]
    if threads:
        cmd.append(str(threads))
    return run(cmd, log, Tracker("tensors", progress).feed)


def imatrix(model: Path, calib: Path, out: Path, ngl: int, chunks: int, threads: int, gpu: bool, log: Path,
            progress: Progress | None = None) -> int:
    """chunks=0 runs the whole calibration file."""
    cmd = imatrix_cmd(gpu) + ["-m", model, "-f", calib, "-o", out, "--output-format", "gguf", "-ngl", str(ngl)]
    if chunks:
        cmd += ["--chunks", str(chunks)]
    if threads:
        cmd += ["-t", str(threads)]
    return run(cmd, log, Tracker("chunks", progress, total=chunks or 100).feed)


def split(path: Path, prefix: Path, split_gb: float, log: Path) -> int:
    return run(split_cmd() + ["--split", "--split-max-size", size_arg(split_gb), path, prefix], log)


def pieces(folder: Path, stem: str) -> list[Path]:
    """<stem>.gguf, or its split parts <stem>-00001-of-0000N.gguf."""
    single = folder / f"{stem}.gguf"
    if single.exists():
        return [single]
    return split_parts(folder, stem)


def split_parts(folder: Path, stem: str) -> list[Path]:
    return sorted(folder.glob(f"{glob_escape(stem)}-[0-9][0-9][0-9][0-9][0-9]-of-[0-9][0-9][0-9][0-9][0-9].gguf"))


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)
