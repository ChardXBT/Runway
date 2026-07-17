# Build status

## Checkpoints

- [x] Milestone 0 — foundation
- [x] Milestone 1 — capture and catalogue
- [x] Milestone 2 — intelligence and retrieval
- [x] Milestone 3 — discovery, ranking, and captions
- [x] Milestone 4 — approval queue and internal publisher
- [x] Fixture end-to-end proof
- [x] Full automated checks
- [x] Private GitHub publication
- [x] ChatGPT-authenticated Codex image/caption runtime with no paid API fallback
- [x] Fan-art and personal-artwork discovery safeguards

## Current failures

- None in the implemented offline workflow. The real-account acceptance tests below have not yet
  been run.

## Remaining work

- Perform and verify the real read-only Qlob capture.
- Build and evaluate the real Qlob retrieval profile.
- Exercise live image discovery and review provenance/rights behavior.
- Test the review UI with real candidates and captions.
- Implement the guarded YouTube browser publisher.
- Manually prove the Editor (Limited) delegate can open the post composer and schedule a post.
- Schedule a private/unlisted test where possible, verify it on YouTube, and test the recovery path.

The exact order, prerequisites, and completion criteria are in `docs/COMPLETION_GUIDE.md`.
