"""Hugging Face: look at the model before eating it, download it, and upload the GGUFs."""

from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from forge import fetch

from .menu import gb

# never needed for a GGUF: other formats, and the original/ duplicates some repos ship
ALWAYS_SKIP = [
    "*.gguf", "*.onnx", "onnx/*", "*.tflite", "*.mlmodel", "*.mlpackage/*", "coreml/*", "openvino/*",
    "*.msgpack", "*.h5", "*.ot", "flax_model*", "tf_model*", "original/*", "*.nemo",
]
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")


@dataclass
class Taste:
    """What the model is, found out before downloading it."""

    link: str
    kind: str = "hf"                   # hf | local (model folder) | gguf (already a GGUF)
    ref: str = ""                      # repo id, or the local path
    revision: str | None = None
    params: int | None = None
    weight_bytes: int = 0              # size of the weights that get downloaded / converted
    download_bytes: int = 0
    is_moe: bool = False
    layers: int | None = None
    arch: str = ""
    has_readme: bool = False
    mistral_format: bool = False
    skip: list[str] = field(default_factory=list)  # ignore_patterns for the download

    @property
    def short_name(self) -> str:
        tail = self.ref.rstrip("/").split("/")[-1]
        for suffix in (".gguf",):
            if tail.endswith(suffix):
                tail = tail[: -len(suffix)]
        return tail or "model"

    def label(self) -> str:
        bits = [self.arch or self.kind]
        if self.params:
            bits.append(f"{self.params / 1e9:.1f}B params" if self.params >= 1e9 else f"{self.params / 1e6:.0f}M params")
        if self.weight_bytes:
            bits.append(f"{gb(self.weight_bytes)} of weights")
        if self.is_moe:
            bits.append("MoE")
        return " · ".join(bits)


def _config_facts(cfg: dict, taste: Taste) -> None:
    sources = [cfg] + [cfg[k] for k in ("text_config", "language_config", "llm_config") if isinstance(cfg.get(k), dict)]
    for c in sources:
        if any(c.get(k) for k in ("num_experts", "num_local_experts", "n_routed_experts", "moe_num_experts", "num_experts_per_tok")):
            taste.is_moe = True
        taste.layers = taste.layers or c.get("num_hidden_layers") or c.get("n_layer") or c.get("num_layers")
    archs = cfg.get("architectures") or []
    taste.arch = archs[0] if archs else cfg.get("model_type", "")


def safetensors_params(path: Path) -> int:
    with open(path, "rb") as fh:
        (n,) = struct.unpack("<Q", fh.read(8))
        header = json.loads(fh.read(n))
    return sum(math.prod(v.get("shape") or [1]) for k, v in header.items() if k != "__metadata__")


