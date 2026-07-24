# RunPod Video Automation

This project provisions an ephemeral RunPod GPU Pod, reuses a persistent model
volume, runs a ComfyUI API workflow, downloads every produced image, video, or
audio file, and terminates the GPU Pod in a `finally` block.

ComfyUI is not exposed to the public internet. The client reaches its local API
through an SSH tunnel. A separate cleanup command terminates stale managed Pods
if the orchestrator machine crashes before normal cleanup.

## Why Pods instead of Serverless

Wan 2.2 A14B and LTX-2.3 require tens of gigabytes of model files and can run
for many minutes. An ephemeral Secure Cloud Pod with an 80 GB GPU is more
predictable than repeatedly cold-starting Serverless workers. Serverless is a
better fit for Wan 5B or sustained parallel queues.

## Setup

```bash
uv sync --extra dev
export RUNPOD_API_KEY="..."
```

The default profile reads `~/.ssh/id_ed25519` and its public key. Override it
with `RUNPOD_VIDEO_SSH_KEY` or `--ssh-key`.

## Safe inspection

These commands do not create billable resources:

```bash
uv run runpod-video inventory
uv run runpod-video plan
```

## Render

Install model groups without queueing a generation:

```bash
uv run runpod-video setup \
  --apply \
  --model-group wan22-i2v \
  --model-group z-image-turbo \
  --stop-pod
```

`setup` creates a new Pod unless `--pod-id` is explicitly supplied, mounts the
profile's Network Volume, verifies or downloads the selected files, creates the
configured model-path aliases, and exits without opening ComfyUI or queueing a
workflow. It is still billable while the setup Pod runs. Existing complete
files are skipped; `.part` downloads resume after interruptions. Without
`--model-group`, setup installs the union required by all profile workflow
presets.

The included `workflows/wan22-i2v-14b-api.json` is an API-format conversion of
ComfyUI's official Wan 2.2 14B I2V example. It produces a WebM output. Upload an
input image as `input.png` and override node 6 for the motion prompt:

```bash
uv run runpod-video run workflows/wan22-i2v-14b-api.json \
  --apply \
  --model-group wan22-i2v \
  --image start.png:input.png \
  --set '6.text=A detailed motion prompt' \
  --output output
```

Overrides use `NODE_ID.INPUT=JSON`. Values that are not valid JSON are treated
as strings. For the included workflow, node 6 is the positive prompt, node 7 is
the negative prompt, node 52 is the input filename, node 57 contains the random
seed, and node 50 controls width, height, frame count, and batch size.

Custom workflows can be exported with `Workflow -> Export (API)`. Select their
dependencies with one or more `--model-group` options.

The default Wan profile enables the ComfyUI Triton backend on H100 workers. In
the measured 576x800, 81-frame, 20-step workload this reduced warm wall time
from about 175 seconds to 161 seconds without an observed quality loss. The
motion trajectory is not bit-identical to the non-Triton backend. See
[`docs/comfyui-single-h100-benchmark.md`](docs/comfyui-single-h100-benchmark.md)
for the complete measurements and rejected optimizations.

During execution, the CLI connects to ComfyUI's WebSocket before queueing the
workflow and prints node transitions plus sampler steps and percentages. If the
WebSocket is unavailable, execution continues with history polling. CLI output
is line-buffered, so status updates also appear immediately in redirected or
`nohup` logs. Download, start-image, node, sampler, output, and completion
progress is therefore shown directly by the main command.

Uploads, ComfyUI execution, and output downloads are retried twice by default.
Set a different number of retries with `--retries N`. Before retrying a failed
ComfyUI job, the command interrupts the current execution and clears pending
queue entries.

## Scene Direction

For timelines longer or more controlled than one prompt, use a scene manifest.
It combines a global character/style description with separate action and
camera direction for every shot. All shots render on the same Pod, so models
are downloaded and initialized once.

See [`docs/scene-manifest-reference.md`](docs/scene-manifest-reference.md) for
the complete field-by-field manifest, prompting, continuity, sampling, image,
resume, and metadata reference.

Scenes are organized as self-contained project bundles:

```text
projects/cozy-bedroom/
├── scene.json
├── assets/
└── output/
```

