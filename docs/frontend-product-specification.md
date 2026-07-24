# Frontend Product Specification

Status: Product and UX specification for implementation

Last updated: 2026-07-24

## 1. Purpose

This document defines the product, user experience, frontend behavior, domain
model, API expectations, security requirements, and acceptance criteria for a
multi-user SaaS application built on top of the video automation system.

The application lets users organize a storyboard, maintain visual continuity,
generate start images, end images, and scene videos, approve explicit variants,
and export the approved scenes as one video.

The words MUST, MUST NOT, SHOULD, and MAY are normative.

## 2. Product Scope

### 2.1 MVP capabilities

The MVP MUST provide:

- Email and password accounts for multiple independent users.
- German and English application interfaces.
- Private projects with an ordered storyboard.
- A project bible for characters, visual style, setting, and continuity.
- Up to 20 scenes per project.
- Separate start-image, end-image, and video prompts for every scene.
- A separate negative prompt for every generation target.
- Uploaded or generated start and end images.
- Image-to-video generation for scenes between 1 and 10 seconds long.
- Explicit variant approval for every media slot.
- Continuation from the preceding scene by default.
- Individual image and scene-video downloads.
- Automatic assembly and download of an approved project video.
- An asynchronous job queue with progress, retries, cancellation, and history.
- Prepaid billing in EUR and USD.
- In-app notifications, support tickets, and an administration area.

Adult content is part of the intended product scope. The selected payment
provider and any other external provider MUST permit the product's content
category. This document intentionally does not define rules about people shown
in generated or uploaded content. Age-verification requirements remain an open
product and legal decision.

### 2.2 Fixed media format

The MVP exposes one output format and no model or quality selector:

| Media | Format |
| --- | --- |
| Generated start and end images | 864x1200 pixels |
| Scene video | 576x800 pixels, 16 fps, 1-10 seconds |
| Downloaded video | MP4 |
| Audio | Not supported |

The system selects image and video models internally. Model names, samplers,
steps, and infrastructure details MUST NOT be normal user controls.
Generation uses one internally configured quality tier.

### 2.3 Non-goals

The MVP does not include:

- Public projects, public profiles, galleries, or share links.
- Team workspaces or collaborative project permissions.
- Audio generation, audio upload, soundtracks, or voice tracks.
- Transitions other than hard cuts.
- Arbitrary dimensions, frame rates, model selection, or quality tiers.
- Offline generation or a complete offline editor.
- Marketing email or job-completion email.

## 3. Product Principles

- Private by default: projects, prompts, uploads, and outputs are accessible
  only to their owner, purpose-bound contracted processors needed for an
  explicitly requested operation, or a narrowly scoped support grant.
- Explicit approval: a generated result never silently becomes the canonical
  project result.
- No surprise charges: every paid action is backed by an immutable price quote
  and an atomic wallet reservation.
- Recoverable editing: autosave, local undo, immutable project versions, and
  restore-as-new-version protect user work.
- Honest state: drafts, stale approvals, expired assets, retries, reservations,
  and pending payments are visible rather than hidden.
- Responsive by design: all user functions remain usable from 320 CSS pixels
  upward, rather than presenting a reduced mobile feature set.

## 4. Users, Roles, and Access

### 4.1 Roles

The MVP has two roles:

- `user`: owns projects, media, jobs, wallet activity, invoices, and tickets.
- `admin`: manages prices, templates, users, credits, jobs, and support.

An admin account MUST use multi-factor authentication before accessing any
administration function. Normal users MAY enable TOTP multi-factor
authentication and MUST receive one-time recovery codes when enabling it.

### 4.2 Account lifecycle

An account has these primary states:

- `pending_verification`
- `active`
- `suspended`
- `deletion_requested`
- `deletion_processing`
- `deleted`

Before email verification, a user can only verify the address, request another
verification message, or sign out. The editor, payments, and generation are not
available.

An administrative suspension MUST revoke active sessions and request
cancellation of active jobs. Relevant financial records and media remain
secured for investigation.

### 4.3 Sessions

- Browser authentication MUST use a Secure, HttpOnly, SameSite session cookie.
- State-changing requests MUST have CSRF protection.
- An active session uses a rolling lifetime of at most 30 days.
- Multiple concurrent devices are permitted.
- The account screen MUST list active sessions and allow revoking one or all.
- Password, email, MFA, data-export, and account-deletion operations MUST
  require recent re-authentication.
- Signing out MUST remove unsynchronized local drafts from that browser.

### 4.4 Passwords

- Password length MUST be between 12 and 128 characters.
- The system MUST NOT impose arbitrary uppercase, number, or symbol rules.
- Known compromised passwords MUST be rejected.
- Authentication endpoints MUST use rate limiting without creating a permanent
  account-lockout denial-of-service vector.

## 5. Project and Storyboard Model

### 5.1 Project

A project contains:

- A stable ID and user-visible name.
- A project language.
- A project bible.
- An ordered list of scenes.
- Immutable saved versions.
- Derived completion and freshness state.

A project starts either empty or from a published admin template. A template
contains a bible, scenes, and prompts, but never user media or generated media.

Duplicating a project copies its bible, scenes, and prompts. It MUST NOT copy or
reference assets, approvals, jobs, exports, or billing records.

### 5.2 Project bible

The project bible is the stable source of project-wide character, style,
setting, lighting, and continuity information. It has a dedicated inspector
mode.

Changing the project bible marks all scene approvals stale because the context
used to produce them has changed. Existing variants remain available until
their normal expiration or explicit deletion.

### 5.3 Scene

Each project contains no more than 20 ordered scenes. Each scene has:

- A stable ID and title.
- An integer duration from 1 through 10 seconds, in one-second increments.
- A start-image prompt and negative prompt.
- An end-image prompt and negative prompt.
- A video prompt and negative prompt.
- Start-image, end-image, and scene-video variant collections.
- One optional approved variant for each slot.
- A continuity source.

The end image is optional. The first scene requires an explicit uploaded or
generated start image before video generation.

For every later scene, the default start source is the final frame of the
approved preceding scene video. A user MAY override this with an uploaded or
generated start image. An explicit start image creates an independently
composed shot rather than a continuation dependency.

### 5.4 Prompts and prompt improvement

Every prompt field is independently editable and revisioned. An OpenRouter
improvement request:

- Targets exactly one prompt field.
- Uses the project bible and complete storyboard as read-only context.
- May change only the targeted prompt.
- Returns a conservative inline diff.
- Requires the user to accept the entire field replacement or reject it.
- Never applies automatically.

At most one improvement may run for a specific prompt field. Improvements for
different fields may run concurrently.

If the source prompt changes while an improvement is running, the resulting
suggestion is stale. It remains visible for comparison but cannot directly
replace the newer prompt. The suggestion text, diff, and applied or rejected
decision are retained in the associated project version.

OpenRouter improvement is included in the product and does not create a wallet
charge.

### 5.5 Variants and approvals

Every generation produces exactly one result. Re-running generation creates a
new variant and does not overwrite an earlier result.

For each media slot:

- Zero or one variant is approved.
- Approval selects the exact asset used by preview, continuation, and export.
- Prompt changes make the related approval stale.
- Project-bible changes make all approvals stale.
- A stale asset MAY be approved again after an explicit warning, without a new
  generation charge.
- A new result never becomes approved automatically.
- Older results are not deleted merely because a newer result exists.

A variant MAY be deleted only when it is not selected and is not an active input
to a continuation, export, waiting job, or other current project dependency. Its
historical origin job does not block deletion: that job retains an asset
tombstone, final cost, and metadata but no downloadable binary.

### 5.6 Generation snapshots and freshness

Every quote and generation job has an immutable intent snapshot. Quote
acceptance requires the target project revision to remain current; if it changed
after quoting, acceptance fails with `quote_input_changed` and the frontend
creates a new quote. Once accepted, editing may continue while the job runs.

Every generated asset records an immutable generation snapshot and canonical
generation fingerprint. The snapshot contains at least:

- Project, project revision, scene, and target slot.
- Project-bible content fingerprint.
- Positive and negative prompt fingerprints for the target.
- Duration, output dimensions, frame rate, and target media type.
- Continuity mode and ordered-scene context where relevant.
- IDs and content hashes of the exact approved start, end, and predecessor
  assets used by the generation.
- Internal workflow, model-configuration, and adapter revisions used for
  reproducibility, even though they are not exposed as user controls.

