# ComfyUI Single-H100 Benchmark

Benchmark date: 2026-07-24

## Workload

- GPU: NVIDIA H100 SXM 80 GB HBM3
- Worker: `runpod/worker-comfyui:5.8.6-base-cuda12.8.1`
- ComfyUI: 0.25.0
- PyTorch: 2.11.0+cu128
- Model: Wan 2.2 I2V A14B high/low-noise scaled FP8
- Output: 576x800, 81 frames, 16 fps
- Sampling: 20 Euler/simple steps, split 10/10, CFG 3.5, shift 8.0
- Seed, source image, prompts, VAE, encoder, and VP9 settings fixed

Each measured render used a different remote input filename to invalidate
ComfyUI's node cache while keeping the input bytes identical.

## Results

| Configuration | Warm wall time | Relative to baseline | Result |
| --- | ---: | ---: | --- |
| Default DynamicVRAM and PyTorch SDPA | 174.87 s / 175.17 s | 1.00x | Baseline |
| `--highvram` | 175.03 s | 1.00x | No benefit |
| Triton FP8, manual process | 159.19 s | 1.10x | Faster |
| Triton FP8, profile-managed | 160.87 s | 1.09x | Selected |
| `torch.compile`, Inductor | N/A | N/A | Incompatible with scaled FP8 tensors |
| `torch.compile`, CUDA graphs | N/A | N/A | Same tensor-subclass failure |

The baseline ComfyUI execution itself took 159.80 seconds. The final Triton
execution took 144.25 seconds. CLI setup, tunnel, upload, and download overhead
was approximately 15 seconds for a standalone `run` invocation. A multi-shot
`scene` keeps one worker session open, so most setup overhead is paid only once.

During sampling, the H100 sustained 96-100% SM utilization and approximately
681-699 W. Idle model memory was about 34.8 GB and peak memory about 44.9 GB.
Both Wan experts were already resident, explaining why `--highvram` did not
help.

Continuation extraction from the completed WebM took 0.28 seconds. Moving it
to the Pod would add complexity without a material critical-path improvement.

## Output Validation

- Two default runs produced bit-identical decoded frames across all 81 frames.
- Two Triton runs also produced bit-identical decoded frames.
- Default versus Triton: SSIM 0.886435 and PSNR 25.4428 dB.
- Side-by-side frames showed comparable detail and artifact levels, but motion
  and pose trajectories differed after the first frame.

Triton therefore preserves the observed visual quality but not bit-identical
output. Remove `--enable-triton-backend` from the profile if exact reproduction
of an older non-Triton render is required.

## Rejected Options

### High VRAM

ComfyUI 0.25.0 DynamicVRAM already retained both FP8 experts on the 80 GB H100.
`--highvram` measured within 0.01% of the baseline.

### Torch Compile

Both available `TorchCompileModel` backends failed before sampling. Inductor
initially required a compiler and Python headers. After installing them, both
Inductor and CUDA graphs failed while Dynamo deep-copied ComfyUI's FP8
`QuantizedTensor` metadata:

```text
Cannot access data pointer of Tensor (e.g. FakeTensor, FunctionalTensor)
```

The compile nodes were not retained in the workflow.

### Attention Replacement

The worker already uses PyTorch SDPA. Sampling saturated the H100. External
FlashAttention, xFormers, and SageAttention packages were not present. Sage was
excluded because it quantizes attention and does not meet the unchanged-quality
constraint.

## Selected Configuration

The Wan profile enables ComfyUI's Triton backend:

```json
"comfy_args": ["--enable-triton-backend"]
```

The custom GHCR worker image already contains the compiler packages required by
Triton's runtime. The orchestrator never invokes a package manager on the GPU
worker. It restarts only the ComfyUI process when the requested argument
configuration is not active.