Pass either the project directory or its `scene.json`. Without `--output`, the
CLI writes to `output/` beside the manifest. Explicit `start_image` and
`end_image` paths are resolved relative to `scene.json`; before rendering they
are copied to content-addressed files under `output/000-inputs/`. The validated
manifest is copied to `output/scene.snapshot.json`. This keeps each render
self-contained while `--output` remains available as an override.

Validate the included example without creating cloud resources:

```bash
uv run runpod-video scene projects/cozy-bedroom --plan
```

Render every shot and assemble the WebM clips locally with FFmpeg:

```bash
uv run runpod-video scene projects/cozy-bedroom --apply
```

A manifest supports these scene-level fields:

- `title`: output filename and human-readable scene name
- `global_prompt`: character, location, continuity, lighting, and style
- `negative_prompt`: constraints shared by all shots
- `width`, `height`, and `fps`: common output format
- `steps`, `transition_step`, and `cfg`: Wan high/low-noise sampling defaults
- `shots`: ordered shot definitions

Each shot supports:

- `name` and `prompt`
- `end_state`, an optional concise description passed to the next shot
- `camera`, appended as an explicit camera direction
- `start_image`, resolved relative to the manifest
- `generate_start_image`, an optional Z-Image or SDXL prompt and sampling configuration
- optional `end_image` for first/last-frame conditioning
- `duration_seconds`, rounded to Wan's required `4n + 1` frame interval
- `seed`, `cfg`, and an additional `negative_prompt`

When a shot defines `end_state`, the next shot's effective prompt receives it
as `Starting state: ...`; the next shot's own prompt is labeled
`Current action: ...`. Previous action prompts are not copied wholesale, which
avoids repeating old motion while preserving the intended pose, location, and
clothing state. Enter `end_state` manually because the intended final state
cannot be inferred reliably from an action prompt.

The first shot requires either `start_image` or `generate_start_image`. Generated
keyframes are created with the included Z-Image workflow on the same Pod, downloaded
to `000-generated-start-image`, and uploaded back into ComfyUI for Wan I2V. The
optional Z-Image model, text encoder, and VAE are only downloaded when a scene
requests generation. The profile selects the default start-image adapter; an
individual request may set `adapter` only when it matches the selected workflow.
Distilled Z-Image uses zeroed negative conditioning and therefore does not
accept `negative_prompt`.
A later shot may omit both fields; the orchestrator then uses the previous
clip's final frame as the next start keyframe. Every rendered shot, including
the final shot, writes that frame to `continuation.png` in its numbered output
directory. Supplying or generating a new start image creates an independently
composed shot. The final WebM is a hard-cut concatenation, while every shot's
individual WebM remains in a numbered output directory.

Render one shot by its 1-based position in the manifest:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --shot 2
```

The original shot number and numbered output directory are preserved. A shot
with its own `start_image` or `generate_start_image` can render independently.
Otherwise, the command requires the immediately preceding shot's
`continuation.png` under the same `--output` directory. This is checked before
any Pod is created; a missing continuation aborts without compute cost. A
single-shot run writes its individual WebM and a new `continuation.png`, but
does not assemble a complete scene WebM.

Select multiple shots with comma-separated numbers and inclusive ranges:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --shots 1,3-5
```

Selected shots run in manifest order. When a selected shot depends on its
immediate predecessor and that predecessor is not selected, its existing
`continuation.png` is required before Pod creation. Selecting every shot still
assembles the complete scene; a partial selection only writes individual shot
outputs.