For a same-batch continuation, the immutable intent snapshot references the
predecessor job because its asset does not exist yet. Approval of that exact
result creates a separate immutable execution snapshot containing the resolved
asset ID and content hash. The intent snapshot is never rewritten.

Changing a later internal workflow revision does not by itself make an existing
approval stale. User-visible freshness is derived from the current project
dependency graph:

- A project-bible change makes every image and video approval stale.
- A target prompt or negative-prompt change makes that target approval stale.
- A video duration change makes that scene-video approval stale.
- Changing the approved start or end asset makes the consuming video stale.
- Changing an approved predecessor video, continuity mode, or scene order makes
  every affected downstream continuation video stale recursively.
- A missing or expired required asset makes the dependent result incomplete,
  not merely stale.

An approval is an append-only record containing the selected asset, current
project revision, current dependency fingerprint, and whether the user accepted
a stale-result warning. Re-approving an old asset records a new approval against
the current dependency fingerprint without changing the asset's original
generation snapshot.

If project inputs change after quote acceptance, a successful result is still
attached to its existing scene but may be stale immediately on arrival. If the
target scene was deleted, the result remains a private downloadable job result
until normal asset expiry but is not attached to the current storyboard.

### 5.7 Project versions and autosave

Autosave groups nearby edits into immutable saved project versions. Every
accepted save advances the project revision. Restoring an older version creates
a new current version rather than rewriting history.

Each project version includes the project bible, scene identities and order,
titles, durations, prompts, continuity modes, asset-selection IDs, and approval
basis fingerprints. It references but does not copy asset bytes, jobs, exports,
or billing records. Restoring a version reuses retained referenced assets. A
missing or expired referenced asset remains represented by its tombstone and
makes the restored project incomplete.

The editor also maintains a session-local undo and redo history for text and
structure changes. Project versions provide durable recovery beyond the local
session.

When the browser is offline:

- Text and structure edits are held as clearly marked local drafts.
- Server-dependent actions are disabled.
- Local drafts remain until synchronized, explicitly discarded, or signed out.
- Reconnection reloads the latest server revision and safely reapplies
  non-overlapping changes.
- Overlapping changes require an explicit user choice.

### 5.8 Completion and export

A project is export-ready only when every scene has an explicitly approved,
non-expired scene-video variant. Stale approvals remain visible but MUST block
export until the user explicitly re-approves or replaces them.

The complete export:

- Uses approved scene versions in storyboard order.
- Uses hard cuts.
- Contains no audio.
- Produces MP4.
- Is itself a generated asset with its own creation and expiration timestamps.

The user can preview and download each retained image, each retained scene MP4,
and each retained complete-project MP4 separately.

## 6. Information Architecture

### 6.1 User-facing areas

The application contains:

- Registration, verification, login, password reset, and MFA screens.
- Project library.
- Project editor.
- Jobs.
- Wallet, payments, transaction journal, and invoices.
- Notification inbox.
- Account, billing profile, sessions, data export, and deletion.
- Support tickets.
- Administration for authorized admins.

### 6.2 Global navigation

Desktop uses a narrow left rail with icons and labels for Projects, Jobs,
Wallet, Support, and Account. Mobile uses a persistent bottom navigation for
Projects, Jobs, Wallet, and More.

The global header provides:

- The current location and relevant project name.
- A compact wallet balance.
- The notification inbox.
- Account and theme controls.

## 7. Visual and Interaction Design

### 7.1 Theme and color

- The product MUST provide complete light and dark themes.
- The initial theme follows the operating-system preference.
- A user can override and persist the preference.
- Petrol is the primary functional accent.
- Accent color is used sparingly for primary actions, active navigation, and
  selected states rather than large decorative surfaces.
- Error, warning, success, focus, and information colors remain semantically
  distinct and meet WCAG contrast requirements in both themes.

### 7.2 Typography and shape

- IBM Plex Sans is the primary interface and reading typeface.
- IBM Plex Mono is reserved for IDs, dimensions, times, monetary values, and
  technical statuses.
- Controls and surfaces use precise 4-8 pixel corner radii.
- Borders and tonal separation are preferred over excessive floating cards.
- Information density is professional and compact without reducing touch
  targets or readability.

### 7.3 Accessibility

The product MUST meet WCAG 2.2 AA. At minimum:

- Every operation is keyboard accessible.
- Focus order and visible focus state are deterministic.
- Drag-and-drop always has menu and keyboard alternatives.
- Icon-only controls have accessible names.
- Status is never communicated by color alone.
- Form errors identify the field and remediation.
- Dialog and sheet focus is trapped and restored correctly.
- Live progress announcements are rate-limited and do not continuously disrupt
  screen readers.
- Reduced-motion preferences are respected.
- Media controls have accessible names and keyboard operation.

### 7.4 Responsive layout

The application MUST be fully operable at 320 CSS pixels without horizontal
page scrolling.

- Large screens show the complete three-column editor.
- Medium screens show the preview and either Storyboard or Inspector.
- Small screens prioritize the preview and use mutually exclusive bottom
  sheets for Scenes and Edit.
- Data-heavy desktop tables use a compact card or definition-list treatment on
  narrow screens rather than forcing page-level horizontal scrolling.
- Desktop panel widths and collapsed states are saved per user.

## 8. Screen Specifications

### 8.1 Project library

The primary project view is a responsive contact-sheet grid. Each project card
shows:

- The latest eligible visual preview or a neutral placeholder.
- Project name.
- Scene count.
- Last update time.
- Completion, stale, active-job, and expiry-warning states.

The library supports title search, status filtering, and sorting by creation or
last update. It does not require folders or tags in the MVP.

Creating a project opens a focused dialog for name, project language, and empty
or template starting point. Success opens the editor directly.

Project actions include rename, duplicate without media, move to trash, and
open. Trash is a separate filtered view showing the exact remaining retention
time, restore, and permanent delete actions.

### 8.2 Project editor

#### Large-screen structure

The editor consists of:

- Left: ordered Storyboard.
- Center: media Preview.
- Right: Inspector.

Storyboard and Inspector are independently resizable and collapsible. The
Preview consumes the remaining width.

#### Storyboard

Every scene row or card shows:

- Position and title.
- Duration.
- Best available thumbnail.
- Start, end, and video readiness.
- Approved, draft, stale, expired, queued, running, or failed indicators.
- Continuation dependency where applicable.

Scenes can be added, duplicated, deleted, and reordered. Reordering supports
drag-and-drop, an explicit Move action, and keyboard commands.

#### Preview

When a scene opens, Preview shows the approved variant first. If no approved
variant exists, it shows the newest successful result with a prominent Draft
label.

Preview provides:

- Image and video display.
- Explicit play controls; video never autoplays.
- Scrubbing and frame/time position for video.
- A variant filmstrip.
- Full-screen mode that retains the filmstrip, approval action, and metadata
  access.
- A synchronized A/B view on larger screens.
- An A/B toggle at the same playback position on small screens.

#### Inspector

The normal scene Inspector has Start, End, and Video tabs. Each tab groups its
main prompt, negative prompt, source or variants, freshness state, and relevant
generation settings. Negative prompts are collapsed by default but expose
status and character count. They open automatically when they contain an error
or require attention.

The project bible is a separate Inspector mode rather than another scene tab.

A sticky Inspector footer always shows:

- The exact action and target.
- Video duration when relevant.
- Current displayed price.
- Wallet sufficiency.
- Generate or price-confirmation action.

The editor header shows a compact autosave state: Saved, Saving, Offline, or
Conflict. Details and recovery actions are available from that state.

#### Jobs in the editor

A compact job bar shows the count and aggregate state of active jobs. Opening it
shows queue position, phase, attempt, percentage, approximate remaining time,
approval requirements and deadlines, and cancellation controls without
permanently consuming an editor column.

#### Mobile editor

The mobile editor keeps the vertical Preview visible as the primary context.
Fixed Scenes and Edit controls open two mutually exclusive bottom sheets. Each
sheet supports compact, half-height, and full-height snap points.

The paid action remains visible in the Inspector sheet. It MUST NOT be obscured
by browser chrome, the mobile bottom navigation, or the virtual keyboard.

#### Keyboard interaction

The editor includes shortcuts for save synchronization, undo, redo, scene
navigation, playback, and common scene actions. A searchable command palette
exposes shortcuts and less frequent operations. Paid and destructive commands
still require their normal safeguards when invoked from the palette.

### 8.3 Jobs

