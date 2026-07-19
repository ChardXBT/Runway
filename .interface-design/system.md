# Runway interface system

This is the source of truth for Runway product-interface work. The implementation lives in
`apps/web/app/runway.css`.

## Product intent

Runway is a private editorial workroom for channel teams making fast image, caption, scheduling,
and connection decisions. It should feel like a contemporary fashion desk: image-led, sharp,
confident, tactile, and calm under repetition.

The three primary rooms are:

- **Generator** — one current image/caption pair and Reject, Edit, Accept.
- **Lineup** — the complete release calendar and verified post controls.
- **Connector** — invite the Runway account, understand least-privilege access, and verify it.

Archive, Profile, Activity, and Settings remain backstage utilities.

Domain vocabulary: look, edit, lineup, release slot, backstage, fitting, invite, access check,
accept, and channel.

Avoid road and airport imagery, arches, lane markings, generic dashboard card grids, analytics
walls, glass effects, decorative gradients, and rainbow UI. Color belongs primarily to the mark,
selection, status, and actions.

## Component intent checkpoint

- **Intent:** help a channel operator make decisions and establish access without ambiguity.
- **Hierarchy:** current image → caption → decision; invitation account → setup steps → verification.
- **Palette:** ink black, warm paper, and white dominate; cobalt acts; coral warns; orchid signals
  a pending connector state.
- **Depth:** white editorial sheets use a restrained two-layer shadow; dark focal stages and
  popovers lift slightly more.
- **Surfaces:** warm paper canvas, white work sheets, black screening and verification stages.
- **Typography:** narrow Bahnschrift display, Aptos body, Cascadia Code utility labels.
- **Spacing:** 4px base; 44–58px controls; dense metadata and generous image/decision surfaces.

## Brand signature

The generated mark is an abstract folded `R` made from black, cobalt, coral, and orchid fabric-like
ribbons. It suggests styling and forward motion without depicting a road. Use
`apps/web/public/runway-logo-512.png` in product chrome and `apps/web/app/icon.png` for app identity.

The slogan is always written exactly as:

> Your fans can't wait

Eyebrows may echo the mark with three flat color ticks. Do not add other decorative motifs.

## Color tokens

Use semantic tokens in `runway.css`; never add route-local color values.

- `--console-*`: ink-black focal surfaces.
- `--canvas`, `--surface*`: warm paper stack.
- `--ink*`: four-level text hierarchy.
- `--cue*` and `--runway-dusk`: electric cobalt selection and primary action.
- `--runway-light` and `--safety*`: coral rejection, destructive, and attention states.
- `--warning*`: orchid pending/paused states.

The multicolor palette is concentrated in the logo and small signals. Most pixels stay neutral.

## Layout contracts

### Global navigation

- Warm-paper top bar with the folded-ribbon mark, Runway wordmark, and slogan.
- Generator, Lineup, and Connector are the visible primary routes.
- Hamburger menu contains Archive, Profile, Activity, Settings, and honest YouTube state.
- At narrow widths, the wordmark collapses before navigation labels do.
- Next.js development indicators remain disabled; no corner `N` may cover product UI.

### Generator

- Desktop: black media stage and white decision sheet form one focal surface.
- Mobile/tablet: media first, decision sheet second, without horizontal overflow.
- The primary row stays Reject, Edit, Accept. Reject is coral outline, Edit is black, Accept is
  cobalt.
- Caption punctuation and Unicode are preserved exactly.
- Generation tools and model context stay visibly secondary.

### Lineup

- Desktop uses a month calendar, upcoming-post list, and selected-post inspector.
- Below 820px, the calendar becomes a chronological agenda.
- Move, modify, remove, and external verification safeguards remain explicit.
- Selected posts use the same cobalt seam as the global active state.

### Connector

- The invitation email is a dark, copyable pass and must show `tryrunwaytoday@gmail.com`.
- Instructions follow YouTube Studio → Settings → Permissions → Invite.
- Recommend Editor (limited): it can create/publish posts without exposing revenue.
- The verification form accepts an `@handle`, YouTube channel URL, or `UC…` channel ID.
- Verification is read-only and must never create, edit, publish, or delete a post.
- Invalid/external URLs are rejected before the browser opens.
- Google challenges are never bypassed; the interface tells the operator to resolve them manually.

## Accessibility and motion

- Use native links, buttons, labels, details, inputs, dates, and textareas.
- Interactive targets are at least 40px, normally 44–58px.
- Every control has visible focus, hover, active, disabled, loading, and failure states.
- Repeated decisions do not animate; occasional feedback stays under 200ms.
- Respect `prefers-reduced-motion`.
- Verify desktop, tablet, and 390px widths with no horizontal overflow.

## Acceptance checks

1. The logo reads as a folded fashion ribbon and never as a road, airport runway, arch, or `N`.
2. The visible brand says `Runway` and `Your fans can't wait`.
3. Generator, Lineup, and Connector are the three primary destinations.
4. The Generator squint test yields image, caption, then three decisions.
5. Connector exposes the exact invite account and a working read-only access check.
6. `Wait... what?!` survives edit, API, persistence, and publishing unchanged.
7. The interface is complete at 390px without horizontal scrolling.
8. Visual QA never clicks Accept or confirms a Lineup mutation against production data.
