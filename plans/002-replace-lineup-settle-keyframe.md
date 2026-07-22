# 002 — Make Lineup updates interruptible

- **Status**: DONE
- **Commit**: 5e0174e
- **Severity**: HIGH
- **Category**: Interruptibility; easing and duration
- **Estimated scope**: 1 file, small motion change

## Problem

The success state for a moved post restarts a 620ms keyframe whenever the class is
applied. It exceeds the UI motion budget and cannot retarget from its live value.

```css
/* apps/web/app/runway.css:8158 — current */
.lineup-day > button.lineup-settled {
  animation: lineup-settled 620ms cubic-bezier(0.2, 0.7, 0.2, 1);
}
```

## Target

Use the existing temporary state class as an interruptible transition target:

```css
.lineup-day > button {
  transition: transform 180ms var(--ease-out), opacity 180ms var(--ease-out);
}
.lineup-day > button.lineup-settled {
  transform: translateY(-2px);
}
```

The selected/success state may also change a static outline or background, but those
properties must not be part of a long animation.

## Repo conventions to follow

- `recentlyChangedIds` already removes the state after 700ms.
- Shared easing tokens live in the global stylesheet.

## Steps

1. Delete the `lineup-settled` keyframes.
2. Move the state change into the existing button transition.
3. In reduced motion, retain a color/outline confirmation without translation.

## Boundaries

- Do not change drag, swap, scheduling, or API behavior.
- Do not add dependencies.

## Verification

- **Mechanical**: run Lineup component tests.
- **Feel check**: move or swap a post, rapidly select another post, and confirm the
  state never restarts from zero. At 10% playback the update should settle cleanly.
- **Done when**: no dynamic Lineup keyframe remains and reduced motion has no movement.