The Jobs page groups entries by batch and project. A batch expands into its
individual jobs. Filters cover status, job type, project, and date.

There is no product-level limit on the number of jobs a user may submit while
the wallet can reserve every accepted quote. Infrastructure capacity controls
execution through the visible queue rather than rejecting otherwise valid work.

A job detail shows:

- User-facing job and batch IDs.
- Project, scene, target slot, and immutable input snapshot.
- Quote and final charge.
- Queue position and estimated wait while queued.
- Required predecessor approval and exact deadline while waiting.
- Current phase, progress, attempt, and estimated remaining time while running.
- Attempt history and sanitized user-facing error details.
- Result asset when successful and not expired.
- Cancellation and manual retry actions when valid.

Job metadata, charges, and errors remain until project or account deletion,
even after the associated media expires.

### 8.4 Wallet and payments

The Wallet page shows Available, Reserved, and Total balances separately. Total
is the posted ledger balance, Reserved is the sum of open reservation
allocations, and Available equals Total minus Reserved. The page explains which
active jobs account for every reservation.

Top-up UI presents configured packages and a custom amount field together. It
shows currency, tax-inclusive final price, and credited amount before leaving
for hosted checkout.

After returning from the provider, a dedicated payment status page remains in
Pending until a trusted webhook confirms Success or Failure. Browser return
parameters never create wallet credit.

The immutable wallet journal is a cursor-paginated chronological table with
filters and these columns or mobile equivalents:

- Date and time.
- Transaction type.
- Project, job, payment, or admin reference.
- Amount.
- Balance after the entry.
- Status and explanation.

Invoices are downloadable as PDF from the payment history.

### 8.5 Notifications

The global header contains an inbox popover for recent notifications. A full
page provides filterable history and bulk read actions. Read state synchronizes
between devices.

Notifications cover at least:

- Job success, failure, and cancellation.
- Continuation waiting for approval and approval-deadline expiry.
- Payment success or failure.
- Asset expiration warning seven days before expiration.
- Asset expiration.
- Support replies and ticket state changes.
- Security-relevant in-app events.

Notifications have independent read and resolution timestamps. Informational
notifications are resolved at creation. Actionable notifications resolve when
the underlying action is no longer needed or the user explicitly dismisses
them. Resolved notifications expire 90 days after `resolved_at` and may be
deleted earlier by the user; unresolved notifications do not silently expire.
Email is reserved for account-security flows, not jobs, expiration, payments,
marketing, or support replies.

### 8.6 Account

The Account area includes:

- Profile and interface language.
- Light, dark, or system theme preference.
- Email and password controls.
- MFA and recovery codes.
- Active session management.
- Fixed wallet currency and its change restrictions.
- Billing profile.
- Asynchronous personal-data export.
- Account deletion.

### 8.7 Support

Users create in-app support tickets and may reference a project, job, payment,
or invoice. Ticket states are:

- `open`
- `waiting_for_user`
- `waiting_internal`
- `resolved`

A resolved ticket can be reopened. Replies and status changes are delivered in
the application, not by email.

### 8.8 Administration

The administration area contains:

- Price and top-up package management.
- User search, account state, and wallet corrections.
- Job and infrastructure diagnostics.
- Template management.
- Support queue.
- Audit log and pending dual-control approvals.

Prices and packages follow Draft, Review, and Published stages. A published
version may activate immediately or at a scheduled time. Existing quotes remain
valid at their immutable price until their individual expiration.

Templates follow Draft, Preview, and Published stages. Publishing a new version
does not alter existing projects.

Manual wallet corrections require an amount and non-empty reason. Corrections
above a configurable threshold require approval by a second admin. No admin may
approve their own pending high-value correction.

Admins can inspect sanitized technical job logs containing phases, worker data,
timings, and error codes. These logs exclude prompts, media, secrets, and signed
URLs by default. Admins can credit a user when justified but cannot directly
restart a paid user job. A new paid attempt remains a user action backed by a
new quote.

## 9. Domain Entities

The implementation SHOULD use the following conceptual entities. Physical
storage may normalize them differently while preserving their semantics.

### 9.1 Identity and account

- `User`: identity, role, locale, theme, account state, wallet currency.
- `Session`: device description, creation, last activity, expiry, revocation.
- `MfaCredential`: TOTP state and protected recovery-code records.
- `BillingProfile`: current invoice identity and address details.
- `SupportAccessGrant`: ticket, scope, consent, admin, reason, and expiry.
- `DataExport`: requested, processing, ready, expired, or failed export.

### 9.2 Project editing

- `Project`: owner, name, language, current revision, lifecycle timestamps.
- `ProjectVersion`: immutable complete logical snapshot, selected approval
  records, and change metadata.
- `Scene`: stable scene identity within project versions.
- `Prompt`: target type, positive text, negative text, and revision.
- `PromptSuggestion`: source revision, suggestion, diff, state, and decision.
- `Template`: stable identity and published template versions.

### 9.3 Media and generation

- `Upload`: signed-upload intent, expected metadata, and finalization state.
- `Asset`: owner, project, type, origin, dimensions, media metadata, hash,
  creation, expiry, and storage state.
- `AssetSelection`: append-only approval record with scene slot, selected asset,
  project revision, dependency fingerprint, warning acknowledgement, and
  freshness derived from current inputs.
- `PriceQuote`: currency, line items, total, price version, input snapshot, and
  expiration.
- `GenerationBatch`: atomic quote acceptance and grouped user intent.
- `GenerationJob`: one generation or export target, immutable intent snapshot,
  and optional immutable dependency-resolved execution snapshot.
- `GenerationAttempt`: attempt number, fencing token, timings, progress, worker,
  metered compute, and candidate result.

### 9.4 Billing

- `CreditLot`: one payment or administrative credit, remaining refundable
  amount, availability, and reservation allocations.
- `WalletReservation`: quote-level hold with immutable total and aggregate
  settlement state.
- `WalletReservationAllocation`: one quote line item allocated across credit
  lots, with open, settled, partially settled, or released state.
- `WalletTransaction`: immutable posted ledger debit, credit, or adjustment.
- `WalletJournalEvent`: append-only presentation event for a posted transaction
  or reservation transition; it is not a second balance source.
- `Payment`: provider-neutral checkout and webhook state.
- `Refund`: payment-linked requested, provider-pending, succeeded, or failed
  return with a temporary credit-lot hold.
- `Invoice`: immutable billing-profile and line-item snapshot with PDF.
- `Receivable`: a separate amount owed after a chargeback; never wallet debt.

### 9.5 Communication and administration

- `Notification`: type, payload reference, creation, read, resolution, and
  expiry timestamps.
- `SupportTicket`: user, status, references, messages, and access grants.
- `AdminAuditEvent`: actor, action, reason, target, before/after values, time.

## 10. State Models

### 10.1 Asset state

Upload processing and retained asset availability are separate state machines:

```text
upload_pending -> uploaded -> processing -> finalized
        |             |           \------> failed
        \-------------+------------------> abandoned

processing -> ready
     \------> failed

ready -> expired
ready -> user_deleted

blob_available -> deletion_pending -> blob_deleted
```

A generated asset starts in `processing` through its job and becomes `ready`
only after media validation and durable storage complete.

Asset-record availability and physical blob state MUST NOT be conflated.
Expiration deletes the blob and thumbnails but retains an asset tombstone with
safe metadata for project versions, job history, and expiry placeholders. User
deletion hides an unreferenced asset from normal UI and deletes its blob while
retaining only the minimal audit tombstone required by referenced history.

Approval is separate from asset storage state:

```text
unselected -> approved_current -> approved_stale
     ^               |                  |
     +---------------+------------------+
```

Re-approving a stale asset returns it to `approved_current`. Changing approval
does not change or copy the asset.

### 10.2 Quote state

```text
valid -> accepted
valid -> expired
valid -> invalidated
```

A quote is immutable and valid for ten minutes. Acceptance after expiry fails
and requires a new quote and visible confirmation. An accepted quote cannot be
accepted again as a new batch.

If the quoted project revision or target inputs changed before acceptance, the
quote becomes `invalidated`, acceptance returns `quote_input_changed`, and the
frontend obtains a new quote. Changes after successful acceptance do not alter
the job snapshot and may cause its result to arrive stale.

### 10.3 Job state

