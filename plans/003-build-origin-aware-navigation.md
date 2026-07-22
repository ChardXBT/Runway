# 003 — Build an origin-aware navigation shell

- **Status**: DONE
- **Commit**: 5e0174e
- **Severity**: MEDIUM
- **Category**: Spatial consistency; physicality and origin
- **Estimated scope**: 3 files, medium navigation redesign

## Problem

The secondary navigation is hidden behind a generic circular menu. The popover appears
without a spatial transition, while desktop users repeatedly pay the cost of opening it.

```tsx
// apps/web/components/nav.tsx:90 — current
<details className="nav-menu">
  <summary aria-label="Open Runway menu">...</summary>
  <div className="nav-menu-popover">...</div>
</details>
```

## Target

- Desktop: persistent 240px product rail; no open/close animation for frequent links.
- Mobile: compact top bar plus primary bottom navigation; secondary menu enters from its
  trigger with `opacity` and `transform` over `220ms var(--ease-drawer)`.
- Press feedback: `transform: scale(0.97)` over `120ms var(--ease-out)`.
- Reduced motion: opacity-only 160ms transition.

## Repo conventions to follow

- Keep semantic links and the existing Escape/outside-click behavior.
- Preserve `aria-current` and the skip link.
- Use the existing Runway logo asset.

## Steps

1. Replace the green-dot status with a labeled state glyph and truthful publishing copy.
2. Recompose existing navigation markup for a desktop rail and mobile sheet.
3. Add route icons with `currentColor` and visible labels; do not rely on color alone.
4. Restore focus to the menu trigger on Escape and route selection.

## Boundaries

- Do not change route URLs.
- Do not add a UI component dependency.
- Do not animate keyboard-triggered navigation.

## Verification

- **Mechanical**: update and run `nav.test.tsx`.
- **Feel check**: open/close the mobile sheet rapidly, press Escape, and confirm it grows
  from the trigger without blocking input. Check 390px and 820px widths.
- **Done when**: all routes are directly discoverable on desktop and keyboard focus is
  restored correctly on mobile.
