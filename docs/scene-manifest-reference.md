# Scene Manifest Reference

This document is the detailed reference for authoring a scene manifest for the
current RunPod Video Automation implementation. It describes the JSON contract,
prompt assembly, image handling, continuity rules, sampling behavior, output
layout, and metadata semantics.

This is a reference document, not a sample manifest. It intentionally contains
no example scene and no copy-paste JSON template.

## Scope

The current scene system supports:

- Ordered multi-shot Wan 2.2 image-to-video rendering.
- A supplied start image or generated start image for the first shot.
- Automatic continuation from the decoded final frame of the previous shot.
- Optional supplied end images through Wan first/last-frame conditioning.
- Generated start images through the configured Z-Image Turbo workflow.
- Generated start or end images through Qwen-Image-Edit-2511 with one to three
  ordered references.
- Code-level SDXL start-image support when a compatible custom profile is used.
- Per-shot image workflow selection, dynamic prior-shot references, generated
  image approval, and dependency-aware resume.
- Per-shot metadata, deterministic resume checks, and
  local metadata backfill.

The current scene system does not yet support:

- Arbitrary ComfyUI node overrides inside the scene manifest.

Those capabilities may be added later. Fields that are not documented here
must not be assumed to have an effect.

## File Location and Project Layout

The CLI accepts either a path to a JSON file or a project directory containing
`scene.json`. When a project directory is supplied, the manifest filename must
be exactly `scene.json`.

The normal project layout consists of:

- `scene.json` for the scene manifest.
- `assets/` for local start and end images.
- `output/` for generated files, metadata, and assembled video.

The entire `projects/` directory is ignored by Git in the current repository.
Project manifests, local assets, and generated outputs are therefore local by
default.

Without an explicit CLI output override, the output root is the `output/`
directory beside `scene.json`.

## General JSON Rules

The manifest root must be a JSON object. There is currently no manifest schema
version field and no standalone JSON Schema file.

Important parser behavior:

- Required strings are trimmed and must remain non-empty.
- Optional strings are trimmed.
- Prompt-style optional strings parsed as text fields convert JSON `null` to an
  empty string.
- Nullable generated-image override fields treat JSON `null` as omission and
  continue through profile and adapter default resolution.
- Numeric values are converted with Python `int` or `float` conversion.
- Unknown fields are currently ignored rather than rejected.
- A misspelled field can therefore silently fall back to its default.
- All referenced image paths are validated while the complete manifest loads,
  including images belonging to shots that are not selected for rendering.
- The legacy generated-image field `model_type` is explicitly rejected. The
  replacement field is `adapter`.

Use exact documented field names and native JSON numbers. Do not rely on
numeric strings or boolean-to-number conversion even where Python conversion
would currently accept them.

## Top-Level Fields

### `title`

| Property | Value |
| --- | --- |
| Required | Yes |
| Type | String |
| Default | None |
| Validation | Must remain non-empty after trimming |

The title is the human-readable scene name. The final assembled filename is
derived from a slug of this value, not from the literal title. Changing only
the title does not invalidate individual shot fingerprints, but it changes the
expected assembled filename and the title recorded in `render-manifest.json`.

### `global_prompt`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

The global prompt contains visual facts that should remain stable across the
scene. It is the first component of every effective video prompt.

Recommended content:

- Stable subject identity and clearly adult age where relevant.
- Persistent physical characteristics.
- Persistent wardrobe and accessories.
- Persistent location and room description.
- Lighting and time-of-day continuity.
- Rendering style and realism level.
- General continuity requirements.

Avoid putting shot-specific actions in this field. A global action is repeated
for every shot and can cause old movement to continue or restart.

If `global_prompt` is empty, every shot must provide a non-empty `prompt`.

### `negative_prompt`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

This is the shared negative prompt for video generation. It is combined with
the current shot's `negative_prompt`.

Recommended content:

- Persistent anatomy failures to avoid.
- Identity drift and unwanted subject duplication.
- Flicker, jitter, abrupt cuts, and unwanted camera movement.
- Persistent quality failures.
- Content that must remain excluded from every shot.

Keep the list focused. Long, contradictory negative prompts can reduce prompt
clarity. This field is not inherited by generated start images.

### `width`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | `768` |
| Validation | Positive multiple of 16 |

This is the requested video width passed to Wan conditioning. Every shot uses
the same video dimensions.

The measured vertical production baseline in this repository is `576` pixels
wide. It reduces render cost relative to the parser default and was used for
the current H100 benchmark.

