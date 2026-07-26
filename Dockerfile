ARG RUNPOD_BASE_IMAGE=runpod/worker-comfyui:5.8.6-base-cuda12.8.1@sha256:1d4281e01c2bf93762d2d799edb3be4d169a7f9cfdd16ce2d3a6c68dbc9fcb6f
FROM ${RUNPOD_BASE_IMAGE}

ARG RUNPOD_BASE_IMAGE

LABEL org.opencontainers.image.title="RunPod ComfyUI Worker" \
      org.opencontainers.image.description="RunPod ComfyUI worker with preinstalled system dependencies" \
      org.opencontainers.image.base.name="${RUNPOD_BASE_IMAGE}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*
