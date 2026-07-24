# Prompt Refiner

Prompt refinement is an optional preprocessing step for scene manifests. It uses
a locally hosted open-weight language model on the RunPod worker and never sends
scene text to a third-party inference API. The feature is disabled unless
`refine` or `scene --refine-prompts` is selected.

The default profile pins all remote artifacts by URL, byte size, and SHA-256:

- KoboldCpp `v1.117.1`
- `Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP`
  in `Q4_K_M` GGUF format
- a 65,536-token context and deterministic seed

The complete [`scene-manifest-reference.md`](scene-manifest-reference.md) is
included in the model's system context. The shorter system prompt constrains the
response to a strict JSON overlay.

## Install Artifacts

Preload the normal model groups and the prompt-refiner artifacts without running
inference:

```bash
uv run runpod-video setup \
  --apply \
  --include-refiner \
  --stop-pod
```

Use `--refiner-profile PATH` to select a different pinned profile.

## Refine Only

Create or reuse a deterministic refinement:

```bash
uv run runpod-video refine projects/cozy-bedroom --apply
```

The result is written below
`output/prompt-refinement/CACHE_KEY/scene.refined.json`, with a neighboring
`provenance.json`. A later invocation without `--apply` succeeds when the cache
entry is valid and fails safely on a cache miss. `--force --apply` reruns the
model and atomically replaces the entry.

The cache key covers the complete source manifest, model and runtime artifacts,
generation settings, system prompt, and reference document. Cache loading also
checks the refined manifest hash and validates it as a normal scene.

## Refine And Render

Refine prompts and render with one Pod allocation:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --refine-prompts
```

On a cache miss, the command performs these phases in order:

1. Download or verify the pinned KoboldCpp and GGUF artifacts.
2. Stop ComfyUI and run KoboldCpp on the worker loopback interface.
3. Validate and persist the strict prompt overlay.
4. Stop KoboldCpp completely.
5. Start ComfyUI, verify the required generation models, and render on the same
   Pod.

A cache hit skips the refiner process and does not create a Pod during
`--plan`. A cache miss under `--plan` reports that refinement must first be
created with `--apply`.

When an existing Pod is named with `--pod-id`, any command that runs the refiner
also requires `--restart`. This explicit opt-in permits the command to stop the
current ComfyUI workload before loading the language model:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --refine-prompts \
  --pod-id POD_ID \
  --restart \
  --keep-pod
```

## Browser Chat

Open KoboldCpp's browser UI through an SSH tunnel:

```bash
uv run runpod-video chat --apply
```

The remote server and local tunnel both bind to `127.0.0.1`; no public HTTP port
is opened. Press Enter to close the server. For unattended use, combine
`--no-browser` with `--duration-seconds N`. Reusing an existing Pod requires
`--pod-id POD_ID --restart`.

## Validation Boundaries

The refiner receives and may return only these prompt-bearing values:

- scene `global_prompt` and `negative_prompt`
- shot `prompt`, `camera`, `negative_prompt`, and `end_state`
- generated start-image `prompt` and `negative_prompt`

The orchestrator applies that overlay to a deep copy of the source document.
It rejects extra fields, changed shot names or counts, malformed JSON, invalid
scene values, and attempts to add generated start images. Titles, paths,
dimensions, sampling settings, seeds, shot order, and all other values remain
source-controlled.

Refinement provenance is included in start-image inputs, shot inputs, canonical
fingerprints, and `render-manifest.json`. Changing the refiner inputs therefore
invalidates affected `--resume` outputs instead of silently reusing them.

The included system prompt permits explicit lawful content involving fictional,
consenting adults while prohibiting minors, ambiguous ages, coercion,
non-consent, incest, bestiality, and illegal content.