### `height`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | `768` |
| Validation | Positive multiple of 16 |

This is the requested video height passed to Wan conditioning.

The measured vertical production baseline in this repository is `800` pixels
high. Together with width `576`, it preserves the established portrait aspect
ratio and benchmark workload.

### `fps`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Number |
| Default | `16` |
| Validation | Greater than zero |

FPS affects both frame-count calculation and the WebM output frame rate. The
current recommended baseline is `16`. Changing FPS changes the number of frames
derived from every shot duration and invalidates existing shot metadata.

### `steps`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | `20` |
| Validation | At least 2 |

This is the total Wan sampling step count. It is applied to both high-noise and
low-noise samplers.

The current recommended baseline is `20`. More steps increase cost and do not
automatically improve temporal consistency. Fewer steps should be validated
against the exact model, backend, resolution, and motion being used.

### `transition_step`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | Integer half of `steps` |
| Validation | Greater than 0 and less than `steps` |

Wan 2.2 uses separate high-noise and low-noise experts. This field defines the
split:

- The high-noise sampler starts at step 0 and ends at `transition_step`.
- The low-noise sampler starts at `transition_step` and ends at its workflow
  terminal bound.

For the recommended 20-step baseline, the recommended split is `10`. Keep the
split near the midpoint unless a controlled comparison shows a benefit.

### `cfg`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Number |
| Default | `3.5` |
| Validation | Greater than zero |

This is the scene fallback CFG. A shot-level `cfg` overrides it.

The current recommended Wan baseline is `3.5`. Higher CFG can increase literal
prompt pressure but may reduce natural motion or introduce artifacts. Keep CFG
stable across related shots unless one shot has a demonstrated adherence
problem.

### `shots`

| Property | Value |
| --- | --- |
| Required | Yes |
| Type | Non-empty array of objects |
| Default | None |

Shots are processed in array order. Their one-based position determines:

- Default seeds.
- Numbered output directories.
- Continuation dependencies.
- Previous `end_state` propagation.
- Shot selection through CLI flags.

Reordering shots changes their indexes, default seeds, output paths, metadata,
and continuity relationships.

## Shot Fields

### `name`

| Property | Value |
| --- | --- |
| Required | Yes |
| Type | String |
| Default | None |
| Validation | Non-empty and unique after trimming |

The name identifies the shot in plans and metadata. It is slugified for output
paths.

Name uniqueness is case-sensitive, while filesystem slugs are lowercase and
replace non-alphanumeric runs with hyphens. Two distinct names can therefore
produce the same slug. The loader does not currently detect slug collisions.
Choose names that are also unique after lowercase slug conversion.

Renaming a shot changes its output directory, generated-image prefix, metadata
path, and fingerprint.

### `prompt`

| Property | Value |
| --- | --- |
| Required | Conditional |
| Type | String or `null` |
| Default | Empty string |

This describes only the current action or current visual development. It is
added to the effective video prompt with the `Current action:` label.

Recommended writing style:

- Describe one primary action or one coherent transition.
- State the initial physical state when ambiguity is likely.
- Describe motion order explicitly when multiple movements are unavoidable.
- State which objects or garments remain fixed.
- Prefer concrete body and object movement over abstract mood language.
- Avoid repeating the entire global identity and room description.
- Avoid describing a completed end pose as though it were present for the
  entire shot.

The field may be empty only when `global_prompt` is non-empty.

### `camera`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

Camera direction is appended with the `Camera:` label.

Recommended content:

- Framing and shot size.
- Camera height and angle.
- Subject orientation.
- Lens or perspective intent when important.
- Explicit movement or explicit lack of movement.
- Occlusion constraints when important.

Use a single coherent camera instruction. Contradictory framing, zoom, dolly,
and locked-camera language reduces predictability.

### `negative_prompt`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

This adds shot-specific exclusions after the scene negative prompt. Use it for
failures associated with the current action, clothing transition, pose, camera
move, or object interaction.

It does not replace the scene negative prompt. It is not passed to generated
start images.

### `end_state`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

This is a concise factual description of the intended final state after the
current shot. It is not used in the current shot's workflow. Instead, the next
shot receives it with the `Starting state:` label.

Recommended content:

- Final pose and orientation.
- Final location within the scene.
- Clothing and object state.
- Which hand holds which object.
- Camera-relative facing direction when continuity depends on it.

Write a static state, not another action. The model does not infer this field
from the current prompt or decoded final frame.

Important behavior:

