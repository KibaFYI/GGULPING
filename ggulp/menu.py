"""The quant menu: which GGUFs get made, in what order, and roughly how big each one is.

Rough sizes are only for the setup screen. During a run, `llama-quantize --dry-run` gives the
exact size of every quant before it is made."""

from __future__ import annotations

import re

from .config import GB

# biggest first: BF16 comes straight from the converter, Q8_0 is also what the imatrix is tasted on
BUFFET = [
    "BF16", "Q8_0", "Q6_K", "Q5_1", "Q5_K_M", "Q5_K_S", "Q5_0", "Q4_1", "Q4_K_M", "Q4_K_S", "IQ4_NL",
    "Q4_0", "IQ4_XS", "Q3_K_L", "Q3_K_M", "IQ3_M", "Q3_K_S", "IQ3_S", "IQ3_XS", "IQ3_XXS", "Q2_K",
    "Q2_K_S", "IQ2_M", "IQ2_S", "IQ2_XS", "IQ2_XXS", "IQ1_M", "IQ1_S",
]
SNACK = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "IQ4_XS", "Q3_K_M", "Q2_K"]
MOE_ONLY = ["MXFP4_MOE"]  # added to the buffet for mixture-of-experts models
# real llama.cpp types that are never on the buffet: special models only, or no point uploading
EXTRAS = ["F16", "F32", "Q1_0", "Q2_0", "TQ1_0", "TQ2_0", "MXFP4_MOE"]
ALIASES = {"Q4_K": "Q4_K_M", "Q5_K": "Q5_K_M", "Q3_K": "Q3_K_M", "FP16": "F16", "BFLOAT16": "BF16"}

# approximate bits per weight in a finished file (embeddings and the output layer included)
BPW = {
    "F32": 32.0, "BF16": 16.0, "F16": 16.0, "Q8_0": 8.5, "Q6_K": 6.56, "Q5_1": 6.0, "Q5_K_M": 5.69,
    "Q5_K_S": 5.54, "Q5_0": 5.5, "Q4_1": 5.0, "Q4_K_M": 4.89, "Q4_K_S": 4.58, "IQ4_NL": 4.56, "Q4_0": 4.55,
    "MXFP4_MOE": 4.6, "IQ4_XS": 4.32, "Q3_K_L": 4.27, "Q3_K_M": 3.91, "IQ3_M": 3.76, "Q3_K_S": 3.5,
    "IQ3_S": 3.52, "IQ3_XS": 3.3, "IQ3_XXS": 3.06, "Q2_K": 3.0, "Q2_K_S": 2.8, "IQ2_M": 2.7, "IQ2_S": 2.5,
    "IQ2_XS": 2.31, "IQ2_XXS": 2.06, "IQ1_M": 1.75, "IQ1_S": 1.56, "TQ2_0": 2.06, "TQ1_0": 1.69,
    "Q2_0": 2.25, "Q1_0": 1.125,
}
# these can't be made without an importance matrix (checked again with --dry-run during the run)
NEEDS_IMATRIX = {"IQ1_S", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ2_M", "IQ3_XXS", "Q2_K_S"}


def normalize(name: str) -> str:
    q = name.strip().upper().replace("-", "_")
    return ALIASES.get(q, q)


def parse_custom(text: str) -> tuple[list[str], list[str]]:
    """'q4_k_m, Q8_0 iq4xs' -> (known quants in size order, unknown words)."""
    known = set(BUFFET) | set(EXTRAS)
    picked, unknown = [], []
    for word in re.split(r"[\s,;]+", text.strip()):
        if not word:
            continue
        q = normalize(word)
        if q not in known:
            q2 = next((k for k in known if k.replace("_", "") == q.replace("_", "")), None)  # iq4xs -> IQ4_XS
            if not q2:
                unknown.append(word)
                continue
            q = q2
        if q not in picked:
            picked.append(q)
    return order(picked), unknown


def order(quants: list[str]) -> list[str]:
    """Biggest first, the way they get made."""
    return sorted(dict.fromkeys(quants), key=lambda q: (-BPW.get(q, 0), BUFFET.index(q) if q in BUFFET else 99))


def resolve(menu: str, custom: list[str], is_moe: bool, available: set[str] | None = None) -> list[str]:
    if menu == "snack":
        picked = list(SNACK)
    elif menu == "custom":
        picked, _ = parse_custom(" ".join(custom))
    else:
        picked = list(BUFFET) + (MOE_ONLY if is_moe else [])
    if available:  # what this llama.cpp build can make (BF16 always comes from the converter)
        picked = [q for q in picked if q == "BF16" or q in available]
    return order(picked)


def estimate(q: str, params: int | None, bf16_bytes: int | None = None) -> int | None:
    """Rough file size in bytes."""
    bpw = BPW.get(q)
    if not bpw:
        return None
    if params:
        return int(params * bpw / 8 * 1.01)
    if bf16_bytes:
        return int(bf16_bytes * bpw / 16 * 1.01)
    return None


def gb(n: int | float | None) -> str:
    if n is None:
        return "?"
    value = n / GB
    if value >= 100:
        return f"{value:,.0f} GB"
    if value >= 10:
        return f"{value:.1f} GB"
    if value >= 1:
        return f"{value:.2f} GB"
    return f"{n / 1000**2:,.0f} MB"
