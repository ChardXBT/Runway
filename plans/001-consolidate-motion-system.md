# 001 — Consolidate the visual and motion system

- **Status**: DONE
- **Commit**: 5e0174e
- **Severity**: HIGH
- **Category**: Performance; cohesion and tokens
- **Estimated scope**: 2 files, large stylesheet replacement

## Problem

`apps/web/app/runway.css` contains three accumulated design systems and repeated
definitions for the same navigation, button, panel, and responsive selectors. The
active token layer begins at line 6238, after more than six thousand lines of older
rules. The browser must parse and match all of them, while contributors cannot tell
which rule owns an interaction.

```css
/* apps/web/app/runway.css:3 and :6238 — competing roots */
:root { /* original tokens */ }
/* ... */
:root { /* release-signal tokens */ }
```

## Target

Replace the accumulated file with one product-interface system. Keep all motion in
shared tokens:

```css
:root {
  --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
  --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
  --ease-drawer: cubic-bezier(0.32, 0.72, 0, 1);
  --duration-press: 120ms;
  --duration-ui: 180ms;
  --duration-panel: 220ms;
}
```

The visual system uses one IBM Plex Sans product family, a cool neutral canvas,
raspberry primary actions, cobalt informational states, and explicit semantic status
colors. Interactive motion stays below 300ms and animates transform/opacity.

## Repo conventions to follow

- Global styles are imported once from `apps/web/app/layout.tsx`.
- Components use stable semantic class names rather than inline styles.
- Existing publishing and safety copy remains technically accurate.

## Steps

1. Replace `apps/web/app/runway.css` with one consolidated token and component layer.
2. Update `apps/web/app/layout.tsx` to self-host IBM Plex Sans through `next/font`.
3. Cover all current route and component class names at desktop, tablet, and mobile.
4. Add one complete reduced-motion and reduced-transparency policy.

## Boundaries

- Do not touch API contracts, backend code, ML, intelligence, database, or migrations.
- Do not add an animation runtime.
- Do not change publishing behavior.

## Verification

- **Mechanical**: `npm run lint`, `npm test`, and `npm run build` in `apps/web`.
- **Feel check**: inspect every route at 1440, 768, and 390 CSS pixels. Confirm hover
  feedback is pointer-gated and `prefers-reduced-motion` removes position movement.
- **Done when**: one stylesheet owns every active selector and no route is visually
  unstyled or horizontally clipped.