- Only the immediately previous shot's `end_state` is propagated.
- The value is propagated even if the next shot has its own start image.
- A new start image breaks pixel dependency but not textual state dependency.
- The current shot's own `end_state` is included in its metadata fingerprint,
  so changing it invalidates that shot and can invalidate dependent successors.

### `start_image`

| Property | Value |
| --- | --- |
| Required | Conditional |
| Type | Path string or `null` |
| Default | `null` |

This supplies a local image as the shot's first-frame conditioning.

Path behavior:

- `~` is expanded.
- Absolute paths are accepted.
- Relative paths are resolved against the directory containing `scene.json`.
- The resolved path must be an existing regular file.
- Image extension and decodability are not checked during manifest parsing.

Before rendering, the image is copied into `output/000-inputs/` under a
content-addressed filename. The copied file, not the original path, is uploaded
and fingerprinted.

`start_image` and `generate_start_image` are mutually exclusive.

### `generate_start_image`

| Property | Value |
| --- | --- |
| Required | Conditional |
| Type | Object or `null` |
| Default | `null` |

This requests a generated start image. Its complete field reference appears in
the Generated Image Fields section.

The generated image is created on the same Pod before video rendering,
downloaded locally, and uploaded back to ComfyUI as the video start image.

### `end_image`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Path string or `null` |
| Default | `null` |

This supplies a desired final-frame image. Path handling and input snapshotting
match `start_image`.

When present, the Wan adapter switches from `WanImageToVideo` to
`WanFirstLastFrameToVideo` and conditions on both images. The final decoded
frame is still extracted from the rendered video as `continuation.png`; the
supplied end image is not copied directly into the continuation path.

Use an end image that is compatible with the start image and global prompt:

- Same identity and number of subjects.
- Same room or a physically plausible transition.
- Matching aspect ratio.
- Compatible lighting and camera perspective.
- A reachable pose for the selected duration.

Large incompatibilities can cause morphing, anatomy errors, abrupt transitions,
or identity drift.

`end_image` and `generate_end_image` are mutually exclusive.

### `generate_end_image`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Object or `null` |
| Default | `null` |

This requests a generated end image and defaults to workflow `image_edit`. The
bundled preset uses Qwen-Image-Edit-2511 with one to three ordered
`reference_images`. The generated result is supplied to Wan as the desired end
frame; the rendered video's decoded final frame remains the continuation.

### `duration_seconds`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Number |
| Default | `5.0` |
| Validation | Greater than zero through frame conversion |

The duration is converted to Wan's required `4n + 1` frame count. The requested
number is retained in metadata, while effective duration is derived from the
rounded frame count and FPS.

The current recommended baseline is five requested seconds at 16 FPS, which
resolves to 81 frames and approximately 5.0625 seconds of encoded video.

Prefer one clear transition per baseline shot. Longer shots increase cost and
give the model more opportunity to drift. Complex sequences are usually more
controllable as separate shots connected by continuation frames and precise
`end_state` values.

### `seed`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | `41 + shot index` |
| Validation | Inclusive range 0 through `2^64 - 1` |

This is the video high-noise sampler seed. The low-noise stage continues the
same sampling process and does not expose a separate manifest seed.

Set explicit seeds for production manifests. Default seeds change when shots
are reordered. Keep a successful seed fixed while adjusting prompt wording so
comparisons remain meaningful. Change the seed when exploring a materially
different motion trajectory or composition.

### `cfg`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Number |
| Default | Scene-level `cfg` |
| Validation | Greater than zero |

This overrides CFG for both Wan sampling stages of the current shot. Use the
scene baseline unless a controlled rerender demonstrates that the shot needs a
different value.

## Generated Image Fields

`generate_start_image` and `generate_end_image` are standalone image-generation
requests. They do not inherit scene or shot prompt text.

### `workflow`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String |
| Default | `start_image` for start generation; `image_edit` for end generation |

This selects a coherent profile workflow preset, including its adapter, model
groups, workflow JSON, and defaults. The bundled `image_edit` preset uses Qwen.

### `adapter`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Parsed default | `null` |

If supplied, this must exactly match the selected workflow adapter.

When omitted, it resolves to the active start-image workflow adapter. That
selection normally comes from the profile preset, but a CLI adapter override
becomes the active adapter instead.

Normally omit this field and let the profile define the coherent combination
of workflow, adapter, model groups, and defaults. Overriding only an adapter can
leave an incompatible workflow or model selection.

Current adapter names:

- `z_image_turbo`
- `sdxl`
- `qwen_image_edit_2511`