Resume an interrupted scene in the same output directory:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --resume
```

`--resume` compares the current effective shot inputs with the shot's saved
`metadata.json`. It reuses a shot only when its fingerprint and the SHA-256
hashes of its WebM and `continuation.png` match. If only the continuation file
is missing, it is recovered locally from the true final frame and validated
against the recorded hash. Existing generated start images are reused only
when their own metadata matches. If every selected shot is current, no Pod is
created; a complete scene is assembled locally from the existing clips.

Input differences are printed by field, for example the effective prompt,
seed, CFG, dimensions, workflow hash, model specification, or start-image hash.
A changed shot invalidates later shots that consume its continuation. A later
shot with its own `start_image` or `generate_start_image` remains independent.
Legacy outputs without metadata are treated as stale and rendered again.

To adopt existing local outputs created before metadata tracking, backfill the
missing sidecars without starting a Pod:

```bash
uv run runpod-video scene projects/my-scene --backfill-metadata
```

Backfill requires exactly one matching video and `continuation.png` per
existing shot. It also adopts uniquely matching generated start images, skips
shots without outputs, writes `render-manifest.json`, and marks provenance as
`inferred_from_existing_outputs`. It never overwrites existing shot or
start-image sidecars; after full validation it refreshes the scene snapshot and
rebuildable render manifest. The historical render time and the relationship
of an existing assembled video to the shots remain explicitly unverified. The
command cannot be combined with `--apply` or Pod lifecycle options. Use it only
when the current `scene.json`, workflow profile, and sampling settings
accurately describe the historical render; those facts cannot be recovered
from WebM files alone. A subsequent `--resume --apply` then reuses matching
adopted shots and renders only missing or changed shots.

Generate only the images configured by `generate_start_image`, without loading
the Wan models, requiring FFmpeg, or rendering video shots:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --start-image-only
```

To use an already running Pod, add `--pod-id POD_ID --keep-pod`. The command
generates every explicitly configured `generate_start_image` entry in the
manifest and skips shots that only reference an existing `start_image`.

For an explicit start-image review, first generate images without video and
keep the same output directory:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --start-image-only \
  --stop-pod
```

After inspecting the files in `000-generated-start-image`, approve them and
render without regenerating them:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --approve-start-images \
  --pod-id POD_ID
```

Approval is checked before Pod use. Every selected shot with
`generate_start_image` must have a matching generated image. Each generated
image has a deterministic `NNN-name.metadata.json` sidecar that identifies the
exact approved file; changing its prompt, seed,
sampler, dimensions, model, or workflow causes approval to fail before Pod use.

## Prompt Refinement

Prompt refinement is opt-in and runs a pinned open-weight Qwen GGUF locally on
the RunPod worker through KoboldCpp. It rewrites only prompt-bearing scene and
shot fields; names, order, paths, seeds, sampling, and other manifest values
remain source-controlled.

Refine and render sequentially on the same Pod:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --refine-prompts
```

Refine without rendering, or open the loopback-only browser chat:

```bash
uv run runpod-video refine projects/cozy-bedroom --apply
uv run runpod-video chat --apply
```

Results are cached from the source manifest, pinned model and runtime, prompts,
reference document, and generation settings. A valid cache can be inspected or
rendered without rerunning the language model; `--force --apply` refreshes it.
On a cache miss the integrated command stops ComfyUI, runs the refiner, fully
stops it to release VRAM, then starts ComfyUI on the same worker. Existing Pods
require explicit `--pod-id POD_ID --restart` whenever the refiner must run.

Preload its artifacts with `setup --include-refiner`. See
[`docs/prompt-refiner.md`](docs/prompt-refiner.md) for cache, lifecycle, browser,
security, model, and provenance details.

## Render Metadata

Every successfully rendered shot writes `metadata.json` beside its WebM and
`continuation.png`. It contains:

- global, shot, camera, effective positive, and effective negative prompts
- seed, CFG, steps, transition step, dimensions, FPS, and frame count
- start/end image hashes and generated-start-image configuration
- worker image, adapters, model groups, workflow hashes, model URLs, sizes, and hashes
- output paths, sizes, and SHA-256 hashes
- prompt-refinement cache and artifact provenance when enabled
- Pod ID, GPU, hourly price, completion time, and measured shot duration
- a canonical fingerprint of all quality-relevant inputs

Metadata is written atomically only after the WebM and exact final continuation
frame are available. `render-manifest.json` in the output root is a rebuildable
index of all shot and start-image metadata plus the current scene source hash
and final stitched video hash. It is refreshed after every completed shot, so
an interrupted run can safely continue with `--resume`.

`run` refuses to create billable resources unless `--apply` is present. The
profile also rejects a provisioned Pod whose reported hourly price exceeds its
`max_hourly_cost` value and immediately terminates it.

The first run creates a 250 GB Network Volume and downloads the model files.
That volume continues to incur storage cost after the GPU Pod terminates. Later
runs reuse the model files and only pay for the active Pod plus storage.

By default, the Pod is terminated after success, failure, timeout, or Ctrl-C.
Use `--keep-pod` only for debugging. Use `--stop-pod` to release the GPU but
retain the Pod configuration instead of terminating it:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --stop-pod
```

