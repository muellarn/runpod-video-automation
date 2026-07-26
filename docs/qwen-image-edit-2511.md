# Qwen-Image-Edit-2511

The default Wan profile includes an opt-in Qwen-Image-Edit-2511 preset for
reference-guided start and end frames. It uses ComfyUI core nodes and the
official Comfy-Org split model files. The default `setup` command does not
install these files.

## Install

Prewarm only the image editor on the persistent Network Volume without creating
a GPU Pod:

```bash
uv run runpod-video setup \
  --apply \
  --model-group qwen-image-edit-2511
```

A scene that requests the `image_edit` workflow selects the group automatically
for cache preflight, but it never downloads a missing model on the GPU worker.
Run the setup command once before rendering; a missing cache aborts before Pod
allocation.

## Manifest

Use `generate_end_image` to edit references into an end frame for Wan:

```json
{
  "title": "Reference-guided turn",
  "global_prompt": "A fictional adult character in a softly lit studio",
  "shots": [
    {
      "name": "Turn",
      "prompt": "The character turns toward the window",
      "start_image": "assets/start.png",
      "generate_end_image": {
        "prompt": "Preserve Picture 1 composition, identity, clothing, and lighting. Turn the fictional adult character toward the window.",
        "negative_prompt": "identity change, different clothing, malformed anatomy",
        "reference_images": [
          {"source": "current_start"},
          {"path": "assets/identity-reference.png"}
        ],
        "seed": 1201
      }
    }
  ]
}
```

`generate_end_image` defaults to workflow `image_edit`, whose included preset
uses adapter `qwen_image_edit_2511`. `generate_start_image` can also select the
same workflow explicitly when its references come from files or prior shots:

```json
"generate_start_image": {
  "workflow": "image_edit",
  "prompt": "Place the subject from Picture 2 into the composition of Picture 1",
  "reference_images": [
    {"path": "assets/composition.png"},
    {"source": "shot_end", "shot": 1}
  ]
}
```

The editor requires one to three references in exact list order. Picture 1 is
the primary image: it determines the composition and output dimensions. Do not
set `width` or `height` for this adapter. More references increase ambiguity;
prefer one reference unless identity, wardrobe, or another concrete detail
requires an additional image.

Each entry in `reference_images` must have one of these forms:

| Reference | Meaning |
| --- | --- |
| `{"path": "assets/reference.png"}` | File relative to `scene.json` |
| `{"source": "current_start"}` | Resolved start frame of the current shot; end generation only |
| `{"source": "shot_start", "shot": 1}` | Resolved start frame of a prior shot |
| `{"source": "shot_end", "shot": 1}` | Explicit or generated end frame of a prior shot |
| `{"source": "shot_continuation", "shot": 1}` | True final video frame of a prior shot |

Shot numbers are positive and 1-based. Dynamic references must point backward,
which keeps the generated-image dependency graph acyclic. If a selected image
depends on a stale generated image from an unselected prior shot, the dependency
is regenerated first. Missing required continuations fail before a worker is
started.

## Review And Resume

Generate configured start and end images without loading Wan or invoking
FFmpeg:

```bash
uv run runpod-video scene projects/my-scene \
  --apply \
  --generated-images-only \
  --stop-pod
```

Outputs and metadata are written under `output/000-generated-start-image` and
`output/000-generated-end-image`. After inspection, render only if those exact
files still match their prompts, settings, workflow, models, and ordered
reference hashes:

```bash
uv run runpod-video scene projects/my-scene \
  --apply \
  --approve-generated-images \
  --pod-id POD_ID
```

Use `--resume` after an interruption. Matching generated images are reused; a
changed prompt, workflow, model, sampler setting, or reference content makes the
affected image stale. If every selected output is current, resume completes
locally without provisioning or connecting to a Pod.

Scenes whose later shots inherit video continuations cannot use
`--generated-images-only` before those videos exist. Use
`--preview-generated-images` to inspect the complete generated-image sequence
without rendering Wan videos. Preview mode substitutes each missing inherited
continuation with the previous generated end image and writes everything below
`output/000-image-preview/`. These isolated previews do not alter normal image
metadata and cannot be approved as render inputs.

Preview artifacts are grouped by shot for direct start/end comparison:

```text
000-image-preview/
  001-opening/
    start.png
    start.metadata.json
    end.png
    end.metadata.json
```

The older `--start-image-only` and `--approve-start-images` flags remain useful
for scenes that generate only start frames. They do not approve generated end
frames.

## Quality Defaults

The API workflow follows the official Qwen-Image-Edit-2511 multi-reference
pattern:

- 40 steps, CFG 4, Euler sampler, Simple scheduler
- `ModelSamplingAuraFlow` shift 3.1
- `CFGNorm` strength 1.0
- `FluxKontextMultiReferenceLatentMethod` set to `index_timestep_zero`
- Qwen-Image VAE and Qwen 2.5 VL 7B FP8 text encoder

Prompt instructions should identify references as `Picture 1`, `Picture 2`,
and `Picture 3`, state the requested change directly, and list details that
must remain unchanged. The workflow has no custom safety checker; manifests and
reference images remain the operator's responsibility.

## Pinned Models

The profile pins immutable Hugging Face revisions, exact byte sizes, and
SHA-256 hashes:

| File | Size | SHA-256 |
| --- | ---: | --- |
| `qwen_image_edit_2511_fp8mixed.safetensors` | 20,533,762,817 | `c9fdc158e46d3b61ef75f21ae866ca2fe808bf4a53643120d1c1e87c19280a4e` |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 9,384,670,680 | `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4` |
| `qwen_image_vae.safetensors` | 253,806,246 | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` |

The podless prewarm rejects source data whose size or configured hash differs,
and GPU allocation requires the resulting content-addressed cache marker.