The bundled profile is fully configured for `z_image_turbo` and the opt-in
`qwen_image_edit_2511` workflow.

Selected generated images and their dynamic generated-image dependencies are
validated against their chosen workflow presets before worker use.

### `prompt`

| Property | Value |
| --- | --- |
| Required | Yes |
| Type | String |
| Default | None |
| Validation | Non-empty after trimming |

This prompt must fully describe the desired still image. It receives no text
from `global_prompt`, shot `prompt`, `camera`, `end_state`, or any negative
prompt outside this object.

Recommended content order:

1. Subject count, identity, and clearly adult age where relevant.
2. Pose, orientation, expression, and visible body extent.
3. Clothing, accessories, and object relationships.
4. Camera distance, framing, angle, and perspective.
5. Room, background, lighting, and time of day.
6. Rendering style, texture, and realism requirements.
7. Explicit crop and visibility constraints.

The generated image should represent the actual initial state of the video
shot, not the movement that the video prompt will perform afterward.

### `negative_prompt`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default | Empty string |

Z-Image Turbo does not support a non-empty negative prompt in the current
adapter. Its workflow uses zeroed negative conditioning. Supplying a non-empty
value with `z_image_turbo` causes validation to fail.

SDXL supports this field when a compatible custom profile is configured.
Qwen Image Edit also supports this field.

### `checkpoint`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default resolution | Manifest, then profile defaults, then adapter default |

This is the model filename visible to the ComfyUI loader, not a local host path
and not a download URL.

The bundled Z-Image profile resolves to
`cyberrealisticZImage_v50.safetensors`. The file must be included in a selected
profile model group and exposed under the model directory expected by the
workflow.

### `width`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | Scene video width |
| Validation | Positive multiple of 8 |

This controls generated image width. It may differ from video width.

Qwen Image Edit derives output dimensions from Picture 1 and rejects explicit
`width` or `height` values.

The safest choice is the exact video aspect ratio. A higher-resolution source
can improve still-image detail, but it should preserve the video aspect ratio
to avoid unexpected crop or rescale behavior. The established vertical source
resolution is 864 by 1200 for a 576 by 800 video, which preserves the same
aspect ratio.

### `height`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | Scene video height |
| Validation | Positive multiple of 8 |

Height follows the same aspect-ratio guidance as generated image width.

### `seed`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer |
| Default | `1000 + shot index` |
| Validation | Inclusive range 0 through `2^64 - 1` |

This seed affects only start-image generation. It is independent of the video
seed. Set it explicitly for reproducibility and approval workflows.

### `steps`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Integer or `null` |
| Default resolution | Manifest, then profile defaults, then adapter default |
| Validation | Must resolve to a positive value |

Recommended adapter values:

- Z-Image Turbo: `8`
- SDXL: `30`

Z-Image Turbo is distilled for its configured step count. Increasing steps is
not equivalent to improving a conventional non-distilled model.

### `cfg`

| Property | Value |
| --- | --- |
| Required | No |
| Type | Number or `null` |
| Default resolution | Manifest, then profile defaults, then adapter default |
| Validation | Must resolve to a positive value |

Recommended adapter values:

- Z-Image Turbo: `1.0`
- SDXL: `7.0`

Keep Z-Image Turbo at its distilled baseline unless the selected checkpoint
explicitly documents another requirement.

### `sampler_name`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default resolution | Manifest, then profile defaults, then adapter default |
| Validation | Must resolve to a non-empty string |

Recommended adapter values:

- Z-Image Turbo: `res_multistep`
- SDXL: `dpmpp_2m`

The value must be accepted by the ComfyUI sampler node in the selected worker
image.

### `scheduler`

| Property | Value |
| --- | --- |
| Required | No |
| Type | String or `null` |
| Default resolution | Manifest, then profile defaults, then adapter default |
| Validation | Must resolve to a non-empty string |

Recommended adapter values:

- Z-Image Turbo: `simple`
- SDXL: `karras`

### Resolution Precedence

The following generated-image fields use layered resolution:

- `checkpoint`
- `steps`
- `cfg`
- `sampler_name`
- `scheduler`

Precedence is:

1. Explicit field in `generate_start_image` or `generate_end_image`.
2. `defaults` in the selected profile workflow preset.
3. Built-in adapter default.

If the CLI overrides the adapter with a name different from the preset adapter,
the preset defaults are discarded. Workflow path and model groups are not
automatically replaced, so adapter-only overrides can produce an invalid
combination.

