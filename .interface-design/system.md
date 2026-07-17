# LeeWay interface system

This file is the source of truth for future LeeWay product-interface work. Apply it before
changing shared styles or building a new screen. The implementation lives in
`apps/web/app/studio.css`.

## Direction and feel

LeeWay is a single-operator editorial desk for Qlob YouTube Community posts. The operator has just
captured, searched, or generated material and needs to compare an image, shape its caption, inspect
evidence and provenance, and move a ten-day local queue forward without accidentally publishing.

The interface should feel like a focused editing suite and contact sheet: cool, precise,
image-led, and calm under dense information. It is not a marketing site, creator-economy landing
page, generic SaaS dashboard, newspaper, or warm lifestyle workspace.

Domain vocabulary:

- editorial desk
- light table
- contact sheet
- frame and square crop
- caption strip
- source record and provenance
- ten-day runway
- approval decision
- immutable audit
- publishing interlock

The signature is the ten-frame runway. It turns proposed posts into an editorial contact sheet
with ordered day indices, square image frames, captions, and explicit gap states. Preserve this
concept on Dashboard and Queue.

Reject these defaults:

- Oversized colored sidebar: use the compact sticky control rail.
- Four interchangeable floating metric cards: use a connected console/status band.
- Cream paper, decorative serif, gradients, and soft lifestyle styling: use the cool light-table
  palette and production typography.
- Decorative color: blue means cue/action, green means approval, red means publishing lock or a
  destructive action, and amber means uncertainty.
- Hidden provenance or model confidence: keep evidence adjacent to decisions.

## Palette and tokens

Use semantic variables from `studio.css`; do not introduce route-level hex values.

| Role | Token | Value |
| --- | --- | --- |
| Light-table background | `--lightbox` | `#f7f8fa` |
| Application canvas | `--canvas` | `#e9ecf2` |
| Primary surface | `--surface` | `#ffffff` |
| Raised surface | `--surface-raised` | `#fbfcfe` |
| Inset control | `--surface-inset` | `#e4e8ef` |
| Media/console | `--console` | `#171a21` |
| Raised console | `--console-raised` | `#222630` |
| Primary ink | `--ink` | `#171a21` |
| Supporting ink | `--ink-secondary` | `#505766` |
| Metadata ink | `--ink-tertiary` | `#777f8f` |
| Cue/action | `--cue` | `#2f5fe3` |
| Cue emphasis | `--cue-deep` | `#2147b7` |
| Cue tint | `--cue-soft` | `#e4eaff` |
| Approval | `--approval` | `#16705a` |
| Safety/destructive | `--safety` | `#b23a48` |
| Warning/uncertainty | `--warning` | `#90620c` |

Use approximately 60% canvas/lightbox, 30% white or console surfaces, and 10% cue/semantic color.
Publishing safety red must remain visible in the global rail and must never be reused as decoration.

## Typography

- Display and data: `Bahnschrift`, then `DIN Alternate` or `Arial Narrow`.
- Reading and controls: `Aptos`, then `Avenir Next` or `Segoe UI`.
- IDs, timestamps, indices, and audit metadata: `Cascadia Code`, then SF Mono or Consolas.
- Body baseline: 15px/1.5.
- Eyebrows and route breadcrumbs: 10px utility face, uppercase, 0.11em tracking.
- H1: responsive 36–64px, weight 650, 0.98 line-height, balanced wrapping.
- H2: 24px/1.15, weight 610.
- Dynamic numbers always use tabular figures.

Use weight and ink level before adding another font size. Primary content uses `--ink`, supporting
copy uses `--ink-secondary`, metadata uses `--ink-tertiary`, and disabled content uses opacity.

## Spacing, radius, and depth

- Base spacing unit: 4px.
- Micro gaps: 4–8px.
- Control and row gaps: 8–16px.
- Panel padding: 20–24px.
- Section separation: 28–48px.
- Main canvas padding: responsive 28–56px; 14px at the narrowest mobile breakpoint.
- Control radius: 7px.
- Panel radius: 10px.
- Media radius: 6px.
- Minimum interactive height: 44px; compact navigation may use 40px.

Depth strategy: surface-color shifts plus low-opacity borders. Do not add decorative drop shadows,
glass cards, gradients, or mixed elevation models. Media frames may use a one-pixel inset outline.
Inputs are inset and slightly darker than their parent surface.

## Layout and hierarchy

Every screen has one focal point:

- Desk: ten-day runway.
- Review: original image and authoritative final caption.
- Queue: ordered day rows and gaps.
- Archive: searchable image grid.
- Archive detail: source media, then source facts and reviewed annotation.
- Profile: calculated style summary and measured holdout evidence.
- Settings: safety state, then editable local configuration.
- Activity: ordered immutable events with collapsed payloads.

Use a maximum 1600px page frame. The control rail is sticky and compact. At normal desktop width,
Review uses an image-led two-column composition; at narrow widths it becomes a single decision
flow. Do not create a permanent sidebar.

Responsive breakpoints:

- 1320px: reduce dense desktop grids.
- 1060px: collapse dashboard/detail/review two-column compositions where necessary.
- 820px: stack headers, use two-column metric bands, and make navigation horizontally scrollable.
- 560px: single-column forms and archive cards while keeping status metrics two-up.
- Always verify at a normal desktop viewport and 390px mobile width with no document-level
  horizontal overflow.

## Reusable component patterns

### Global control rail

- 68px minimum desktop height.
- Split blue/graphite `LW` mark.
- Active route uses cue text on `--cue-soft`.
- Persistent publishing interlock on desktop; it reads `Publishing disabled`.
- Mobile navigation scrolls horizontally without widening the document.

### Buttons

- 44px minimum height, 17px horizontal padding, 7px radius, 13px/700.
- Primary: cue blue.
- Secondary: white with quiet structural border.
- Approve: approval green.
- Reject or destructive: safety red.
- Provide hover, focus-visible, active scale `0.97`, busy, and disabled states.
- Keep action labels literal: `Save caption`, `Approve`, `Reject`, `Build ten-day draft`.

### Panels and controls

- Panels use white surface, 1px quiet border, 10px radius, and 20–24px padding.
- Inputs use inset surface, 42px minimum height, 7px radius, and a cue-colored focus state.
- Prefer native button, link, input, select, textarea, details, and summary elements.
- Empty states explain what happened and identify the next useful action.

### Status console

- Connected graphite band rather than separate metric cards.
- The lead metric may use the cue background.
- Labels are small supporting text; values use Bahnschrift and tabular figures.
- Mobile uses a two-column grid with the lead metric spanning both columns.

### Ten-frame runway

- Five frames per desktop row, two per mobile row.
- Each frame contains a real sequence index, date, square media frame, caption, and proposal state.
- Empty days use an inset dashed frame and `Open day`; they are not decorative placeholders.
- Conflict uses a safety-red inset edge.

### Review workspace

- Original image remains the largest visual element.
- Square preview stays visible beside it on desktop and follows it on mobile.
- Caption editor uses the display face at 20px/1.45.
- Alternatives are secondary selectable rows.
- Grounding, confidence, reference IDs, factual uncertainty, provenance, rights status, and closest
  historical matches remain visible before secondary replacement/blocking controls.
- Never auto-approve, internally schedule, or publish during visual tests.

### Archive and audit

- Archive shows 60 records per page and uses a one-record look-ahead to avoid false Next pages.
- Cards are image-led and carry record ID, type, date precision, profile eligibility, caption, and
  source date.
- Raw URLs are separated from detail-page headlines.
- Audit rows expose event ID, timestamp, entity, and event name; payload JSON stays collapsed in
  native `details`.

## Motion and accessibility

- Motion is short and purposeful: 100–160ms controls, custom ease-out, transform/opacity only.
- Never use `transition: all`.
- Respect `prefers-reduced-motion`.
- Maintain visible keyboard focus and 44px primary hit areas.
- Keep headings balanced, body copy pretty-wrapped, and images outlined against their surfaces.
- Decorative images use empty alt text; decision-critical images use descriptive alt text.

## Signature checks

Before accepting future UI work, confirm these five LeeWay-specific signatures remain:

1. Split `LW` control-rail mark and persistent publishing interlock.
2. Connected graphite status console with cue lead metric.
3. Ten-frame indexed runway/contact sheet.
4. Original-versus-square-preview review stage with adjacent caption decision.
5. Utility-face record, queue, and audit indices that preserve provenance and sequence.

Also run:

- Swap test: replacing the contact-sheet runway, Bahnschrift, or control rail with generic
  dashboard patterns should materially weaken the product.
- Squint test: the current decision remains obvious and borders do not dominate.
- Token test: new colors and spacing map back to this file and `studio.css`.
- Browser test: every route at desktop plus Desk, Review, and Archive at 390px.
