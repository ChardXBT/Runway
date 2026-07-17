# Intelligence and retrieval

Caption statistics, hashes, image descriptors, date arithmetic, queue rules, and weighted ranking
are deterministic. Typed agent runtimes are limited to semantic annotations, style prose, search
planning, candidate semantics, and caption options.

The active profile contains calculated distributions plus cited examples. Retrieval returns at
most eight visual examples, eight caption examples, five negative examples, recent exclusions,
rotation state, and the profile version. The full catalogue is never sent to a model.

Historical annotations, candidate analysis, and caption generation include the actual local image,
not only image metadata. Caption generation produces a nine-item pool: four open questions, three
observations, and two reactions. When character and visible emotion are confidently known, LeeWay
injects a grounded question such as `Why is Homer so excited?`.

A deterministic reranker rewards open-ended question structure, Qlob style fit, length, novelty,
and similarity to positive human feedback. It penalizes yes/no questions, generic engagement bait,
flat emotion descriptions, recent/historical duplication, and similarity to rejected captions.
The final review set always starts with an open question and retains one observation and one
reaction alternative.

Edits, selected alternatives, approvals, explicit preferences, and rejections are append-only
`caption_feedback` records with reason codes and optional image verdicts/notes. Relevant positive
and negative examples are added to the next candidate's bounded retrieval context immediately.
Qlob examples therefore tune behavior without modifying Codex model weights or requiring a
fine-tuning bill.

The synthetic duplicate test set covers exact identity, resize/recompression, center crop, color
adjustment, added border, and an unrelated checkerboard. The measured baseline uses perceptual
similarity `>= 0.88` or local-embedding cosine similarity `>= 0.99` for transformed duplicate
classification; production thresholds remain configurable and evaluation reports both recall and
unrelated false-positive rate.