## Effective Video Prompt Assembly

The Wan adapter constructs the positive prompt in this exact order:

1. `global_prompt` without a label.
2. The previous shot's `end_state` with the label `Starting state:`.
3. The current shot's `prompt` with the label `Current action:`.
4. The current shot's `camera` with the label `Camera:`.

Empty components are omitted. Remaining components are joined with a comma and
single space.

The effective negative prompt is assembled in this exact order:

1. Scene `negative_prompt`.
2. Current shot `negative_prompt`.

Empty components are omitted and the remaining components are joined with a
comma and single space.

Generated-image prompts use only the text inside their generation object. There
is no automatic inheritance.

### `reference_images`

Qwen Image Edit requires one to three references in exact list order. Picture 1
controls composition and output size. Entries can name a manifest-relative
`path`, the end generation's `current_start`, or a prior `shot_start`,
`shot_end`, or `shot_continuation` with a positive 1-based `shot` number.
References must point backward, so generated-image dependencies remain acyclic.
See [`qwen-image-edit-2511.md`](qwen-image-edit-2511.md) for the complete forms
and quality guidance.

## Prompting Strategy for Temporal Continuity

Use each prompt layer for one responsibility:

| Layer | Responsibility |
| --- | --- |
| `global_prompt` | Stable identity, wardrobe, environment, light, and style |
| Previous `end_state` | Static starting state inherited from the prior shot |
| Shot `prompt` | Current action and transition only |
| Shot `camera` | Framing, angle, perspective, and camera motion |
| Scene negative prompt | Persistent exclusions |
| Shot negative prompt | Action-specific or shot-specific exclusions |
| Generated start prompt | Complete standalone description of the initial still |
| Generated end prompt | Direct edit instruction tied to ordered reference pictures |

For best continuity:

- Keep invariant visual facts worded consistently.
- Do not rename the same object or garment between shots without reason.
- Do not repeat completed actions in later shot prompts.
- Make `end_state` agree with the intended final visual state.
- Make the next shot action begin from that state.
- Keep camera changes physically plausible across a continuation frame.
- Use explicit start images to reset composition when continuation drift has
  become unacceptable.
- Remember that an explicit start image does not suppress previous
  `end_state`; remove or revise the previous state if it should not carry over.

## Start-Image Modes

Every shot uses exactly one of three start modes.

### Supplied Start Image

The shot declares `start_image`. This is the most deterministic way to control
composition and identity for a new sequence segment.

### Generated Start Image

The shot declares `generate_start_image`. The image is generated before video
rendering. The current bundled implementation uses Z-Image Turbo and always
forces image-generation denoise to `1.0` and batch size to `1`.

### Previous Continuation

A shot after the first may omit both start fields. It then consumes the
immediately previous shot's `continuation.png`.

The first shot cannot use continuation mode and must declare one of the first
two modes.

## Generated-Image Review and Approval

`--start-image-only` renders configured start images. The generic
`--generated-images-only` mode renders configured start and end images without
loading Wan models, requiring FFmpeg, or rendering video shots.

Approval behavior:

- A generated image receives a deterministic metadata sidecar.
- The sidecar identifies the exact output path and content hash.
- Approval validates prompt, resolved sampling settings, checkpoint, workflow,
  model declarations, aliases, output path, and output hash.
- An approved image is uploaded as the video start frame without regeneration.
- Approval does not imply video resume.
- Start-image-only mode and approval mode are mutually exclusive.
- `--approve-generated-images` validates both start and end roles, including
  ordered reference hashes.

Changing any fingerprinted generated-image input invalidates approval.

## Frame Calculation

Shot frame count is derived and cannot be set directly in the manifest.

The implementation calculates:

`target = duration_seconds * fps`

`frames = max(5, int(((target - 1) / 4) + 0.5) * 4 + 1)`

The exact implementation produces frame counts of the form `4n + 1`, with a
minimum of five frames. It rounds to the nearest compatible positive frame
count and resolves exact midpoints upward.

Consequences:

- Requested duration and encoded duration can differ slightly.
- Metadata stores both requested seconds and derived frames.
- Scene duration displayed by the plan is the sum of derived frames divided by
  FPS.
- Changing FPS changes every derived frame count unless duration happens to
  resolve to the same compatible count.

## Current Recommended Production Baseline

The current measured Wan 2.2 vertical baseline is:

