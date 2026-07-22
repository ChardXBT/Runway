# 004 — Make pending work visibly alive

- **Status**: DONE
- **Commit**: 5e0174e
- **Severity**: MEDIUM
- **Category**: Feedback; perceived performance; accessibility
- **Estimated scope**: 4 files, medium state-system change

## Problem

Route loading is a static centered card, and long client actions often change only the
button label. When local generation or a browser check takes time, the interface can
look frozen even though work continues.

```tsx
// apps/web/app/loading.tsx:4 — current
<section className="route-loading" aria-busy="true">
  <RunwayLogo className="route-loading-mark" />
  <h1>Preparing the desk.</h1>
</section>
```

## Target

- Route loading uses a structural skeleton and progress rail immediately.
- After five seconds, copy changes to a specific “still working” message while navigation
  remains available.
- Every `[aria-busy="true"]` control gets a compact linear activity indicator.
- Generator work retains its truthful poll-based status.
- Reduced motion keeps opacity/state feedback and removes translation/shimmer movement.

## Repo conventions to follow

- Existing client action locks prevent duplicate requests.
- Existing `role="status"` / `role="alert"` regions remain the source of truth.

## Steps

1. Make `loading.tsx` a small client component with delayed guidance.
2. Add reusable loading skeleton and busy-control styles.
3. Give Generator, Connector, Lineup, Settings, and annotation actions consistent busy
   feedback without changing their requests.
4. Ensure pending state does not disable navigation.

## Boundaries

- Do not invent progress percentages.
- Do not change API timing, polling, or backend behavior.
- Do not use looping decorative motion outside active loading states.

## Verification

- **Mechanical**: run component tests and production build.
- **Feel check**: throttle a route and a client request; confirm acknowledgment is
  immediate, the delayed message appears, and reduced motion removes movement.
- **Done when**: no long-running route or action can appear silently frozen.
