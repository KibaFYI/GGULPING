"""One GGULPING job: what to eat, what to call it, where it goes and how hungry we are."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

GB = 1000**3

# bartowski's calibration text (the one his imatrix quants use); wikitext-2 if it can't be reached
BARTOWSKI_CALIBRATION = (
    "https://gist.githubusercontent.com/bartowski1182/82ae9b520227f57d79ba04add13d0d0d/raw/calibration_datav5.txt"
)

MODES = ("smart", "dump")
MENUS = ("buffet", "snack", "custom")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = re.sub(r"\s+#.*$", "", value).strip()  # docker env-files keep inline comments
    return value if value != "" else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _float(name: str) -> float | None:
    value = _env(name)
    try:
        return float(value) if value else None
    except ValueError:
        return None


@dataclass
class Job:
    model: str = ""                     # HF id / link, a model folder on this pod, or a .gguf
    name: str = ""                      # static: repo <namespace>/<name>-GGUF, files <name>-<QUANT>.gguf
    namespace: str | None = None        # HF user or org; None = keep everything on this pod
    mode: str = "smart"                 # smart | dump
    menu: str = "buffet"                # buffet | snack | custom
    custom: list[str] = field(default_factory=list)
    imatrix: str = "auto"               # auto (on when there is a GPU) | on | off
    private: bool = True
    stomach_gb: float | None = None     # smart mode: never use more than this many GB (None = all free space)
    webhook: str | None = None
    hf_token: str | None = None
    home: Path = field(default_factory=lambda: Path("/workspace/ggulp"))
    split_gb: float = 45.0              # bigger files are split (the Hub takes files up to 50 GB)
    reserve_gb: float = 2.0             # always leave this much free
    imatrix_data: str | None = None     # calibration text: path or https:// url (default: bartowski's v5)
    imatrix_chunks: int = 200           # chunks of 512 tokens to taste; 0 = the whole calibration file
    upload_imatrix: bool = True
    threads: int = 0                    # 0 = all cores

    @property
    def repo_id(self) -> str | None:
        return f"{self.namespace}/{self.name}-GGUF" if self.namespace and self.name else None

    @property
    def workdir(self) -> Path:
        return self.home / self.name

    @classmethod
    def from_env(cls) -> "Job":
        j = cls()
        j.model = _env("GGULP_MODEL", "") or ""
        j.name = _env("GGULP_NAME", "") or ""
        j.namespace = _env("GGULP_NAMESPACE")
        j.mode = (_env("GGULP_MODE", "smart") or "smart").lower()
        quants = _env("GGULP_QUANTS", "buffet") or "buffet"
        if quants.lower() in ("buffet", "all", "everything"):
            j.menu = "buffet"
        elif quants.lower() in ("snack", "classics"):
            j.menu = "snack"
        else:
            j.menu, j.custom = "custom", re.split(r"[\s,]+", quants.strip())
        j.imatrix = (_env("GGULP_IMATRIX", "auto") or "auto").lower()
        j.private = _bool("GGULP_PRIVATE", True)
        j.stomach_gb = _float("GGULP_STOMACH_GB")
        j.webhook = _env("GGULP_DISCORD_WEBHOOK") or _env("FORGE_DISCORD_WEBHOOK")
        j.hf_token = _env("HF_TOKEN") or _env("HUGGING_FACE_HUB_TOKEN")
        j.home = Path(_env("GGULP_HOME", "/workspace/ggulp") or "/workspace/ggulp")
        j.split_gb = _float("GGULP_SPLIT_GB") or 45.0
        j.reserve_gb = _float("GGULP_RESERVE_GB") if _float("GGULP_RESERVE_GB") is not None else 2.0
        j.imatrix_data = _env("GGULP_IMATRIX_DATA")
        chunks = _float("GGULP_IMATRIX_CHUNKS")
        j.imatrix_chunks = 200 if chunks is None else int(chunks)  # 0 = all of it
        j.upload_imatrix = _bool("GGULP_UPLOAD_IMATRIX", True)
        j.threads = int(_float("GGULP_THREADS") or 0)
        return j

    def check(self) -> None:
        if not self.model:
            raise ValueError("no model given (GGULP_MODEL)")
        if not self.name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$", self.name):
            raise ValueError(f"bad model name {self.name!r}: letters, numbers, . _ - only")
        if self.mode not in MODES:
            raise ValueError(f"mode must be smart or dump, not {self.mode!r}")
        if self.imatrix not in ("auto", "on", "off"):
            raise ValueError(f"GGULP_IMATRIX must be auto, on or off, not {self.imatrix!r}")