```text
blocked -> queued -> running -> succeeded
   |          |         |   \-> retrying -> running
   |          |         |   \-> cancellation_requested -> cancelled
   |          |         |   \-> cancellation_requested -> succeeded
   |          |         \-----> failed
   |          \---------------> cancelled
   |\-> waiting_for_approval -> queued
   |                 |       \-> approval_expired
   |                 \----------> cancelled
   |\-> dependency_failed
   \--> cancelled
```

A job receives at most two automatic retries after its first failed attempt.
The UI displays every attempt and resets attempt progress visibly when retrying.

If completion wins a race with cancellation, the state is `succeeded`, the
asset remains available, and the full quoted line-item price is charged.

Queue delivery and worker execution are at least once. Every attempt receives a
monotonically increasing fencing token. Only the currently valid token may
atomically commit a result while the job is non-terminal. A late or duplicate
attempt output is quarantined and deleted, cannot create another variant, and
cannot create another charge. Thus one generation job exposes exactly one
canonical result even when acknowledgement loss triggers a retry.

### 10.4 Dependency behavior

A continuation job depends on the approved output of the preceding scene. If a
job in a continuation chain fails permanently, dependent jobs do not start and
finish as `dependency_failed`, release their line-item reservations without a
charge, and show the blocking dependency. Independent jobs in the same project
or batch continue.

A batch may contain a continuation whose predecessor is another job in that
batch. The dependent job snapshot references the predecessor job and remains
`blocked`. When the predecessor succeeds, its result remains an unapproved
draft and the dependent job moves to `waiting_for_approval`; it never continues
automatically. Approving that exact predecessor result resolves the immutable
dependency and queues the child job.

The 24-hour approval-window policy is shown before batch acceptance. Its exact
deadline is calculated when the predecessor result becomes ready and displayed
while waiting. The dependent line-item reservation remains held during that
period. If the exact result is not approved before the deadline, the waiting job
becomes `approval_expired`, its allocation is released without charge, and its
descendants become `dependency_failed`. Continuing later requires a new quote.
An in-app notification is created when approval becomes necessary and before
the deadline where practical.

Cancelling a batch requests cancellation for every cancellable child job:

- Blocked and approval-waiting jobs cancel without compute charge.
- Queued jobs cancel without compute charge.
- Running jobs cancel on a best-effort basis and settle consumed compute.
- Completed jobs and their results remain unchanged.

### 10.5 Prompt suggestion state

```text
queued -> running -> ready -> applied
                    |   | \-> rejected
                    |   \--> stale
                    \------> failed
```

### 10.6 Payment state

```text
checkout_created -> pending -> succeeded
                         | \-> failed
                         \--> expired
                         \--> reconciliation_required -> refunded
                                                   \---> failed
succeeded -> partially_refunded -> refunded
succeeded ----------------------> disputed
```

Webhook order MUST NOT be trusted. Provider event IDs and state precedence make
processing idempotent and safe for duplicate or delayed events.

`reconciliation_required` represents captured or ambiguous provider funds that
were not credited to the wallet, such as a currency mismatch. Resolution either
confirms no payment or refunds the provider transaction; it never silently
credits an incompatible wallet.

A refund has its own asynchronous state:

```text
requested -> provider_pending -> succeeded
                         \-----> failed
```

Creating a refund temporarily holds the selected unreserved credit-lot amount.
Provider success atomically posts the wallet debit, reduces the lot, updates the
payment, and creates any legally required correction document. Provider failure
releases the hold without changing posted wallet balance.

### 10.7 Project derived state

Project completion is derived, not manually stored as a user-controlled flag.
At minimum, the UI derives:

- `draft`: at least one scene is not ready and no blocking expiry exists.
- `ready`: every scene has a current approved video whose required dependency
  graph is current and complete.
- `stale`: at least one required approval is stale.
- `incomplete`: a required referenced asset is missing or expired.
- `active`: at least one related job is blocked, waiting for approval, queued,
  retrying, running, or `cancellation_requested`.
- `trashing`: trash was requested and related jobs or reservations are still
  settling.
- `trashed`: project is inside its seven-day trash retention period.

Several derived labels may apply simultaneously, such as Active and Stale.

## 11. Pricing, Wallet, and Settlement

### 11.1 Currency

The MVP supports EUR and USD. A new account starts without a wallet currency.
Creating the first hosted checkout atomically sets a provisional currency
before contacting the provider. While that checkout or another payment is
pending, checkout in another currency is prohibited. The first successful
top-up makes the currency fixed.

If every checkout in the provisional currency fails or expires and no payment,
credit, reservation, or refund exists, the user may choose another currency. A
successful webhook in a currency that does not match the account's provisional
or fixed currency is quarantined for reconciliation and refund and MUST NOT
credit the wallet.

After the first successful top-up, currency can change only when:

- Available, reserved, and total wallet balances are zero.
- No quote reservation is open.
- No payment or refund is pending.

EUR and USD have independently published price lists. The product does not
perform automatic wallet conversion.

All monetary values MUST be stored as integer minor units. Floating-point money
is prohibited.

The posted Total balance is the sum of immutable posted wallet transactions.
Reserved balance is the sum of open reservation allocations. Available balance
is `Total - Reserved` and MUST never be negative. Reservation and release events
are append-only wallet-journal events but do not alter the posted Total balance.

### 11.2 Prices

- Every image generation has a configured fixed price and returns one image.
- Video prices are independently fixed for each integer duration from 1 through
  10 seconds. The UI uses a one-second step and the API rejects fractional or
  out-of-range durations.
- Prices shown to consumers include applicable taxes and payment fees.
- OpenRouter prompt improvement is included and has no separate price.
- Price lists and top-up limits are admin-configurable and versioned.

The exact prices, package values, and custom top-up limits remain open product
decisions.

### 11.3 Quote and reservation

Every paid action uses this sequence:

1. The server creates an immutable quote from an immutable input snapshot.
2. The UI shows the line items, currency, total, and expiration.
3. Quote acceptance atomically allocates every line item across available
   credit lots, reserves the entire amount, and creates the job or batch.
4. Success settles that line-item allocation into exactly one posted ledger
   debit and reduces the allocated credit lots.
5. Failure, dependency failure, approval timeout, or pre-compute cancellation
   releases that line-item allocation without a generation charge.
6. Partial cancellation atomically posts the measured debit and releases the
   unused remainder of that line-item allocation.

Batch reservation is all-or-nothing. The system never starts only the portion a
wallet can afford. Each quote line has an immutable allocation and may settle or
release independently. Every allocation reaches one final accounting outcome;
mixed batch results never require splitting or rewriting a posted transaction.

Users may globally disable routine price-confirmation dialogs. The current price
remains visible on the Generate control. Automatic start is allowed only when
the accepted quote exactly matches the price last shown to the user. Any price
change requires confirmation; an expired quote always requires a new quote.

Quote acceptance MUST be idempotent. Repeating a request with the same
idempotency key returns the original batch or job and cannot reserve or charge
again.

### 11.4 Failures, retries, and cancellation

- Technical failures and their automatic retries cost the user nothing.
- A queued cancellation costs nothing.
- A running cancellation is charged from backend-measured consumed compute at
  the configured internal rate, capped by the quoted line-item amount.
- A successful result completed during a cancellation race costs the full
  quoted amount.
- Manual retry after final failure requires a new quote and current price.
- Wallet balance can never become negative.

The UI does not attempt to calculate final cancellation cost. It displays the
backend settlement and its relationship to the reserved maximum.

Every quote line stores the cancellation meter version and a fixed-point rate.
Workers record billable compute milliseconds from accepted execution until
confirmed stop, excluding queue time and automatic-retry attempts that fail
technically. The charge is calculated with integer arithmetic, rounded down to
the nearest minor currency unit, and capped by the line-item price. Meter input,
rate version, calculated debit, and released remainder are retained for audit.

### 11.5 Top-up and refund

The provider uses hosted checkout. Purchased credit becomes available only
after a verified successful webhook.

Credit does not expire. Payment-provider fees are included in the displayed
top-up price.

Every successful payment creates one refundable credit lot. Administrative
credits create separately identified lots and are non-refundable unless the
admin action explicitly states otherwise. Reservations allocate available lots
in deterministic FIFO order by `available_at` and stable lot ID. Settlement
reduces those exact lots; release returns the allocation to the same lots. This
attribution is immutable and auditable.

