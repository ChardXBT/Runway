# RunWay interface system

This is the source of truth for RunWay product-interface work. The implementation lives in
`apps/web/app/runway.css`.

## Product intent

RunWay is a private editorial desk for one Qlob operator making many fast image-and-caption
decisions. It should feel like a calm fashion editor’s fitting room: decisive, image-led,
prestigious, and unburdened by operational detail.

The two primary rooms are:

- **Generator** — one current image/caption pair and three decisions: Reject, Edit, Accept.
- **Lineup** — the complete release calendar, with confirmed modify, move/swap, and remove actions.

Archive, Profile, Activity, and Settings are backstage utilities inside the menu. They must never
compete with the two primary rooms.

Domain vocabulary: runway, look, line, lineup, release slot, next opening, backstage, fit, pull,
accept, and Qlob.

Avoid generic dashboard cards, analytics walls, creator-economy language, rights gates in the
decision flow, fixed scheduling horizons, decorative gradients, glass effects, and rainbow color.

## Component intent checkpoint

- **Intent:** make repeated judgment fast while keeping publishing changes explicit and verifiable.
- **Hierarchy:** current image → caption → Reject/Edit/Accept → secondary context.
- **Palette:** carbon, warm paper, and white dominate; amber, dusk blue, green, and red carry meaning.
- **Depth:** borders and surface shifts only, plus one restrained floating menu/dialog shadow.
- **Surfaces:** black screening stage, paper decision sheet, compact calendar cells.
- **Typography:** narrow editorial display face, readable system body, monospaced operational data.
- **Spacing:** 4px base; compact controls, generous media, no oversized empty hero space.

## Signature

The mark is an arch over a converging runway with a broken amber centerline and two restrained
runway lights. The screening stage repeats the centerline at very low contrast. This is the one
recognizable visual motif; do not add competing decoration.

## Color tokens

Use semantic tokens in `runway.css`; never add route-local hex values.

- `--console-deep`, `--console`, `--console-raised`: carbon screening room.
- `--canvas`, `--surface`, `--surface-raised`, `--surface-inset`: warm paper stack.
- `--ink`, `--ink-secondary`, `--ink-tertiary`: text hierarchy.
- `--runway-light`: restrained amber centerline and swap notice.
- `--runway-dusk`, `--cue-*`: selection and editing.
- `--approval-*`: Accept, verified, and ready states.
- `--safety-*`: Reject, remove, and destructive states.
- `--warning-*`: paused, ambiguous, and attention states.

Most pixels must remain neutral. Green and red are reserved for the two judgment poles.

## Typography

- Display and decisions: `Bahnschrift`, then `DIN Alternate` or `Arial Narrow`.
- Reading and controls: `Aptos`, then `Avenir Next` or `Segoe UI`.
- Dates, slots, IDs, and counters: `Cascadia Code`, then SF Mono or Consolas.
- Dynamic numbers use tabular figures.
- Use weight and color before introducing another type size.

## Layout contracts

### Global navigation

- Carbon top bar with the RunWay arch/runway mark.
- Only Generator and Lineup are visible as primary links.
- Hamburger menu contains Archive, Profile, Activity, Settings, and honest YouTube state.
- At narrow widths, mark, two links, and menu remain on one row.

### Generator

- Desktop: media stage and decision sheet share one contained focal surface.
- Mobile/tablet: media first, decision sheet second, with no horizontal overflow.
- Caption accepts and preserves ordinary Unicode and punctuation, including `!`, `?`, and emoji.
- The primary command row is always Reject (red), Edit (white/dusk), Accept (green).
- Replacement-image and regenerated-caption controls stay secondary but visible.
- Accept saves positive image/caption feedback, assigns the first open daily slot, queues YouTube
  publishing, and immediately advances.
- Reject saves negative evidence and immediately advances.
- Alternatives and model rationale remain collapsed secondary material.

### Lineup

- Desktop uses a month calendar, a visible upcoming-post list, and a selected-post inspector.
- Below 820px, the month grid becomes a chronological agenda.
- Modify can change caption and date in one confirmation.
- Moving onto an occupied date swaps the two posts; it never creates a daily collision.
- Remove requires a second explicit confirmation.
- Externally scheduled changes are applied to YouTube first and committed locally only after
  verification. In-flight or unverified posts refuse mutation.
- Internal queued changes supersede stale payloads before replacement payloads are queued.
- Waiting, failed, and unverified YouTube actions expose explicit retry or verify controls.

### Backstage

- Archive combines captured published history with rejected Generator options.
- Settings owns an explicit Platform Connection panel for status checks and queue recovery.

## Behavior and safeguards

- At most one RunWay-created Qlob post occupies a Toronto local date, at 10:00 AM.
- There is no fixed horizon or session-size cap.
- Manual Qlob posts do not consume RunWay slots.
- YouTube browser work is serialized and uses the dedicated persistent profile.
- Google challenges are never bypassed.
- Codex uses saved ChatGPT authentication; API keys are stripped and no paid fallback exists.
- Ambiguous external submissions stop and remain inspectable; they are never blindly retried.
- Candidate images must match a supported franchise from the active Qlob profile; visually similar
  but unrelated photographs are rejected before Generator.
- Artist portfolio domains are rejected even when automated fan-art confidence is low.
- Every consequential mutation creates proposal and audit history.

## Accessibility and motion

- Native links, buttons, labels, details, date fields, and textareas are mandatory.
- Interactive targets are at least 40px, normally 44–58px.
- Every control has a visible focus state and meaningful accessible name.
- Dialogs use `role="dialog"` and `aria-modal`; Escape/backdrop behavior must not interrupt work.
- Repeated decisions do not animate. Feedback is limited to 100–160ms color/press transitions.
- Respect `prefers-reduced-motion`.
- Verify desktop, tablet, and 390px widths with no horizontal overflow.

## Acceptance checks

1. The squint test yields image, caption, then three decisions.
2. Runway and Lineup are the only visible primary destinations.
3. The logo reads as an arch and runway in monochrome before its two small color cues.
4. `Wait... what?!` survives edit, API, persistence, and publishing payload unchanged.
5. Acceptance advances immediately and never creates two RunWay slots on one day.
6. Lineup changes require confirmation and stale queued payloads cannot publish.
7. External edit/remove failures leave local state unchanged.
8. The interface is complete at 390px without horizontal scrolling.
9. Visual QA never clicks Accept or confirms a Lineup mutation against production data.
