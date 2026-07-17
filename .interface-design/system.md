# LeeWay interface system

This file is the source of truth for LeeWay product-interface work. The implementation lives in
`apps/web/app/studio.css`.

## Direction and feel

LeeWay is a private editorial conveyor for one Qlob operator. The operator opens the app to make
many fast calls: approve the image/caption pair, change the caption, request another image, or
reject the option. The next option should appear immediately while learning and scheduling happen
quietly behind the decision.

The interface should feel decisive, image-led, prestigious, and calm—an editor’s private screening
room rather than an admin dashboard. It is not a ten-day planner, analytics wall, rights-review
form, generic SaaS dashboard, or creator-economy landing page.

Domain vocabulary:

- editorial tray
- screening room
- final cut
- approve and advance
- daily slot
- outbound queue
- preference signal
- continuous feed

The signature is the editorial conveyor: one large feed preview beside one authoritative caption,
followed by `Reject`, `Another image`, and `Approve & schedule`. Every decision advances the tray.

Reject these defaults:

- Dashboard metrics and planning cards → a compact session counter and one current decision.
- A duplicate original/crop comparison → one large YouTube feed preview.
- Training forms and checkboxes → infer learning from edits and decisions automatically.
- Provenance and copyright gates → keep source metadata in storage, outside the approval path.
- Fixed calendar horizons → show the next open daily slot and an uncapped ordered schedule.
- Multi-step publishing interlocks → the explicit `Approve & schedule` click is the human action.

## Palette and tokens

Use semantic variables from `studio.css`; do not introduce route-level color values.

- `--lightbox`, `--canvas`: cool screening-room daylight.
- `--surface`, `--surface-raised`, `--surface-inset`: paperless editorial surfaces.
- `--console-deep`, `--console`, `--console-raised`: screening-room media stage.
- `--ink`, `--ink-secondary`, `--ink-tertiary`: four-level text hierarchy.
- `--cue`, `--cue-deep`, `--cue-soft`: selection and navigation.
- `--approval`, `--approval-deep`, `--approval-soft`: approve-and-schedule.
- `--safety`, `--safety-soft`: rejection and destructive actions.
- `--warning`, `--warning-soft`: safely paused publisher state.

Color communicates state. Most of the interface remains neutral.

## Typography

- Display and decisions: `Bahnschrift`, then `DIN Alternate` or `Arial Narrow`.
- Reading and controls: `Aptos`, then `Avenir Next` or `Segoe UI`.
- IDs, slots, and counters: `Cascadia Code`, then SF Mono or Consolas.
- Body baseline: 15px/1.5.
- Eyebrows: 10px utility face, uppercase, 0.11em tracking.
- Review headline: responsive 42–68px.
- Caption editor: responsive 22–30px display face.
- Dynamic numbers use tabular figures.

Use weight and text color before adding another size.

## Spacing, radius, and depth

- Base unit: 4px.
- Micro: 4–8px.
- Controls: 8–16px.
- Decision console: 24–48px.
- Main canvas: responsive 28–56px; 14px at the narrowest breakpoint.
- Controls: 7px radius and at least 44px high.
- Panels: 10px radius.
- Decision stage: 14px radius.

Depth uses surface-color shifts and quiet low-opacity borders. No gradients, glass, decorative
shadows, or mixed elevation systems.

## Layout and hierarchy

Every screen has one focal point:

- Review: current image/caption decision.
- Schedule: the complete ordered list of LeeWay posts.
- Archive: searchable historical material.
- Settings: the few rules that govern the conveyor.

Review is a two-column screening stage at desktop widths and a single image-then-decision flow
below 1060px. The image remains the largest element. The three decision controls form one command
row; `Approve & schedule` carries the strongest contrast.

The global rail contains only Review, Schedule, Archive, and Settings. Profile and Activity remain
available as secondary routes but do not compete in primary navigation.

## Behavior contracts

- Approval captures positive caption/image feedback.
- A changed caption captures the original as negative evidence and the edit as preferred evidence.
- Rejection captures a negative option signal.
- `Another image` captures a negative image signal before replacement.
- Approval assigns the first open 10:00 AM `America/Toronto` slot.
- LeeWay reserves at most one bot post per local date; manual Qlob posts are independent.
- There is no scheduling-horizon cap.
- Approved posts enter a persisted FIFO publisher outbox and browser submissions run serially.
- A publisher/session failure pauses the outbox without duplicating submissions.
- The next review option appears immediately; when the tray is empty, LeeWay replenishes accepted
  candidates and can open a visible discovery pass.

## Reusable patterns

### Global rail

- Compact sticky rail with split blue/graphite `LW` mark.
- Primary links: Review, Schedule, Archive, Settings.
- Publisher state reads `Auto-schedule on · One bot post daily` when enabled.

### Editorial conveyor

- Session counters: decisions this session, posts on the way, externally scheduled.
- Large square feed preview on a graphite media stage.
- Slot cue shows the next available Eastern date at 10:00 AM.
- Caption is directly editable; no separate save button.
- Alternatives are compact selectable rows.
- Commands: Reject, Another image, Approve & schedule.
- Model rationale is collapsed in native `details`.
- No copyright, rights, source, score, or training form in the primary decision.

### Schedule

- No denominator or horizon language.
- Ordered rows show slot, thumbnail, caption, and scheduling state.
- Header shows total queued/scheduled posts and the next open slot.

## Motion and accessibility

- Repeated editorial decisions do not animate; speed wins.
- Controls use 100–160ms color/press feedback and `scale(0.97)` on active.
- Never use `transition: all`.
- Respect `prefers-reduced-motion`.
- Maintain visible focus, semantic native controls, and 44px hit areas.
- Verify at normal desktop width and 390px mobile with no horizontal overflow.

## Acceptance checks

1. The current image/caption pair wins the squint test.
2. Approve is the single strongest action.
3. No source/copyright gate appears before approval.
4. The next option replaces the current one after every decision.
5. Schedule language is uncapped and explicitly one bot post per day.
6. The page remains usable at 390px without horizontal overflow.
7. Visual QA never clicks `Approve & schedule` against production data.