| Setting | Recommended value |
| --- | ---: |
| Video width | 576 |
| Video height | 800 |
| FPS | 16 |
| Requested shot duration | 5 seconds |
| Derived frames | 81 |
| Total steps | 20 |
| Transition step | 10 |
| CFG | 3.5 |
| Wan sampler | Euler, inherited from workflow |
| Wan scheduler | Simple, inherited from workflow |
| Wan model shift | 8.0, inherited from workflow |
| Output | VP9 WebM, inherited from workflow |
| Workflow CRF | 13.333, inherited from workflow |

The default profile enables the ComfyUI Triton backend. In the recorded H100
benchmark, this reduced warm render time without an observed quality loss, but
the motion trajectory was not bit-identical to the non-Triton backend.

The current Z-Image Turbo baseline is:

| Setting | Recommended value |
| --- | ---: |
| Steps | 8 |
| CFG | 1.0 |
| Sampler | `res_multistep` |
| Scheduler | `simple` |
| Batch size | 1 |
| Denoise | 1.0 |
| Negative prompt | Unsupported |

Treat these as coherent baselines. Change one parameter at a time and preserve
metadata when comparing results.

## Workflow-Controlled Values Not Exposed in Scene JSON

The scene manifest does not expose every ComfyUI workflow input.

For the bundled Wan workflow, these values remain controlled by the workflow
JSON or adapter implementation:

- High-noise and low-noise model filenames.
- Text encoder and VAE filenames.
- Sampler algorithm.
- Scheduler algorithm.
- Model shift.
- Video codec.
- CRF and output quality settings.
- Batch size, which is forced to one.
- Workflow node topology.

For the bundled start-image workflow, these remain workflow-controlled:

- Text encoder filename and loader type.
- VAE filename.
- Model sampling shift.
- Negative-conditioning topology.
- Output node topology.

Use a different coherent workflow and profile when those values must change.
The scene command does not support the low-level `NODE.INPUT=JSON` override
mechanism available to the raw workflow command.

## Path Resolution and Input Snapshotting

Manifest path rules:

- The manifest argument is resolved from the current working directory.
- A directory argument resolves to its `scene.json`.
- Image paths are resolved relative to the manifest directory unless absolute.
- Profile workflow paths are resolved relative to the profile file.
- CLI workflow overrides are resolved relative to the repository root.
- A relative CLI output override is relative to the current working directory.

Explicit start and end images are copied to content-addressed files before
rendering. The filename includes:

- One-based shot index.
- Input role.
- First twelve hexadecimal characters of the SHA-256 digest.
- Lowercase source extension, or a generic fallback extension.

Content-addressing preserves the exact assets used by a completed output
bundle. A later invocation still reloads `scene.json` and requires every
originally declared image path to exist; resume and backfill do not
automatically substitute the stored snapshot for a missing source asset.

## Output Structure

The output root contains:

| Path | Purpose |
| --- | --- |
| `scene.snapshot.json` | Atomic copy of the scene manifest used by the run |
| `000-inputs/` | Content-addressed supplied start and end images |
| `000-generated-start-image/` | Generated keyframes and their sidecars |
| Numbered shot directories | Video, continuation frame, and shot metadata |
| `render-manifest.json` | Rebuildable index of scene and shot metadata |
| Slugified title plus video suffix | Final assembled video for a full-scene run |

Every successfully rendered shot writes:

- Exactly one selected video output.
- `continuation.png` extracted from the true decoded end of that video.
- `metadata.json` written after both files exist.

The final video is a hard-cut FFmpeg concat using stream copy. It is created
only when all shots are selected and available.

Downloaded basenames come from ComfyUI. Remote output subdirectories are
flattened during local download. If a destination basename already exists, the
downloader adds a random eight-character suffix instead of overwriting it.
After a successful shot rerender, older videos with the active adapter suffix
are pruned from that shot directory. Older generated start images are not
automatically pruned, so multiple matching images can make later backfill
ambiguous.

## Continuation Semantics

After each shot, FFmpeg seeks from the end, decodes through the final frame, and
writes `continuation.png`. This is the source image for the next dependent
shot.

Important consequences:

- The continuation is the actual decoded video result, not the requested end
  image.
- A mismatch between intended `end_state` and actual continuation pixels can
  still propagate visually.
- Textual continuity and pixel continuity are separate mechanisms.
- A supplied/generated start image breaks pixel dependency.
- A supplied/generated start image does not break previous `end_state`
  propagation.

## Shot Selection

Shot indexes are one-based.

Selection can target one shot or a sorted, deduplicated set of positive shot
numbers and inclusive ranges.

