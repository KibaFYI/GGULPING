# syntax=docker/dockerfile:1.7
# ───────────────────────────────────────────────────────────────────────────────
#   GGULPING · every llama.cpp GGUF of a model, up to the Hugging Face Hub
#
#   Built on the axolotl-forge image (CUDA 13 · Python 3.12 · uv · the purple shell · the wolfgon)
#   + llama.cpp twice: a CPU build (quantize / split - works on any pod)
#                      a CUDA build of llama-imatrix (tastes the model on the GPUs)
#   + llama.cpp's converter in its own venv (it pins its own transformers / torch)
# ───────────────────────────────────────────────────────────────────────────────
ARG BASE_IMAGE=ghcr.io/kibafyi/axolotl-forge:latest
ARG CUDA_VERSION=13.0.3

# ── llama.cpp, built on its own so a new axolotl-forge image doesn't rebuild it ──
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS llama

# pinned llama.cpp release (the GitHub build can swap in the newest one)
ARG LLAMA_CPP_REF=b12370
# sm_80 code also runs on 8.6 / 8.9 (A40, 3090, 4090, L40S), sm_100 on B300, sm_120 on DGX Spark
ARG LLAMA_CUDA_ARCHS="80-real;90-real;100-real;120-real"
ARG BUILD_JOBS=4

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git cmake ninja-build build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp /opt/llama.cpp \
 && echo "${LLAMA_CPP_REF}" > /opt/llama.cpp/VERSION

# CPU build: llama-quantize, llama-gguf-split, llama-imatrix (CPU fallback).
# GGML_NATIVE=OFF keeps it portable; AVX/AVX2/FMA/F16C stay on (every cloud x86 CPU has them).
RUN cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_CUDA=OFF \
        -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF \
 && cmake --build /opt/llama.cpp/build -j "${BUILD_JOBS}" --target llama-quantize llama-gguf-split llama-imatrix \
 && (/opt/llama.cpp/build/bin/llama-quantize --help 2>&1 || true) | grep -q "Q4_K_M" \
 && echo "llama-quantize (CPU) ok"

# CUDA build: llama-imatrix on the GPUs (links the CUDA driver stub; the real driver comes from the pod)
RUN cmake -S /opt/llama.cpp -B /opt/llama.cpp/build-cuda -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${LLAMA_CUDA_ARCHS}" \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,--allow-shlib-undefined -L/usr/local/cuda/lib64/stubs" \
        -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF \
 && cmake --build /opt/llama.cpp/build-cuda -j "${BUILD_JOBS}" --target llama-imatrix

# keep the source tree (the converter and gguf-py live in it) and the three tools; drop the build leftovers
RUN mkdir -p /out/llama.cpp \
 && tar -C /opt/llama.cpp --exclude=./build --exclude=./build-cuda --exclude=./.git -cf - . | tar -C /out/llama.cpp -xf - \
 && mkdir -p /out/llama.cpp/build/bin /out/llama.cpp/build-cuda/bin \
 && cp /opt/llama.cpp/build/bin/llama-quantize /opt/llama.cpp/build/bin/llama-gguf-split /opt/llama.cpp/build/bin/llama-imatrix \
       /out/llama.cpp/build/bin/ \
 && cp /opt/llama.cpp/build-cuda/bin/llama-imatrix /out/llama.cpp/build-cuda/bin/

# ── GGULPING ──────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

COPY --from=llama /out/llama.cpp /opt/llama.cpp

# the converter (convert_hf_to_gguf.py) in its own venv, with llama.cpp's pinned requirements
RUN uv venv --python 3.12 /opt/llama.cpp/venv \
 && VIRTUAL_ENV=/opt/llama.cpp/venv uv pip install --index-strategy unsafe-best-match \
        -r /opt/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt \
 && /opt/llama.cpp/venv/bin/python /opt/llama.cpp/convert_hf_to_gguf.py --help > /dev/null \
 && echo "converter ok"

ENV PYTHONPATH=/opt/ggulp:/opt/forge \
    GGULP_HOME=/workspace/ggulp \
    GGULP_LLAMA_DIR=/opt/llama.cpp \
    PATH=/opt/llama.cpp/build/bin:${PATH}

COPY ggulp /opt/ggulp/ggulp
COPY rootfs/ /
RUN for f in /usr/local/bin/ggulp /usr/local/bin/ggulp-entrypoint /root/.zshrc /root/.bashrc; do sed -i 's/\r$//' "$f"; done \
 && chmod +x /usr/local/bin/ggulp /usr/local/bin/ggulp-entrypoint \
 && ggulp menu \
 && echo "GGULPING build: llama.cpp $(cat /opt/llama.cpp/VERSION) on ${BASE_IMAGE}" >> /opt/ggulp/build-notes.txt

WORKDIR /workspace
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/ggulp-entrypoint"]
CMD []
