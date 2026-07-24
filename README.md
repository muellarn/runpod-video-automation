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

The included `workflows/wan22-i2v-14b-api.json` is an API-format conversion of
ComfyUI's official Wan 2.2 14B I2V example. It produces a WebM output. Upload an
input image as `input.png` and override node 6 for the motion prompt:

```bash
uv run runpod-video run workflows/wan22-i2v-14b-api.json \
  --apply \
  --image start.png:input.png \
  --set '6.text=A detailed motion prompt' \
  --output output
```

Overrides use `NODE_ID.INPUT=JSON`. Values that are not valid JSON are treated
as strings. For the included workflow, node 6 is the positive prompt, node 7 is
the negative prompt, node 52 is the input filename, node 57 contains the random
seed, and node 50 controls width, height, frame count, and batch size.

Custom workflows can be exported with `Workflow -> Export (API)` and must
reference the model filenames from `profiles/wan22-i2v-fp8.json`.

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
progress is therefore shown directly by the main command; `watch-progress.py`
is only an optional compact view for an already redirected log and is not
started automatically.

Uploads, ComfyUI execution, and output downloads are retried twice by default.
Set a different number of retries with `--retries N`. Before retrying a failed
ComfyUI job, the command interrupts the current execution and clears pending
queue entries.

## Scene Direction

For timelines longer or more controlled than one prompt, use a scene manifest.
It combines a global character/style description with separate action and
camera direction for every shot. All shots render on the same Pod, so models
are downloaded and initialized once.

Validate the included example without creating cloud resources:

```bash
uv run runpod-video scene examples/scene.example.json --plan
```

Render every shot and assemble the WebM clips locally with FFmpeg:

```bash
uv run runpod-video scene examples/scene.example.json \
  --apply \
  --output output/window-scene
```

The tiny PPM keyframes included with the example only make it self-validating.
Replace them with suitable PNG, JPEG, or WebP keyframes before a real render.

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
requests generation. Set `model_type` to `z_image_turbo`; distilled Z-Image uses
zeroed negative conditioning and therefore does not accept `negative_prompt`.
A later shot may omit both fields; the orchestrator then uses the previous
clip's final frame as the next start keyframe. Every rendered shot, including
the final shot, writes that frame to `continuation.png` in its numbered output
directory. Supplying or generating a new start image creates an independently
composed shot. The final WebM is a hard-cut concatenation, while every shot's
individual WebM remains in a numbered output directory.

Render one shot by its 1-based position in the manifest:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --shot 2 \
  --output output/bedroom-scene
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
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --shots 1,3-5 \
  --output output/bedroom-scene
```

Selected shots run in manifest order. When a selected shot depends on its
immediate predecessor and that predecessor is not selected, its existing
`continuation.png` is required before Pod creation. Selecting every shot still
assembles the complete scene; a partial selection only writes individual shot
outputs.

Resume an interrupted scene in the same output directory:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --resume \
  --output output/bedroom-scene
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

Generate only the images configured by `generate_start_image`, without loading
the Wan models, requiring FFmpeg, or rendering video shots:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --start-image-only \
  --output output/start-image-review
```

To use an already running Pod, add `--pod-id POD_ID --keep-pod`. The command
generates every explicitly configured `generate_start_image` entry in the
manifest and skips shots that only reference an existing `start_image`.

For an explicit start-image review, first generate images without video and
keep the same output directory:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --start-image-only \
  --stop-pod \
  --output output/bedroom-review
```

After inspecting the files in `000-generated-start-image`, approve them and
render without regenerating them:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --approve-start-images \
  --pod-id POD_ID \
  --output output/bedroom-review
```

Approval is checked before Pod use. Every selected shot with
`generate_start_image` must have a matching generated image. Each generated
image has a deterministic `NNN-name.metadata.json` sidecar that identifies the
exact approved file; changing its prompt, seed,
sampler, dimensions, model, or workflow causes approval to fail before Pod use.

## Render Metadata

Every successfully rendered shot writes `metadata.json` beside its WebM and
`continuation.png`. It contains:

- global, shot, camera, effective positive, and effective negative prompts
- seed, CFG, steps, transition step, dimensions, FPS, and frame count
- start/end image hashes and generated-start-image configuration
- worker image, workflow hashes, model URLs, model sizes, and known model hashes
- output paths, sizes, and SHA-256 hashes
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
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --stop-pod \
  --output output/bedroom-scene
```

`--keep-pod` and `--stop-pod` are mutually exclusive. A stopped Pod incurs no
GPU or container-disk cost; the attached network volume continues to be billed
at the same rate as after termination. Reusing a stopped Pod with `--pod-id`
starts it automatically, but GPU capacity is not reserved and may be
unavailable when it is started again.

To keep a Pod available briefly but stop it automatically after ComfyUI is
idle, combine `--keep-pod` with a local watchdog:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --keep-pod \
  --idle-stop-minutes 5 \
  --output output/bedroom-scene
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
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --keep-pod \
  --output output/bedroom-review
```

Get the Pod ID from the command output or `runpod-video inventory`. After
adjusting the manifest, reuse that Pod and cancel the old ComfyUI execution and
pending queue before starting the corrected scene:

```bash
uv run runpod-video scene scenes/adult-bedroom-15s.example.json \
  --apply \
  --pod-id POD_ID \
  --restart \
  --keep-pod \
  --output output/bedroom-restart
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

The included profile uses the four official Comfy-Org Wan 2.2 I2V FP8 files
needed by the starter workflow and prefers 80 GB GPUs. It stores diffusion
models under `models/unet` and text encoders under `models/clip`, matching the
Network Volume paths exposed by the pinned worker image. Add LoRAs as extra
model entries in a copied profile and reference them from the API workflow. A
profile controls:

- Docker image and GPU fallback order
- data center and persistent volume size
- minimum system RAM and vCPU
- exact HTTPS model downloads and destination paths

Network Volumes are tied to one data center. Change both `data_center_id` and
`volume_name` together when using another region.

## Tests

```bash
uv run pytest
```