A manual refund goes to the original payment method when possible. The
refundable amount cannot exceed the payment lot's unconsumed and unreserved
remainder. Starting a refund holds that remainder so new jobs cannot reserve it.
Provider success atomically reduces the lot and posts the debit; provider
failure releases the hold. The transition MUST be idempotent. If provider refund
is impossible, an admin may issue a documented wallet correction where
appropriate.

### 11.6 Chargeback

On chargeback, jobs with open reservations using the disputed payment lot are
cancelled and their allocations settle or release first. The system then removes
the lot's remaining unconsumed and unreserved amount. If the disputed amount
exceeds that remaining lot after reconciliation:

- The wallet remains non-negative.
- A separate receivable records the amount owed.
- The account is suspended from further use.
- Resolution requires payment recovery or documented manual review.

### 11.7 Billing profile and invoices

The user maintains a billing profile. Every successful top-up creates a PDF
invoice from an immutable snapshot of that profile. Later profile changes affect
only future invoices.

Legally required invoice and accounting data may remain after account deletion
for the applicable retention period. It must be minimized, access-restricted,
and unavailable for product use.

## 12. Media Retention and Deletion

### 12.1 Asset expiration

Each generated or uploaded image, scene video, and complete export expires 30
days after its own creation. Creating another asset does not extend earlier
assets.

Seven days before expiration, the user receives an in-app notification. There
is no expiration email.

After expiration:

- The binary and thumbnails are unavailable.
- The UI retains a placeholder with slot, origin, creation, and expiration
  metadata.
- A referenced expired asset makes the project incomplete.
- Preview, dependent generation, and export remain blocked until replacement.

### 12.2 Project trash

Moving a project with no active work to trash starts a seven-day restoration
period immediately. If the project has blocked, approval-waiting, queued,
retrying, running, or cancellation-pending jobs, the confirmation shows that
partial cancellation charges may apply. Confirmation changes the project to
`trashing`, rejects new project work, requests cancellation, and waits for every
reservation allocation to settle or release before starting the seven-day
period.

A result that wins a cancellation race is attached to the trashed project and
charged normally, but it is inaccessible unless the project is restored. The
trash timer and normal 30-day asset-expiry timers run independently.

After seven days, project media and non-required metadata are deleted. Permanent
deletion from Trash bypasses the remaining period after explicit confirmation,
but physical deletion starts only after all related jobs and reservations are
terminal. Financial ledger records remain under their own retention rules.

### 12.3 Account deletion

Account deletion is immediate after final informed confirmation but uses a
settlement stage when the exact financial consequence is not yet known.

Starting deletion requires recent password verification and an exact
confirmation text such as the account name or `DELETE`. If there are no active
jobs, open reservations, pending payments or refunds, positive balance, or
receivable, this confirmation may atomically become final deletion.

Otherwise, the account enters `deletion_requested`:

- Normal product sessions are revoked and the initiating browser receives a
  narrowly scoped deletion-settlement session.
- Project editing, media access, generation, uploads, and checkout are disabled
  immediately. Only deletion-specific support communication remains available.
- Active and approval-waiting jobs are cancelled and every allocation is
  settled or released.
- Open checkout sessions are cancelled or allowed to expire. Pending payments,
  refunds, chargebacks, and already received provider events reach a terminal
  reconciled state before deletion continues.
- A late successful payment is credited only long enough to be reconciled and
  refunded; it cannot reactivate product access.

After settlement, the API calculates the exact posted balance and refundability
from credit lots. Eligible unused payment lots are refunded to their original
payment methods and deletion waits for each refund result. The restricted page
shows the exact refunded amount, any non-refundable residual credit, and any
unresolved receivable. Non-refundable residual credit must be explicitly
relinquished. A receivable or failed provider reconciliation requires documented
support resolution rather than silently deleting the obligation.

The user may cancel `deletion_requested` before final confirmation. Normal
access resumes only after in-flight settlement operations are terminal; jobs
already cancelled and refunds already completed are not reversed.

The final confirmation is enabled only when all amounts and provider outcomes
are known. It transitions the account to `deletion_processing` and:

- Revokes the restricted deletion-settlement session and all remaining
  sessions.
- Removes product and media access permanently, with no recovery grace period.
- Deletes media, derivatives, raw uploads, exports, and active object-storage
  records within 24 hours.
- Makes backup copies inaccessible to product systems immediately; encrypted
  backups age out under the documented backup-retention schedule.
- Retains only minimized, access-restricted records required for accounting,
  disputes, security, or other law.
- Allows the same email address to register a completely new account later.
- Never restores projects, wallet value, or history to the new account.

## 13. API Contract

### 13.1 General conventions

- API base path: `/api/v1`.
- Resource API: REST over HTTPS with JSON request and response bodies.
- API timestamps: UTC with explicit offsets.
- List endpoints: cursor pagination with stable ordering.
- Errors: RFC 9457 Problem Details.
- Browser authentication: session cookie and CSRF protection.
- Paid and otherwise retry-sensitive mutations: `Idempotency-Key` header.
- Project mutations: optimistic concurrency with an expected revision.
- Downloads: short-lived authorized URLs, never permanent public URLs.

Problem responses SHOULD add stable `code`, optional `field_errors`, and a
support-safe `trace_id` to the standard Problem Details members. Error details
MUST NOT expose prompts, storage paths, credentials, provider payloads, or
internal stack traces.

Project mutations SHOULD use an `If-Match` ETag. A stale precondition returns
`412 Precondition Failed` with the current revision reference and enough safe
metadata for the client to reload and merge.

### 13.2 Core endpoint groups

The exact response schemas belong in a future OpenAPI 3.1 document. The REST API
MUST cover at least these operations.

#### Authentication and account

| Method and path | Purpose |
| --- | --- |
| `POST /auth/register` | Register and send verification |
| `POST /auth/verify-email` | Verify email token |
| `POST /auth/verification-emails` | Resend verification with rate limiting |
| `POST /auth/login` | Create browser session |
| `POST /auth/logout` | Revoke current session |
| `POST /auth/password-reset` | Start password reset |
| `POST /auth/password-reset/complete` | Complete password reset |
| `GET /account` | Read profile and preferences |
| `PATCH /account` | Update profile or preferences |
| `GET /account/sessions` | List active sessions |
| `DELETE /account/sessions/{id}` | Revoke one session |
| `DELETE /account/sessions` | Revoke all other sessions |
| `POST /account/mfa/totp` | Start TOTP enrollment |
| `POST /account/mfa/totp/confirm` | Confirm TOTP enrollment |
| `DELETE /account/mfa/totp` | Disable TOTP after step-up |
| `POST /account/mfa/recovery-codes` | Rotate recovery codes after step-up |
| `GET /account/billing-profile` | Read current billing profile |
| `PATCH /account/billing-profile` | Update future-invoice profile |
| `POST /account/data-exports` | Request personal-data export |
| `GET /account/data-exports` | List export requests and states |
| `GET /account/data-exports/{exportId}` | Read export state and expiry |
| `POST /account/data-exports/{exportId}/download` | Create authorized archive download |
| `POST /account/deletion-requests` | Re-authenticate and start settlement |
| `GET /account/deletion-requests/{requestId}` | Read settlement and exact amounts |
| `POST /account/deletion-requests/{requestId}/confirm` | Final irreversible confirmation |
| `DELETE /account/deletion-requests/{requestId}` | Cancel before final confirmation |

#### Projects and scenes

| Method and path | Purpose |
| --- | --- |
| `GET /templates` | List published project templates |
| `GET /templates/{templateId}` | Preview one published template |
| `GET /projects` | Search, filter, sort, and paginate projects |
| `POST /projects` | Create empty or template-based project |
| `GET /projects/{id}` | Read current complete project state |
| `PATCH /projects/{id}` | Autosave a revisioned project patch |
| `POST /projects/{id}/duplicate` | Duplicate without media |
| `DELETE /projects/{id}` | Move project to trash |
| `POST /projects/{id}/restore` | Restore from trash |
| `DELETE /projects/{id}/permanent` | Permanently delete from trash |
| `GET /projects/{id}/versions` | Paginate immutable versions |
| `POST /projects/{id}/versions/{version}/restore` | Restore as new version |
| `POST /projects/{id}/scenes` | Add a scene |
| `PATCH /projects/{id}/scenes/{sceneId}` | Update a scene |
| `DELETE /projects/{id}/scenes/{sceneId}` | Remove a scene |
| `POST /projects/{id}/scenes/reorder` | Reorder scenes atomically |