When a selected shot uses continuation mode and its predecessor is not
selected, the previous continuation file must already exist in the same output
root. This is checked before Pod creation.

A partial selection:

- Keeps original shot indexes and output directory names.
- Does not assemble a final scene video.
- Can render independently only when each selected shot has its own start or
  the required previous continuation already exists.

## Metadata Fingerprints

Shot metadata schema version is currently `2`.

Fingerprint inputs include:

- Shot index and name.
- Global, action, camera, starting-state, and end-state strings.
- Effective positive and negative prompts.
- Seed, CFG, steps, and transition step.
- Video dimensions, FPS, derived frames, and requested duration.
- Start source category.
- Start and end image names, sizes, and SHA-256 hashes.
- Resolved generated start- and end-image settings and their generation
  fingerprints when applicable.
- Ordered generated-image reference names, sizes, and SHA-256 hashes.
- Prompt-refinement cache and artifact provenance when enabled.
- Container image and ComfyUI arguments.
- Workflow adapter, canonical base-workflow hash, and output suffix.
- Workflow model groups and complete model declarations.
- Workflow defaults and model path aliases.

The following runtime facts do not affect the fingerprint:

- Pod ID.
- GPU model actually allocated.
- Hourly cost.
- Completion timestamp.
- Elapsed render time.

The following manifest or implementation details are not directly
fingerprinted:

- Scene title.
- Original input filesystem paths after snapshotting.
- Profile name, data center, volume name, and GPU preference order.
- Adapter source-code version.
- Fully mutated workflow JSON after adapter changes.

The workflow fingerprint is a canonical hash of the parsed base workflow.
Whitespace and JSON key order do not affect it.

## Resume Behavior

Resume reuses a shot only when:

- Metadata exists and parses as an object.
- Metadata schema version matches.
- Saved input structure matches current effective inputs.
- Canonical fingerprint matches.
- Saved video and continuation files exist.
- Their SHA-256 hashes match metadata.

If the video is valid and only `continuation.png` is missing, the continuation
is re-extracted locally and checked against its recorded hash.

Dependency invalidation:

- A selected dependent shot is invalidated when its selected predecessor must
  rerender.
- A shot with an explicit or generated start image does not depend on the
  predecessor's continuation.
- Generated start and end images have separate metadata and dependency-aware
  reuse validation.

If every selected shot is valid, no Pod is started. A complete scene can be
assembled locally from reused clips.

## Metadata Backfill

Backfill creates missing metadata from existing local outputs without starting
a Pod.

Backfill requires:

- A valid current scene manifest.
- Current workflow and profile selections that accurately describe the old
  render.
- Exactly one matching video per adopted shot.
- An existing continuation image for every adopted shot.
- A unique generated start image or a valid existing generated-image sidecar.
- Existing previous continuations for dependent shots.

For a shot that does not already have `metadata.json`, having neither video nor
continuation causes the shot to be skipped. Having only one of those two files
causes backfill to fail. A shot with an existing sidecar is skipped before
backfill inspects or validates its video and continuation; normal resume is
responsible for validating that existing sidecar and its outputs.

Backfilled metadata is marked with:

- `backfilled: true`
- `provenance: inferred_from_existing_outputs`
- `historical_render_time_unknown: true`

Backfill cannot prove that the historical prompt, workflow, model, seed, or
sampling values match the current manifest. It is an explicit trust operation.

Backfill does not overwrite existing shot or generated-image sidecars. It
validates existing generated-image sidecars, refreshes the scene snapshot, and
rebuilds the render manifest after the adoption set passes validation.

## Image Adapter Details

### Z-Image Turbo

The bundled profile uses:

- `cyberrealisticZImage_v50.safetensors`
- `qwen_3_4b.safetensors`
- `ae.safetensors`

The adapter writes the checkpoint to the workflow's `unet_name` input. It uses
zeroed negative conditioning and rejects non-empty negative prompts.

This is text-to-image generation. The current workflow does not accept a
reference image and is not an instruction-editing workflow.

### SDXL

The code includes an SDXL adapter and a base workflow. It supports negative
prompting and writes the checkpoint to `ckpt_name`.

The bundled profile does not include an SDXL model group or an SDXL workflow
preset. SDXL therefore requires a custom coherent profile before it can run.

### Qwen-Image-Edit-2511

The bundled opt-in adapter accepts one to three ordered reference images,
supports negative prompting, and derives output dimensions from Picture 1. Its
default workflow uses 40 steps, CFG 4, Euler, Simple, AuraFlow shift 3.1, and
CFGNorm strength 1.0. Model artifacts and hashes are documented in
[`qwen-image-edit-2511.md`](qwen-image-edit-2511.md).