`--keep-pod` and `--stop-pod` are mutually exclusive. A stopped Pod incurs no
GPU or container-disk cost; the attached network volume continues to be billed
at the same rate as after termination. Reusing a stopped Pod with `--pod-id`
starts it automatically, but GPU capacity is not reserved and may be
unavailable when it is started again.

To keep a Pod available briefly but stop it automatically after ComfyUI is
idle, combine `--keep-pod` with a local watchdog:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --keep-pod \
  --idle-stop-minutes 5
```

The detached watchdog polls ComfyUI's running and pending queues. Active work
resets the idle timer. The watchdog runs on the orchestrator machine, so it
cannot stop the Pod if that machine shuts down; the regular stale-Pod cleanup
remains the fallback.

### Restart on the same Pod

Start a scene with `--keep-pod` when you may need to inspect and regenerate its
start image. This prevents the original orchestrator from deleting the Pod when
it is interrupted:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --keep-pod
```

Get the Pod ID from the command output or `runpod-video inventory`. After
adjusting the manifest, reuse that Pod and cancel the old ComfyUI execution and
pending queue before starting the corrected scene:

```bash
uv run runpod-video scene projects/cozy-bedroom \
  --apply \
  --pod-id POD_ID \
  --restart \
  --keep-pod
```

`--restart` requires `--pod-id`. The existing Pod, mounted model volume, and
running ComfyUI installation are reused; the command opens a new SSH tunnel but
does not create another Pod. Keep `--keep-pod` while iterating. Omit it from the
final restart to terminate the reused Pod automatically when rendering
finishes. The original command must also have used `--keep-pod`, otherwise its
`finally` cleanup can terminate the Pod after its ComfyUI execution is
interrupted.

## Watchdog

Run this from a second machine or a user systemd timer every five minutes:

```bash
uv run runpod-video cleanup --max-age-hours 2
```

It only touches Pods whose names begin with `runpod-video-`. It never deletes
Network Volumes. To terminate all managed Pods immediately:

```bash
uv run runpod-video cleanup --all
```

## Profiles

Profiles separate infrastructure from model and workflow selection. The
included profile defines independent `wan22-i2v` and `z-image-turbo` groups,
plus `video` and `start_image` workflow presets. A preset binds an API workflow
to an adapter, any number of model groups, and optional adapter defaults such as
the checkpoint or sampler. Scene CLI overrides retain the preset's other
values: `--workflow`, `--video-adapter`, `--video-model-group`,
`--start-image-workflow`, `--start-image-adapter`, and
`--start-image-model-group` can be changed independently.

Node-specific workflow mutation lives in `adapters.py`, not in the scene
orchestrator. Supporting another model family therefore means adding a small
adapter, workflow JSON, model group, and workflow preset. The low-level `run`
command remains adapter-independent.

A profile controls:

- Docker image and GPU fallback order
- data center and persistent volume size
- minimum system RAM and vCPU
- arbitrary named groups of exact HTTPS model downloads and destination paths
- model-directory aliases exposed to the worker image
- default workflow paths, adapters, model groups, and adapter settings

Minimal structure:

```json
{
  "model_groups": {
    "my-video-model": [
      {"url": "https://example/model.safetensors", "path": "models/unet/model.safetensors"}
    ]
  },
  "model_path_aliases": [
    {"source": "models/diffusion_models", "target": "models/unet"}
  ],
  "workflows": {
    "video": {
      "path": "../workflows/video-api.json",
      "adapter": "my_video_adapter",
      "model_groups": ["my-video-model"]
    }
  }
}
```

Model aliases preserve paths below their source directory. For example,
`models/diffusion_models` to `models/unet` exposes every selected diffusion
model under the worker's expected loader directory without duplicating the
file. Conflicting model destinations or alias targets are rejected before
generation.

Network Volumes are tied to one data center. Change both `data_center_id` and
`volume_name` together when using another region.

## Tests

```bash
uv run pytest
```
