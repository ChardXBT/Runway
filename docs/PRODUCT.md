# Product

Runway is a local editorial conveyor for Qlob YouTube Community posts. It uses the captured Qlob
archive as its primary style reference, retrieves relevant positive and negative feedback, finds
candidate images, writes question-first captions, and presents one decision at a time.

## Primary workflow

1. Review the current image and caption.
2. Edit the caption inline when needed.
3. Choose `Reject`, open `Edit` when needed, or `Accept`.
4. Runway records the decision and immediately presents the next option.
5. Every accepted option receives the first open default Lineup slot. Acceptance creates no
   YouTube queue work and opens no browser.
6. Edit the post's date and time in the channel timezone, then use the explicit Lineup action to
   prepare an assisted workspace or queue separately authorized browser handling.

There is no session-size or ten-day horizon cap. Runway reserves at most one bot-created slot per
local day; manually created YouTube posts are independent. The practical horizon ends only when
the operator stops approving. The one-post-per-local-day rule is a Runway product policy, not a
YouTube limitation.

Edits, approvals, alternatives, image replacements, and rejections become append-only learning
signals. They affect later retrieval and ranking immediately; model-weight fine-tuning is optional
future work, not a prerequisite for improvement.

Source metadata and automated image warnings remain stored for diagnostics. They are not part of
the normal approval form and no provenance checkbox is required to approve. Images explicitly
blocked by the safeguards cannot be scheduled.

## Scope

Runway is single-user and pinned to the Qlob channel. It includes the local archive, style profile,
candidate discovery, caption generation, review conveyor, uncapped daily scheduler, default
assisted publishing workspace, optional persisted browser-publisher outbox, archive, settings, and
activity history.

Comments, moderation, credential export, hidden browser automation, automatic Google challenge
bypass, and more than one Runway-generated post per day are out of scope.
