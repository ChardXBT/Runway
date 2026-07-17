# Intelligence and retrieval

Caption statistics, hashes, image descriptors, date arithmetic, queue rules, and weighted ranking
are deterministic. Typed agent runtimes are limited to semantic annotations, style prose, search
planning, candidate semantics, and caption options.

The active profile contains calculated distributions plus cited examples. Retrieval returns at
most eight visual examples, eight caption examples, five negative examples, recent exclusions,
rotation state, and the profile version. The full catalogue is never sent to a model.

Historical annotations, candidate analysis, and caption generation include the actual local image,
not only image metadata. Qlob examples tune behavior through retrieval and profile rebuilding; the
underlying Codex model weights are not fine-tuned.

The synthetic duplicate test set covers exact identity, resize/recompression, center crop, color
adjustment, added border, and an unrelated checkerboard. The measured baseline uses perceptual
similarity `>= 0.88` or local-embedding cosine similarity `>= 0.99` for transformed duplicate
classification; production thresholds remain configurable and evaluation reports both recall and
unrelated false-positive rate.