Every project, scene, reorder, version-restore, asset-selection, prompt-apply,
and prompt-rejection mutation participates in project optimistic concurrency
and advances the project revision. Approval records are append-only; clearing or
changing approval creates a new selection state in the new project version.

#### Uploads and assets

| Method and path | Purpose |
| --- | --- |
| `POST /uploads` | Validate intent and create signed upload URL |
| `POST /uploads/{id}/finalize` | Verify bytes and create processing asset |
| `GET /assets/{id}` | Read authorized metadata |
| `POST /assets/{id}/download` | Create short-lived download URL |
| `DELETE /assets/{id}` | Delete unselected, unreferenced asset |
| `POST /projects/{id}/asset-selections` | Approve or re-approve exact variant |
| `DELETE /projects/{id}/asset-selections/{selectionId}` | Clear current approval |

#### Prompt suggestions

| Method and path | Purpose |
| --- | --- |
| `POST /projects/{id}/prompt-suggestions` | Improve one prompt field |
| `GET /projects/{id}/prompt-suggestions/{id}` | Read suggestion and diff |
| `POST /projects/{id}/prompt-suggestions/{id}/apply` | Apply as new project revision |
| `POST /projects/{id}/prompt-suggestions/{id}/reject` | Record rejection in version history |

#### Quotes, batches, and jobs

| Method and path | Purpose |
| --- | --- |
| `POST /quotes` | Quote one action or a batch from an input snapshot |
| `GET /quotes/{id}` | Read immutable quote and expiry |
| `POST /quotes/{id}/accept` | Atomically reserve and start |
| `GET /batches/{id}` | Read batch and child summaries |
| `POST /batches/{id}/cancel` | Cancel every cancellable child |
| `GET /jobs` | Filter and paginate job history |
| `GET /jobs/{id}` | Read snapshot, attempts, cost, and result |
| `POST /jobs/{id}/cancel` | Request idempotent cancellation |

Manual retry is expressed by requesting and accepting a new quote referencing
the failed job. It is not an unpriced direct restart endpoint.

#### Wallet, payments, and invoices

| Method and path | Purpose |
| --- | --- |
| `GET /wallet` | Read available, reserved, and total balance |
| `GET /wallet/transactions` | Filter and paginate ledger entries |
| `GET /wallet/journal` | Paginate ledger plus reservation and release events |
| `GET /prices` | Read active prices and top-up packages |
| `POST /payments/checkout-sessions` | Create hosted checkout |
| `GET /payments/{id}` | Read webhook-backed payment state |
| `GET /refunds/{id}` | Read refund and provider state |
| `GET /invoices` | Paginate invoices |
| `POST /invoices/{id}/download` | Create authorized PDF download |

Provider webhooks use a provider-specific endpoint outside browser session
authentication. They MUST verify signatures, retain provider event IDs, and
process duplicates and out-of-order delivery safely.

#### Notifications and support

| Method and path | Purpose |
| --- | --- |
| `GET /notifications` | Filter and paginate notification history |
| `PATCH /notifications/{id}` | Change read state |
| `POST /notifications/mark-all-read` | Mark current result scope read |
| `DELETE /notifications/{id}` | Delete one notification |
| `GET /support-tickets` | List user's tickets |
| `POST /support-tickets` | Create ticket with resource references |
| `GET /support-tickets/{id}` | Read ticket conversation |
| `POST /support-tickets/{id}/messages` | Add message |
| `POST /support-tickets/{ticketId}/content-grants` | Grant scoped support access |
| `DELETE /support-tickets/{ticketId}/content-grants/{grantId}` | Revoke access early |

#### Realtime events

| Method and path | Purpose |
| --- | --- |
| `GET /events` | Open authenticated user-level SSE stream |

#### Administration

Admin endpoints MUST cover versioned prices and templates, user suspension,
wallet-correction approval, payment refund creation, refund reconciliation, job
diagnostics, support workflow, scoped content access, and audit-log search. They
MUST use the same resource and error conventions and enforce MFA and step-up
requirements server-side.

At minimum, financial administration includes:

| Method and path | Purpose |
| --- | --- |
| `POST /admin/payments/{paymentId}/refunds` | Create bounded provider refund |
| `GET /admin/refunds/{refundId}` | Inspect refund and reconciliation state |
| `POST /admin/wallet-corrections` | Propose reasoned wallet correction |
| `POST /admin/wallet-corrections/{correctionId}/approve` | Second-admin approval |

### 13.3 Direct upload flow

Supported input types are static JPEG, PNG, and WebP, at most 20 MB. GIF, SVG,
animated images, and mismatched file signatures are rejected.

The flow is:

1. Client requests an upload intent with expected size, type, and purpose.
2. API returns a short-lived signed destination for private object storage.
3. Browser uploads bytes directly.
4. Client finalizes the upload.
5. Server verifies actual type, size, hash, dimensions, and decodeability.
6. Server strips metadata and processes the user-selected crop.
7. Server creates the normalized 864x1200 asset.

An upload is not a usable asset before successful finalization. Abandoned upload
objects are never exposed as project media and are physically deleted within 24
hours after their signed upload intent expires. After successful normalization,
the raw upload and its metadata-bearing source are deleted within 24 hours; only
the normalized private asset and required tombstone metadata remain.

### 13.4 Optimistic autosave

Autosave sends only changed fields plus the expected project revision. Success
returns the new revision and normalized changed resources.

On conflict, the client:

1. Keeps the local draft intact.
2. Loads the new server revision.
3. Reapplies non-overlapping local changes.
4. Presents overlapping fields for explicit resolution.
5. Retries against the resulting current revision.

Last-write-wins is prohibited for project editing.

### 13.5 Quote acceptance transaction

`POST /quotes/{id}/accept` MUST perform these operations atomically:

1. Return the previously committed response if the idempotency key already
   resolved, regardless of later project changes.
2. Reserve the new idempotency key for this operation.
3. Validate ownership, quote state, expiry, currency, current project revision,
   target existence, input snapshot, and disclosed approval deadlines.
4. Confirm sufficient Available balance for the entire quote.
5. Allocate every quote line across credit lots in FIFO order.
6. Create the quote-level reservation and line-item allocations.
7. Create the batch, blocked dependencies, and all intended jobs.
8. Persist the idempotent response and durable queue-outbox records.
9. Commit before queue dispatch.

Queue dispatch occurs from durable committed state. A worker outage after the
transaction MUST NOT lose the user's accepted job or create another charge.

### 13.6 Server-Sent Events

The browser opens one authenticated user-level SSE stream. The stream carries
jobs, assets, wallet, project revisions, notifications, payments, and support
updates.

Every event uses an envelope equivalent to:

```json
{
  "event_id": "stable-event-id",
  "type": "job.progress",
  "occurred_at": "2026-07-24T12:00:00Z",
  "resource_type": "generation_job",
  "resource_id": "job-id",
  "resource_revision": 14,
  "project_id": "project-id-or-null",
  "project_revision": 7,
  "data": {}
}
```

The SSE frame's `id:` value MUST equal `event_id`. `resource_revision` is
monotonic for that resource. `project_revision` is present only when the event
relates to a specific saved project revision and MUST NOT be overloaded with a
wallet, job, payment, or event-stream sequence.

Required event families include:

- `job.queued`
- `job.blocked`
- `job.waiting_for_approval`
- `job.approval_expired`
- `job.started`
- `job.progress`
- `job.retrying`
- `job.cancellation_requested`
- `job.cancelled`
- `job.succeeded`
- `job.failed`
- `job.dependency_failed`
- `asset.ready`
- `asset.expiring`
- `asset.expired`
- `wallet.changed`
- `payment.changed`
- `refund.changed`
- `project.revision_changed`
- `notification.created`
- `notification.changed`
- `support_ticket.changed`
- `data_export.changed`
- `stream.reset_required`

SSE delivery is at least once. The client deduplicates by event ID and treats
REST resource state as authoritative.

The server retains replayable events for 24 hours. The client reconnects with
`Last-Event-ID`. If replay is no longer possible, the server emits
`stream.reset_required` and closes the stream. The client then reloads all
authoritative volatile resources through REST and reconnects without
`Last-Event-ID`.

Progress events are emitted no more than once per second per job. They include
phase, attempt number, attempt progress, and approximate remaining time. A retry
clearly starts a new attempt; it is not hidden behind artificial monotonic
progress.

If SSE remains unavailable, the frontend falls back to polling active jobs,
wallet, and other currently visible volatile resources until streaming recovers.
Signed URLs and sensitive payloads MUST NOT appear in an event.