## Wan Video Adapter Details

Only the `wan22_i2v` video adapter is currently registered.

The adapter controls fixed workflow nodes for:

- Positive and negative prompts.
- Start-image upload.
- Optional end-image upload.
- Width, height, frame count, and batch size.
- High-noise and low-noise sampling ranges.
- Seed, steps, and CFG.
- Output filename prefix and FPS.

The workflow must preserve the node IDs expected by the adapter. A visually
equivalent workflow with different node IDs is not automatically compatible.

## Validation and Common Failure Conditions

Manifest loading fails when:

- The root is not an object.
- `title` is missing or empty.
- `shots` is missing, empty, or not an array.
- A shot is not an object.
- Shot names duplicate exactly after trimming.
- Both global and current shot prompts are empty.
- Width or height is not a positive multiple of 16.
- FPS, CFG, or duration is not positive.
- Steps or transition split is invalid.
- Seed is outside the unsigned 64-bit range.
- The first shot has no start source.
- A shot declares both supplied and generated start images.
- A referenced image file does not exist.
- Generated-image dimensions are not positive multiples of 8.
- Generated-image sampling values are invalid.

Selected generated-image resolution fails when:

- The generated-image adapter differs from the selected workflow adapter.
- The selected start-image adapter name is unknown.
- Z-Image Turbo receives a non-empty negative prompt.
- Layered checkpoint, sampler, scheduler, steps, or CFG values do not resolve to
  valid adapter settings.

Execution or resume can fail when:

- FFmpeg is unavailable for video rendering or assembly.
- A required previous continuation is missing.
- Multiple videos or generated images make backfill ambiguous.
- A workflow output does not contain exactly one video of the adapter suffix.
- Start-image generation does not produce exactly one supported image.
- Metadata or output hashes do not match current inputs.
- The selected profile does not provide required model files.
- A workflow lacks a node required by its adapter.
- Workflow node options do not accept a configured checkpoint, sampler, or
  scheduler.

Plan mode validates the manifest, image paths, profile, workflow selection,
the active video adapter name, and selected generated-image requests. An
invalid generated-image request in an unselected shot may remain unnoticed.
Plan returns before loading the workflow JSON and therefore does not validate
required node IDs or remote ComfyUI option lists. A successful plan reduces
avoidable failures but does not guarantee that billable execution will queue
successfully.

## Authoring Checklist

Before rendering, verify all of the following:

- The title is stable and meaningful.
- Global prompt contains only persistent facts.
- Every shot has one clear current action.
- Every camera instruction is internally consistent.
- Every transition has a precise static `end_state` when continuity matters.
- The next shot logically begins from the previous state.
- The first shot has exactly one start source.
- Every later continuation-dependent shot has a valid predecessor.
- Generated start-image prompts are complete standalone still descriptions.
- Start-image aspect ratio matches video aspect ratio.
- Seeds are explicit for production reproducibility.
- Steps, transition split, CFG, FPS, and dimensions use a tested coherent
  baseline.
- End images are reachable from their corresponding start images.
- All image paths are relative to the manifest where practical.
- A plan validation succeeds before billable execution, with the understanding
  that workflow node structure and remote ComfyUI option lists are checked
  later.
- Start images are reviewed and approved before expensive video generation when
  composition or identity is critical.
- Resume is used only with the same output root and trusted metadata.
- Backfill is used only when the current manifest accurately describes old
  outputs.

## Authoritative Code Locations

The current behavior is implemented in:

- `src/runpod_video_automation/scene.py` for manifest parsing, validation,
  frame conversion, and path handling.
- `src/runpod_video_automation/adapters.py` for prompt assembly, defaults, and
  workflow node mutation.
- `src/runpod_video_automation/cli.py` for scene orchestration, input snapshots,
  shot selection, start-image review, resume, backfill, and output handling.
- `src/runpod_video_automation/render_metadata.py` for fingerprints, sidecars,
  output validation, and render manifests.
- `src/runpod_video_automation/config.py` for profiles, workflow presets, model
  groups, aliases, and default resolution.
- `profiles/wan22-i2v-fp8.json` for the bundled infrastructure, model, workflow,
  and adapter defaults.
- `workflows/wan22-i2v-14b-api.json` for the bundled video workflow.
- `workflows/z-image-turbo-start-image-api.json` for the bundled generated
  start-image workflow.
