# Product

RunWay is a local editorial conveyor for Qlob YouTube Community posts. It uses the captured Qlob
archive as its primary style reference, retrieves relevant positive and negative feedback, finds
candidate images, writes question-first captions, and presents one decision at a time.

## Primary workflow

1. Review the current image and caption.
2. Edit the caption inline when needed.
3. Choose `Reject`, open `Edit` when needed, or `Accept`.
4. RunWay records the decision and immediately presents the next option.
5. Every accepted option receives the first open 10:00 AM `America/Toronto` slot and enters the persisted
   YouTube publisher queue.

There is no session-size or ten-day horizon cap. RunWay reserves at most one bot-created slot per
local day; manually created YouTube posts are independent. The practical horizon ends only when
YouTube refuses a future date or the operator stops approving.

Edits, approvals, alternatives, image replacements, and rejections become append-only learning
signals. They affect later retrieval and ranking immediately; model-weight fine-tuning is optional
future work, not a prerequisite for improvement.

Source metadata and automated image warnings remain stored for diagnostics. They are not part of
the normal approval form and no provenance checkbox is required to approve. Images explicitly
blocked by the safeguards cannot be scheduled.

## Scope

RunWay is single-user and pinned to the Qlob channel. It includes the local archive, style profile,
candidate discovery, caption generation, review conveyor, uncapped daily scheduler, persisted
publisher outbox, archive, settings, activity history, and visible-browser YouTube integration.

Comments, moderation, credential export, hidden browser automation, automatic Google challenge
bypass, and more than one RunWay-generated post per day are out of scope.