## 14. Security and Privacy

### 14.1 Private content

- Object storage is private.
- Media access is authorized for every request.
- Download URLs are short-lived and scoped.
- Browser and CDN caches MUST use privacy-appropriate controls.
- Logs and metrics MUST not contain prompt text, media bytes, session tokens,
  signed URLs, payment payloads, or TOTP secrets.

### 14.2 Contracted processors

Private content may be disclosed to an approved contracted processor only to
perform an operation explicitly requested by the user:

- OpenRouter receives the targeted prompt plus the project bible and complete
  storyboard context only when the user requests prompt improvement.
- The generation provider receives only the prompts, approved input assets, and
  technical settings required for the accepted generation job.
- Private object storage processes uploaded and generated bytes for retention
  and delivery.
- The payment provider receives billing and checkout data, never project prompts
  or media.

The UI and privacy notice disclose these processor categories and the unusually
broad project context required for prompt improvement before first use. Provider
contracts and configuration MUST permit the product's content category and
require purpose limitation, no model training or independent reuse, encryption
in transit and at rest, documented processing regions, a data-processing
agreement where applicable, bounded operational retention, and deletion after
the requested operation or contractual retention period. A processor that
cannot meet these requirements cannot be used.

### 14.3 Admin content access

Admins see only metadata by default. Opening a user's prompt or media for
support normally requires:

- An active ticket.
- Explicit user consent scoped to that ticket and content.
- Admin step-up authentication.
- A non-empty reason.
- An immutable audit event.
- Automatic expiry after at most 30 minutes.

The user can revoke a grant before expiry.

Break-glass access is limited to genuine security incidents. It requires step-up
authentication, an explicit incident reason, enhanced audit marking, and later
review. It cannot become a routine support shortcut.

### 14.4 Administration controls

Audit events include actor, action, target, reason, timestamp, and applicable
before and after values. Audit events are append-only and retained for at least
24 months.

Administrative MFA, step-up checks, dual-control thresholds, and content scopes
MUST be enforced by the API rather than only hidden in the frontend.

### 14.5 Analytics and cookies

The MVP uses only privacy-minimizing first-party product metrics. Metrics MUST
exclude prompts, media, sensitive free text, and cross-site identifiers.

Only technically necessary cookies are used for session, CSRF, and applicable
preference functions. There are no marketing cookies or third-party analytics
trackers in the MVP.

### 14.6 Personal-data export

A user can request an asynchronous archive containing structured account data
and media that has not expired. The archive:

- Requires recent re-authentication to request and download.
- Is created as a background job.
- Is private and encrypted at rest.
- Uses a short-lived authorized download.
- Expires and is deleted automatically.
- Creates an in-app security event when ready or downloaded.

## 15. Non-Functional Requirements

### 15.1 Browser support

The frontend supports the latest two major stable versions of Chrome, Edge,
Firefox, and Safari, including iOS Safari.

### 15.2 Performance

Core user pages SHOULD meet good Core Web Vitals at the 75th percentile:

- LCP at most 2.5 seconds.
- INP at most 200 milliseconds.
- CLS at most 0.1.

Large editor code, administration code, media comparison tools, and charts
SHOULD be loaded only when needed. Contact-sheet images use appropriately sized
private derivatives and must not download full media files for thumbnails.

### 15.3 Localization

- All UI strings, validation messages, problem mappings, and accessibility
  labels are localizable.
- German and English are complete at launch.
- The API stores timestamps in UTC.
- The UI formats dates, times, numbers, and money for the chosen language and
  local browser time zone.
- Currency code remains explicit wherever an amount could be ambiguous.
- User prompt text and project content are never translated automatically.

### 15.4 Resilience

- Refreshing the browser does not duplicate jobs, reservations, payments, or
  prompt-improvement requests.
- Temporary SSE loss does not block editing.
- Temporary API loss preserves visible local drafts.
- Every asynchronous terminal state can be reconstructed from REST without
  relying on a previously received event.
- User-facing failures provide a stable support trace ID without exposing
  internal details.

## 16. Acceptance Criteria

### 16.1 Identity and sessions

1. Given a newly registered unverified account, when the user signs in, then
   only verification, resend, and sign-out actions are available.
2. Given a verified account and correct credentials, when login succeeds, then
   a Secure HttpOnly session is created without exposing an API token to
   browser JavaScript.
3. Given two active devices, when one session is revoked from Account, then
   that device loses access while the other session remains active.
4. Given an admin without MFA, when an admin route or endpoint is requested,
   then access is denied until MFA enrollment and verification are complete.

### 16.2 Editing and concurrency

1. Given project revision 12, when a valid patch expecting revision 12 is
   accepted, then revision 13 is saved atomically and connected sessions receive
   a revision event.
2. Given a newer server revision, when an older tab attempts autosave, then no
   newer data is overwritten and the client enters conflict resolution.
3. Given non-overlapping server and local changes, when reconnecting, then the
   client reapplies the local changes to the latest revision.
4. Given overlapping changes, when reconnecting, then the user chooses the
   resulting field value before synchronization.
5. Given a network loss during text editing, when the user continues typing,
   then the draft remains local and visibly unsaved while paid and upload-final
   actions are disabled.
6. Given a local offline draft, when it synchronizes successfully, then the
   local copy is removed.
7. Given a local offline draft, when the user signs out, then the local copy is
   removed from that browser.
8. Given a scene duration is fractional, below 1, or above 10 seconds, when save
   or quote is requested, then the API rejects it with a field error.

### 16.3 Uploads and assets

1. Given a static JPEG, PNG, or WebP no larger than 20 MB, when upload, crop,
   and finalization succeed, then exactly one private 864x1200 asset becomes
   ready.
2. Given a mismatched signature, animation, invalid image, or oversized file,
   when finalization runs, then no usable asset is created and the user receives
   a clear error.
3. Given several variants, when one is approved, then exactly that asset drives
   Preview, dependencies, and export.
4. Given an approved asset and a related prompt change, when the save succeeds,
   then the approval is stale but the asset remains retained.
5. Given a stale approval, when the user accepts the warning and re-approves it,
   then it becomes current without a generation charge.
6. Given a selected asset or active project dependency, when deletion is
   requested, then the API rejects deletion and explains the active reference.
7. Given a scene video generated from an approved start image, when the start
   selection changes, then that video and every affected downstream
   continuation become stale recursively.
8. Given a job accepted from revision 12, when revision 13 changes one of its
   generation dependencies before completion, then the successful result is
   attached with a stale state rather than presented as current.
9. Given a restored project version that references an expired asset, when the
   restore completes, then the tombstone remains selected and the project is
   incomplete.
10. Given a finalized upload, when 24 hours pass, then the raw source object is
    deleted while the normalized private asset remains.
11. Given an unselected asset referenced only by historical job origin, when the
    user deletes it, then its blob is removed and job history retains a
    non-downloadable tombstone.

### 16.4 Prompt improvement

1. Given a current prompt revision, when an improvement completes, then the UI
   displays a field-level inline diff and does not apply it automatically.
2. Given a ready suggestion, when the user accepts it, then the complete target
   field is replaced and a new project revision is created.
3. Given a running suggestion, when the source prompt changes, then the result
   is marked stale and cannot overwrite the newer field directly.
4. Given two different prompt fields, when improvements are requested, then
   they may run concurrently.
5. Given a field with an active improvement, when another request targets the
   same revision and field, then the API does not create a duplicate active
   request.

### 16.5 Quotes and wallet

1. Given a valid ten-minute quote and sufficient available credit, when it is
   accepted with a new idempotency key, then reservation and job or batch
   creation happen exactly once in one transaction.
2. Given the same idempotency key, when acceptance is repeated, then the same
   response is returned without another reservation or job.
3. Given insufficient credit for a batch, when acceptance is attempted, then no
   child job starts and no partial reservation is made.
4. Given an expired quote, when acceptance is attempted, then no job starts and
   a newly priced quote requires visible confirmation.
5. Given disabled routine price confirmation, when a new quote differs from the
   last displayed price, then the job does not auto-start and the new price is
   shown for confirmation.
6. Given concurrent accept requests whose sum exceeds available credit, when
   they race, then only atomically affordable requests succeed and wallet
   balance never becomes negative.
7. Given a quote for project revision 12, when the project advances to revision
   13 before acceptance, then acceptance returns `quote_input_changed`, reserves
   nothing, and requires a new quote.
