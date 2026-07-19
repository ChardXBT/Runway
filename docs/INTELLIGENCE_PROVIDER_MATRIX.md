# Runway Intelligence Provider Matrix

Updated: 2026-07-18

Runway’s only active default is the deterministic, offline `runway-local`
baseline. The optional adapters below require an explicit local model directory,
never download weights, never activate themselves, and are not used by automated
tests. “Unvalidated” means the adapter boundary is implemented but no Qlob-specific
frozen evaluation, latency study, or creator study has authorized activation.

## Selection matrix

| Capability | Candidate and version | License / commercial condition | Approximate size and output | Local resource profile | Strengths relevant to Runway | Material limitations and decision |
| --- | --- | --- | --- | --- | --- | --- |
| Multilingual text semantics | `Qwen/Qwen3-Embedding-0.6B` (pinned local revision required) | Apache-2.0 | 0.6B parameters; model card supports configurable embeddings up to 1024 dimensions | CPU-capable but expected to be slower than the deterministic baseline; batching supported; GPU optional | Current multilingual retrieval model, Sentence Transformers interface, instruction-aware | Adapter-compatible candidate, not downloaded or validated. Preferred first trained text experiment when hardware permits. |
| Multilingual dense/sparse/late-interaction text | `BAAI/bge-m3` (pinned local revision required) | MIT | 1024 dimensions, up to 8192 tokens | Larger CPU footprint; GPU helpful; batching supported | More than 100 languages; dense, sparse, and ColBERT-style outputs from one family | Dense adapter can use the generic Sentence Transformers boundary. Sparse and multi-vector modes require separate evaluation and storage work; inactive. |
| Image-text alignment / global visual semantics | `google/siglip2-base-patch16-224` (pinned local revision required) | Apache-2.0 | About 1.54 GB repository; aligned image/text feature space | CPU inference is possible but likely slow; GPU preferred for full backfill; batching supported | Image-text retrieval, improved semantic understanding, localization, and dense features | Implemented offline adapter for image, text, and composed average. Exact dimensions are read from output rather than assumed. Unvalidated and inactive. |
| Visual-only global and patch features | DINOv3 ViT-S/16 or ConvNeXt-Tiny, exact checkpoint TBD | Custom DINOv3 License; legal review required | Family ranges from small distilled backbones to 7B; class and patch tokens | Smaller variants may be CPU-capable; practical backfill likely GPU-assisted | Strong global and dense visual features, image nearest-neighbour retrieval | No native text alignment and custom license. Research candidate only; no adapter or activation. |
| Patch-level visual-document retrieval | `vidore/colqwen2.5-v0.2` / ColPali family, exact revision TBD | Model/backbone terms vary; verify exact card before use | Roughly 2B–4B for current ColQwen families; multi-vector patch outputs | GPU-oriented; high storage and retrieval cost | Strong token-to-patch late interaction for visually rich documents | Optimized for document pages rather than entertainment stills; likely poor cost/fit for Runway. No activation. |
| Cross-encoder reranking | `BAAI/bge-reranker-v2-m3` (pinned revision required) | Apache-2.0 | XLM-R-based multilingual cross-encoder | CPU possible on small candidate sets; GPU improves latency | Direct query/document relevance scoring; suitable after broad retrieval | Adds per-request latency and has no Runway evidence yet. Evaluate only on frozen labels. |
| Cross-encoder alternative | `jinaai/jina-reranker-v2-base-multilingual` | CC-BY-NC-4.0 locally; commercial use requires a commercial route | 278M class reported by model family comparisons; long-context multilingual | CPU/GPU; custom code is requested by the model card | Multilingual reranking | Rejected for canonical local use: non-commercial restriction and `trust_remote_code=True` conflict with the safe adapter policy. |
| Learned composed-image retrieval | SEARLE / iSEARLE, exact checkpoint TBD | Repository/model terms require checkpoint-level review | CLIP plus textual-inversion network | GPU generally expected for practical throughput | Explicitly models reference image plus relative text rather than claiming deterministic fusion is learned | Research boundary only. Older architecture and no Qlob-specific evidence; deterministic fusion remains clearly labeled. |
| Reference-image generation/editing | `black-forest-labs/FLUX.1-Kontext-dev` | FLUX.1 dev non-commercial license; production/commercial weights require another agreement or API | 12B parameters | GPU-heavy, not suitable for the current laptop | Text-guided image editing, reference consistency, iterative edits | Inactive. License, hardware, safety-filter, provenance, and paid-provider requirements make it unsuitable for the current local default. |
| Remote generation/editing | Explicit commercial provider, exact model TBD | Provider-specific terms and pricing | Remote | Network, paid account, and privacy review required | Could make reference editing practical without local GPU | Tier 3 only. No hidden fallback, no test calls, and no activation without explicit user authorization. |

## Implemented provider tiers

- Tier 0: `runway-local`, deterministic and offline. Active baseline.
- Tier 1: `sentence-transformers`, explicit local path, CPU by default. Adapter
  implemented; unvalidated and inactive.
- Tier 1/2: `siglip2`, explicit local path, CPU or explicitly configured CUDA.
  Adapter implemented; unvalidated and inactive.
- Tier 3: no remote representation provider is registered.

The optional dependency group is `runway-local[intelligence-ml]`. Installing the
code does not download a model. An operator must separately provision a trusted,
pinned model directory and set the corresponding `RUNWAY_*_MODEL_PATH`. Runway
passes `local_files_only=True` and `trust_remote_code=False`.

## Activation gates

No trained provider may become active until the exact representation plan has
100% coverage, vectors pass integrity validation, the frozen retrieval suite shows
value without duplicate or grounding regressions, channel isolation and rollback
are proven, resource measurements are recorded, and a creator-blind comparison is
completed when downstream captions materially change. Public benchmark results are
research evidence, not an activation decision.

## Primary sources

- [Qwen3 Embedding model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [BGE-M3 model card](https://huggingface.co/BAAI/bge-m3)
- [SigLIP 2 Base model card](https://huggingface.co/google/siglip2-base-patch16-224)
- [Transformers SigLIP 2 documentation](https://huggingface.co/docs/transformers/model_doc/siglip2)
- [DINOv3 model card](https://github.com/facebookresearch/dinov3/blob/main/MODEL_CARD.md)
- [ColPali model card](https://huggingface.co/vidore/colpali)
- [ColQwen2 model collection](https://huggingface.co/collections/vidore/colqwen2-models)
- [BGE reranker v2 M3 model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- [Jina multilingual reranker model card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)
- [SEARLE official repository](https://github.com/miccunifi/SEARLE)
- [FLUX.1 Kontext dev model card](https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev)