def taste(link: str, token: str | None) -> Taste:
    """Look at a model without eating it. Raises ValueError with a friendly message."""
    src = fetch.classify(link, "model")
    t = Taste(link=link, ref=src.ref, revision=src.revision)
    if src.kind == "local":
        path = Path(src.ref)
        if path.is_file() and path.suffix == ".gguf":
            t.kind = "gguf"
            parts = [path] if "-00001-of-" not in path.name else sorted(path.parent.glob(path.name.replace("-00001-of-", "-*-of-")))
            t.weight_bytes = sum(p.stat().st_size for p in parts)
            t.arch = "GGUF"
            return t
        if not path.is_dir():
            raise ValueError(f"{path} is not a model folder or a .gguf file")
        t.kind = "local"
        weights = [p for p in path.rglob("*") if p.suffix in WEIGHT_SUFFIXES and p.is_file()]
        if not weights:
            raise ValueError(f"no model weights (.safetensors / .bin) in {path}")
        st = [p for p in weights if p.suffix == ".safetensors"]
        use = st or weights
        t.weight_bytes = t.download_bytes = sum(p.stat().st_size for p in use)
        try:
            t.params = sum(safetensors_params(p) for p in st) or None
        except (OSError, ValueError, struct.error):
            t.params = None
        if (path / "config.json").exists():
            _config_facts(json.loads((path / "config.json").read_text(encoding="utf-8")), t)
        t.mistral_format = not (path / "config.json").exists() and (path / "params.json").exists()
        t.has_readme = (path / "README.md").exists()
        return t
    if src.kind != "hf" or src.repo_type != "model":
        raise ValueError("give me a Hugging Face model (id or link), a model folder or a .gguf on this pod")

    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi(token=token)
    try:
        info = api.model_info(src.ref, revision=src.revision, files_metadata=True)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "Gated" in name:
            raise ValueError(f"{src.ref} is gated - accept its terms on huggingface.co with this account") from exc
        if "NotFound" in name or "404" in str(exc):
            raise ValueError(f"{src.ref} not found (typo? private models need a token with access)") from exc
        raise ValueError(f"could not look at {src.ref}: {exc}") from exc
    files = {s.rfilename: (s.size or 0) for s in (info.siblings or [])}
    has_st = any(f.endswith(".safetensors") for f in files)
    t.skip = list(ALWAYS_SKIP) + (["*.bin", "*.pt", "*.pth", "consolidated*"] if has_st else [])
    if has_st and not any(f.endswith(".safetensors") and not f.startswith("consolidated") for f in files):
        t.skip.remove("consolidated*")  # mistral-format repos only have consolidated.safetensors
    kept = {f: s for f, s in files.items() if not _skipped(f, t.skip)}
    t.download_bytes = sum(kept.values())
    t.weight_bytes = sum(s for f, s in kept.items() if f.endswith(WEIGHT_SUFFIXES))
    st_meta = getattr(info, "safetensors", None)
    t.params = int(st_meta.total) if st_meta and getattr(st_meta, "total", None) else None
    t.has_readme = "README.md" in files
    t.mistral_format = "config.json" not in files and "params.json" in files
    if "config.json" in files:
        try:
            cfg_path = hf_hub_download(src.ref, "config.json", revision=src.revision, token=token)
            _config_facts(json.loads(Path(cfg_path).read_text(encoding="utf-8")), t)
        except Exception:  # noqa: BLE001
            pass
    if not t.weight_bytes:
        raise ValueError(f"{src.ref} has no .safetensors or .bin weights to convert")
    return t


def _skipped(name: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(name, p) for p in patterns)


def download(t: Taste, dest: Path, token: str | None) -> Path:
    from huggingface_hub import snapshot_download

    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(t.ref, revision=t.revision, local_dir=dest, token=token, ignore_patterns=t.skip)
    return dest


# ── uploading ─────────────────────────────────────────────────────────────────────

def api(token: str | None):
    from huggingface_hub import HfApi

    return HfApi(token=token)


def ensure_repo(hf, repo_id: str, private: bool) -> str:
    hf.create_repo(repo_id, private=private, exist_ok=True, repo_type="model")
    return f"https://huggingface.co/{repo_id}"


def remote_files(hf, repo_id: str) -> set[str]:
    try:
        return set(hf.list_repo_files(repo_id))
    except Exception:  # noqa: BLE001
        return set()


def push(hf, repo_id: str, files: list[Path], message: str, tries: int = 4, on_retry=None) -> None:
    """One commit with these files (named as they are on disk). Retries; the Hub resumes big files."""
    from huggingface_hub import CommitOperationAdd

    ops = [CommitOperationAdd(path_in_repo=f.name, path_or_fileobj=str(f)) for f in files]
    for attempt in range(1, tries + 1):
        try:
            hf.create_commit(repo_id, operations=ops, commit_message=message)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == tries:
                raise
            wait = 20 * attempt
            if on_retry:
                on_retry(f"upload hiccup ({type(exc).__name__}: {str(exc)[:120]}), trying again in {wait}s")
            time.sleep(wait)


def push_readme(hf, repo_id: str, readme: Path, source: str) -> None:
    """The original README, byte for byte."""
    from huggingface_hub import CommitOperationAdd

    hf.create_commit(
        repo_id,
        operations=[CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=readme.read_bytes())],
        commit_message=f"README from {source}",
    )