8. Given a batch with mixed successful, failed, and partially cancelled jobs,
   when all children become terminal, then each line-item allocation has exactly
   one auditable settlement or release and their sum equals the original hold.
9. Given a first EUR checkout is pending, when USD checkout is requested, then
   it is rejected until the provisional EUR payment state is resolved.
10. Given a reservation across several credit lots, when it settles, then FIFO
    attribution and remaining refundable amount are reproducible from immutable
    allocation records.

### 16.6 Jobs, retries, and cancellation

1. Given a running job, when progress arrives, then the UI displays phase,
   attempt, percentage, and approximate remaining time at no more than one
   update per second.
2. Given a technical failure on attempt one or two, when automatic retry starts,
   then the same job shows a new attempt and its visible progress restarts.
3. Given three failed attempts, when the job becomes terminal, then its
   line-item allocation is released and a manual retry requires a new quote.
4. Given a failed continuation job, when dependent jobs are queued, then those
   jobs finish as dependency-failed without a charge while independent jobs
   continue.
5. Given a queued job, when cancellation succeeds, then the complete allocation
   for that line item is released.
6. Given a running job, when cancellation succeeds before output completion,
   then the measured compute charge is no greater than the reserved line item.
7. Given a cancellation request and a result that completes first, then the job
   succeeds, the result remains available, and the full quoted price is charged.
8. Given a batch cancellation, when child jobs have mixed states, then every
   cancellable job receives cancellation while completed results remain.
9. Given a predecessor job in the same continuation batch succeeds, when its
   result is ready, then the child waits for explicit approval and does not
   consume compute automatically.
10. Given a continuation child waiting for approval, when the exact predecessor
    result is approved within 24 hours, then the child queues against that exact
    asset and its original immutable intent snapshot.
11. Given the 24-hour approval deadline passes, when the child is still waiting,
    then it becomes approval-expired and its complete line allocation is
    released without charge.
12. Given an old attempt returns after a newer fencing token exists, when it
    tries to commit output, then no second variant or charge is created and the
    late blob is deleted.
13. Given a running cancellation with a partial charge, when settlement commits,
    then the measured debit and release of the unused allocation remainder are
    atomic.

### 16.7 Preview, approval, and export

1. Given an approved variant, when a scene opens, then Preview selects it before
   any newer unapproved result.
2. Given no approved variant and at least one successful result, when a scene
   opens, then Preview selects the newest result with a visible Draft label.
3. Given a selected video, when the scene opens, then playback does not start
   until user action.
4. Given two variants in mobile A/B mode, when A/B is toggled, then both are
   shown at the same playback position where possible.
5. Given any scene without a current, non-expired approved video and current
   transitive dependency graph, when complete export is requested, then export
   is blocked with the exact affected scenes and dependencies.
6. Given every scene has a valid explicit approval, when export succeeds, then
   one silent MP4 with hard cuts and storyboard order is created.

### 16.8 Payments and invoices

1. Given a successful browser return without a trusted webhook, when the status
   page loads, then payment remains Pending and wallet credit is unchanged.
2. Given a valid success webhook, when it is processed repeatedly, then exactly
   one credit transaction and invoice are created.
3. Given out-of-order provider events, when they are processed, then a terminal
   successful payment cannot be incorrectly reverted to an earlier pending
   state.
4. Given a refund request, when unused attributable credit is insufficient,
   then the refund is limited or rejected without making the wallet negative.
5. Given a chargeback after credit was spent, when it is processed, then a
   separate receivable and account suspension are created while wallet balance
   remains non-negative.
6. Given a refund enters provider-pending, when another job requests the held
   lot amount, then that amount is unavailable for reservation.
7. Given a provider rejects a refund, when failure is reconciled, then the hold
   is released and posted wallet balance remains unchanged.
8. Given a successful payment webhook in a currency different from the account's
   provisional or fixed currency, when it is processed, then no wallet credit is
   posted and the payment enters reconciliation.

### 16.9 Expiration and deletion

1. Given an asset seven days from expiration, when the retention check runs,
   then one in-app warning is available with the exact expiry time.
2. Given a referenced expired asset, when the project opens, then a metadata
   placeholder remains and preview, dependent generation, and export are
   blocked.
3. Given a trashed project inside seven days, when Restore is selected, then the
   project returns without changing retained project versions or assets.
4. Given a project with active jobs, when moving it to Trash is confirmed, then
   new work is blocked, active jobs are cancelled and settled, and the seven-day
   timer starts only after all allocations are terminal.
5. Given account deletion with active jobs or pending provider operations, when
   deletion is requested, then normal product access ends and a restricted
   settlement session shows progress without permitting new activity.
6. Given settlement produces refundable credit, when deletion continues, then
   final confirmation remains disabled until every refund succeeds or receives
   documented resolution.
7. Given all amounts are final, when non-refundable residual credit exists, then
   the exact amount must be explicitly relinquished before final confirmation.
8. Given final deletion confirmation, when it succeeds, then every remaining
   session is revoked immediately and no recovery grace period exists.
9. Given final deletion confirmation, when 24 hours have passed, then active
   product storage and derivatives are removed while only required locked
   records and inaccessible aging backups remain.

### 16.10 Realtime and notifications

1. Given a dropped SSE connection shorter than the replay window, when the
   client reconnects with `Last-Event-ID`, then missed events are replayed and
   duplicates cause no duplicate UI action.
2. Given a reconnect after the 24-hour replay window, when replay is rejected,
   then `stream.reset_required` is emitted, the stream closes, and the client
   performs a full authoritative REST refresh before reconnecting without an
   event ID.
3. Given prolonged SSE unavailability, when active work exists, then polling
   keeps job and wallet state current until streaming recovers.
4. Given a notification read on one device, when another device refreshes or
   receives the event, then it also shows the notification as read.
5. Given a job, expiry, payment, or support event, when notification is created,
   then no email is sent unless the event independently qualifies as account
   security.
6. Given an actionable notification becomes resolved, when 90 days pass from
   `resolved_at`, then it expires regardless of read state; an unresolved
   notification does not silently expire.

### 16.11 Responsive, accessibility, and performance

1. Given a 320 CSS-pixel viewport, when any user flow is completed, then no
   required action is unavailable and the page has no horizontal page scroll.
2. Given keyboard-only input, when a project is edited and scenes are reordered,
   then every operation is possible with visible focus and logical order.
3. Given a screen reader, when job state changes, then meaningful state is
   announced without announcing every raw progress event.
4. Given reduced-motion preference, when sheets, dialogs, or state changes
   occur, then non-essential motion is removed.
5. Given light or dark system preference, when no account override exists, then
   the matching complete theme is used without an incorrect-theme flash.
6. Given supported production traffic at the 75th percentile, then core pages
   meet LCP <= 2.5 seconds, INP <= 200 milliseconds, and CLS <= 0.1.

### 16.12 Administration and privacy

1. Given a normal admin session, when private prompt or media content is opened,
   then access is denied without a valid scoped grant or qualified break-glass
   flow.
2. Given user consent, admin step-up, and a ticket reason, when support access is
   granted, then it expires within 30 minutes and every access is audited.
3. Given a large manual wallet correction, when only its creator has approved
   it, then no ledger entry is posted until a second admin approves.
4. Given a price version is published, when an older valid quote is accepted,
   then the quote retains its original price until its own expiry.
5. Given a template update, when it is published, then existing projects remain
   unchanged.
6. Given first-party analytics collection, when events are inspected, then they
   contain no prompts, media, sensitive free text, or cross-site identifiers.
7. Given first use of prompt improvement, when the action is requested, then the
   UI discloses that the target prompt, project bible, and complete storyboard
   are sent to the contracted processor before submission.
8. Given a processor lacks purpose limitation, no-training terms, bounded
   retention, or permission for the product content category, when provider
   configuration is validated, then it cannot be activated.

## 17. Open Decisions

Implementation can proceed around these items, but release requires explicit
resolution:

- Product name, logo, and final brand assets.
- Exact EUR and USD image prices.
- Exact per-duration EUR and USD video prices.
- Top-up package values and custom minimum and maximum.
- Adult-content-compatible payment provider and supported payment methods.
- Legal and product requirements for age verification.
- Frontend framework and deployment architecture.
- Exact legal retention periods beyond the specified 24-month admin-audit
  minimum.

The content rules regarding people shown in uploaded or generated media are
deliberately outside this specification and must not be inferred from it.
