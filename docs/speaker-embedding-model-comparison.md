# Speaker Embedding Model Comparison (Issue 1.1)

> Voice enrollment and identification on Apple Silicon (M-series Mac)
> Research date: 2026-02-13

## Executive Summary

After evaluating four candidate models for local speaker embedding on Apple Silicon,
**SpeechBrain ECAPA-TDNN** is the recommended choice. It offers the best combination
of embedding quality (0.80% EER on VoxCeleb1), a clean Apache 2.0 license, strong
community adoption (1.3M+ monthly downloads on HuggingFace), a production-ready
Python API, and reliable CPU inference performance on Apple Silicon. The 192-dimension
embeddings are compact and efficient for cosine-similarity-based speaker verification.

---

## Candidates Evaluated

| # | Model | Architecture | Library |
|---|-------|-------------|---------|
| 1 | pyannote/embedding | X-Vector TDNN + SincNet | pyannote.audio |
| 2 | Resemblyzer | 3-layer LSTM (GE2E) | resemblyzer |
| 3 | SpeechBrain ECAPA-TDNN | ECAPA-TDNN (Conv + SE + Res2Net) | speechbrain |
| 4 | ECAPA-TDNN standalone | ECAPA-TDNN (unofficial reimpl.) | TaoRuijie/ECAPA-TDNN |

---

## Detailed Comparison

### 1. pyannote/embedding

**Architecture:** Canonical x-vector TDNN with trainable SincNet features replacing
fixed filter banks. 512-unit-wide, 3-recurrent-layer deep network.

| Criterion | Details |
|-----------|---------|
| Embedding dimension | 512 (reduced to 150 via LDA in some pipelines) |
| EER (VoxCeleb1-O) | 2.8% (cosine distance, no VAD/PLDA) |
| Model size | ~70-80 MB estimated (checkpoint not publicly documented) |
| License | MIT (code) -- but **gated model** on HuggingFace requiring acceptance of user conditions and an access token |
| MPS/Apple Silicon | **Problematic.** Known issues include wrong timestamps on MPS (M1), kernel crashes on M4, and the need for `PYTORCH_ENABLE_MPS_FALLBACK=1`. CPU fallback works but is slower. |
| Inference latency | Not benchmarked by authors; pure PyTorch inference (3.x removed onnxruntime dep). Expect 50-150ms on CPU for a 3-5s segment on Apple Silicon. |
| Maintenance | Actively maintained (pyannote.audio 3.3.x, 2025). Premium commercial offerings also available. |
| Python API | Clean pipeline API (`Model.from_pretrained()`), but requires HuggingFace token setup. Gated access adds friction. |

**Strengths:**
- Part of a larger diarization ecosystem (segmentation + embedding + clustering).
- MIT license on the code itself.
- Pure PyTorch in 3.x (no more onnxruntime dependency issues).

**Weaknesses:**
- Gated model requires HuggingFace account and token -- adds deployment friction.
- Worst EER of the four candidates (2.8%).
- Known MPS issues on Apple Silicon (crashes, wrong timestamps).
- Higher embedding dimension (512) means larger enrollment database and slower similarity computation (though still fast in practice).

**References:**
- [pyannote/embedding on HuggingFace](https://huggingface.co/pyannote/embedding)
- [pyannote-audio GitHub](https://github.com/pyannote/pyannote-audio)
- [MPS timestamp issue #1337](https://github.com/pyannote/pyannote-audio/issues/1337)
- [M4 kernel crash issue #1886](https://github.com/pyannote/pyannote-audio/issues/1886)

---

### 2. Resemblyzer

**Architecture:** 3-layer LSTM with 256 hidden units, trained with Generalized
End-to-End (GE2E) loss on VoxCeleb1 + VoxCeleb2 + LibriSpeech-other (~8.4k speakers).

| Criterion | Details |
|-----------|---------|
| Embedding dimension | 256 |
| EER (VoxCeleb1-O) | ~4.5% (reported by authors on internal test set; actual VoxCeleb1-O EER not officially published) |
| Model size | ~17 MB (3-layer LSTM with 256 units is lightweight; `pretrained.pt` ships with the package) |
| License | Apache 2.0 |
| MPS/Apple Silicon | No explicit MPS support. CPU-only by default, with optional CUDA. CPU performance is excellent given the small model size. |
| Inference latency | ~1000x real-time on GPU; CPU on Apple Silicon should be <20ms for a 3s segment given the tiny model. |
| Maintenance | **Low/stale.** Last meaningful code push ~2 years ago. Compatibility issues with newer librosa versions. PyPI version 0.1.4 is dated. |
| Python API | Very simple: `VoiceEncoder()`, `encoder.embed_utterance(wav)`. Minimal dependencies. Easiest to integrate of all candidates. |

**Strengths:**
- Smallest model, fastest inference, simplest API.
- Ships the pretrained model inside the pip package -- no downloads or tokens needed.
- Apache 2.0, no restrictions.
- L2-normalized embeddings work directly with cosine similarity.

**Weaknesses:**
- Worst embedding quality by a significant margin (4.5% EER vs 0.80% for SpeechBrain).
- Essentially unmaintained -- broken with current librosa versions unless using the git master branch.
- LSTM architecture is outdated compared to ECAPA-TDNN.
- Trained primarily on English; cross-language performance degrades.
- 256-dim embeddings are less discriminative than ECAPA-TDNN's 192-dim trained embeddings (dimension alone does not determine quality).

**References:**
- [Resemblyzer GitHub](https://github.com/resemble-ai/Resemblyzer)
- [Resemblyzer on PyPI](https://pypi.org/project/Resemblyzer/)
- [GE2E Loss paper](https://arxiv.org/abs/1710.10467)

---

### 3. SpeechBrain ECAPA-TDNN (RECOMMENDED)

**Architecture:** ECAPA-TDNN (Emphasized Channel Attention, Propagation and Aggregation
in TDNN). Uses 1024-channel convolutional layers, Res2Net modules with skip connections,
Squeeze-and-Excitation (SE) blocks, and attentive statistical pooling. Trained with
Additive Angular Margin (AAM) Softmax loss on VoxCeleb1 + VoxCeleb2.

| Criterion | Details |
|-----------|---------|
| Embedding dimension | 192 |
| EER (VoxCeleb1-O, cleaned) | **0.80%** |
| EER (VoxCeleb1-E) | 1.30% |
| EER (VoxCeleb1-H) | 1.98% |
| Model size | **83.3 MB** (embedding_model.ckpt) |
| Parameters | ~14.7M (estimated from checkpoint size and architecture) |
| License | **Apache 2.0** (both SpeechBrain framework and pretrained model) |
| MPS/Apple Silicon | PyTorch-based; MPS device can be used but is not explicitly tested by SpeechBrain. CPU inference on Apple Silicon is the reliable path. Convolution-heavy architecture runs efficiently on ARM NEON. |
| Inference latency | ~30-80ms on Apple Silicon CPU for a 3-5s segment (estimated from architecture complexity and comparable benchmarks; one implementation reports 300ms for full verification pipeline including I/O). |
| Maintenance | **Actively maintained.** SpeechBrain 1.x released in 2024-2025. 1.3M+ monthly downloads. Active GitHub and HuggingFace community. |
| Python API | Clean high-level API via `EncoderClassifier.from_hparams()` and `SpeakerRecognition.from_hparams()`. Supports `encode_batch()` for embeddings and `verify_batch()` for verification with built-in cosine scoring. |

**Strengths:**
- Best embedding quality of all candidates (0.80% EER).
- Well-maintained, production-grade toolkit with excellent documentation.
- Apache 2.0 license with no restrictions on commercial use.
- 192-dimension embeddings are compact yet highly discriminative.
- Built-in speaker verification API with configurable cosine similarity threshold.
- AAM-Softmax training produces well-separated embedding spaces ideal for cosine scoring.
- No gated access -- downloads directly from HuggingFace without tokens.
- Training included noise/reverb/compression augmentation, providing robustness to system audio loopback quality.

**Weaknesses:**
- Larger model (83 MB) compared to Resemblyzer (17 MB), though well within the 500 MB budget.
- SpeechBrain has a heavier dependency tree (though only the inference path is needed).
- First model load takes a few seconds to download from HuggingFace (cached afterward).
- No official MPS benchmarks from the SpeechBrain team.

**References:**
- [speechbrain/spkrec-ecapa-voxceleb on HuggingFace](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)
- [SpeechBrain GitHub](https://github.com/speechbrain/speechbrain)
- [ECAPA-TDNN paper](https://arxiv.org/abs/2005.07143)
- [SpeechBrain speaker inference API docs](https://speechbrain.readthedocs.io/en/latest/API/speechbrain.inference.speaker.html)
- [embedding_model.ckpt file](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/blob/main/embedding_model.ckpt)

---

### 4. ECAPA-TDNN Standalone (TaoRuijie)

**Architecture:** Unofficial reimplementation of ECAPA-TDNN based on the
voxceleb_trainer framework. Same core architecture as SpeechBrain's version.

| Criterion | Details |
|-----------|---------|
| Embedding dimension | 192 (default configuration) |
| EER (VoxCeleb1-O) | 0.86% (with AS-norm), 0.96% (without AS-norm) |
| Model size | ~80-85 MB (similar architecture to SpeechBrain) |
| License | MIT |
| MPS/Apple Silicon | Pure PyTorch. Same considerations as SpeechBrain -- CPU is the reliable path. |
| Inference latency | Similar to SpeechBrain (~30-80ms on Apple Silicon CPU). |
| Maintenance | **Low.** Unofficial reimplementation. No active release cycle. Research code quality. |
| Python API | No high-level inference API. Requires manual model loading, feature extraction, and forward pass. Significantly more integration work than SpeechBrain. |

**Strengths:**
- MIT license (most permissive).
- Competitive accuracy (0.86% EER).
- Lightweight -- no large framework dependency.

**Weaknesses:**
- Unofficial reimplementation -- no guarantee of correctness or continued support.
- No high-level API for embedding extraction or verification.
- Requires writing your own audio preprocessing pipeline.
- Less community support and documentation.
- Research code, not production-grade.

**References:**
- [TaoRuijie/ECAPA-TDNN GitHub](https://github.com/TaoRuijie/ECAPA-TDNN)
- [ECAPA-TDNN paper](https://arxiv.org/abs/2005.07143)

---

## Side-by-Side Summary

| Criterion | pyannote | Resemblyzer | SpeechBrain ECAPA | ECAPA Standalone |
|-----------|----------|-------------|-------------------|------------------|
| **EER (VoxCeleb1-O)** | 2.80% | ~4.50% | **0.80%** | 0.86% |
| **Embedding dim** | 512 | 256 | **192** | 192 |
| **Model size** | ~70-80 MB | ~17 MB | 83.3 MB | ~80-85 MB |
| **License** | MIT (gated) | Apache 2.0 | **Apache 2.0** | MIT |
| **Apple Silicon** | MPS issues | CPU only | CPU (reliable) | CPU (reliable) |
| **Est. latency (CPU)** | 50-150ms | <20ms | **30-80ms** | 30-80ms |
| **API quality** | Good (needs token) | Excellent (simple) | **Excellent** | Poor (manual) |
| **Maintenance** | Active | Stale | **Active** | Low |
| **Gated access** | Yes | No | **No** | No |
| **Noise/compression robust** | Moderate | Moderate | **Strong** | Strong |

---

## Cosine Similarity Thresholds for Speaker Verification

Speaker verification with cosine similarity uses a threshold to decide whether two
embeddings belong to the same speaker. The optimal threshold depends on the embedding
model, the target false-accept/false-reject tradeoff, and the audio conditions.

### Typical Threshold Ranges

| Model | Recommended threshold | Notes |
|-------|----------------------|-------|
| SpeechBrain ECAPA-TDNN | **0.25** (default in `verify_batch`) | SpeechBrain's default; well-calibrated for AAM-Softmax embeddings. Score > 0.25 = same speaker. |
| pyannote | ~0.3-0.5 | Less documented; depends on whether LDA reduction is applied. |
| Resemblyzer | ~0.6-0.7 | GE2E embeddings tend to have higher same-speaker scores; threshold is higher. |

### Score Distribution Characteristics

For well-trained ECAPA-TDNN embeddings with AAM-Softmax loss:

- **Same-speaker pairs:** Cosine similarity typically ranges from 0.3 to 0.9, with a
  mean around 0.6-0.7.
- **Different-speaker pairs:** Cosine similarity typically ranges from -0.1 to 0.3,
  with a mean around 0.05-0.15.
- **EER threshold:** The point where FAR = FRR, typically around 0.25 for SpeechBrain's
  ECAPA-TDNN.

### Threshold Selection Strategy for Our Use Case

For meeting speaker identification (not security-critical), we should optimize for
low false-rejection to avoid missing speaker labels:

1. **Start with SpeechBrain's default: 0.25**
2. **Tune empirically:** Enroll 2-3 test speakers, collect similarity scores, and
   adjust based on the actual score distribution in our audio environment.
3. **Consider a two-tier approach:**
   - Score > 0.40: High confidence match (label immediately)
   - Score 0.20-0.40: Tentative match (label with lower confidence flag)
   - Score < 0.20: No match / unknown speaker

---

## Compressed Audio and System Loopback Compatibility

Our use case captures audio through BlackHole 2ch (virtual audio device loopback).
This means the audio has been through at least one encode/decode cycle and may have
artifacts from the application's audio pipeline.

### Key Considerations

1. **Sample rate:** All four models expect 16kHz mono input. BlackHole captures at
   the system sample rate (typically 48kHz). Downsampling to 16kHz is already handled
   in our audio pipeline.

2. **Audio quality:** System audio loopback is typically high quality (no microphone
   noise, no room reverb), but may include:
   - Codec artifacts from video conferencing apps (Zoom/Teams use Opus/Silk at
     variable bitrates)
   - Slight latency-related discontinuities
   - Mixed speaker audio (multiple remote participants on same channel)

3. **Robustness of ECAPA-TDNN:** The SpeechBrain ECAPA-TDNN model was trained with
   augmentation that includes compression, noise, reverb, and speed perturbation.
   This makes it robust to the kind of artifacts present in system audio loopback.
   ECAPA2 (the successor model) explicitly lists compression as a training
   augmentation strategy.

4. **Mixed speaker channels:** System audio loopback captures all remote participants
   on a single channel. Speaker embedding extraction works best on single-speaker
   segments. Our chunking strategy (3-5 second segments) combined with basic energy
   VAD should provide segments where one speaker dominates, but overlap will reduce
   embedding quality. This is an inherent limitation of loopback capture, not a
   model limitation.

---

## Apple Silicon Compatibility Notes

### PyTorch MPS Backend Status (as of early 2026)

- MPS backend is still in beta/experimental status in PyTorch.
- 8-10x speedups over CPU have been reported for transformer inference when MPS
  works correctly.
- Known issues with specific operations (e.g., `torch.istft`, `scaled_dot_product_attention`)
  that can cause crashes or incorrect results.
- The `PYTORCH_ENABLE_MPS_FALLBACK=1` environment variable enables CPU fallback for
  unsupported MPS operations, but this can be slower than running entirely on CPU.

### Model-Specific MPS Status

| Model | MPS Status |
|-------|-----------|
| pyannote | **Avoid.** Known crashes (M4) and incorrect output (M1). |
| Resemblyzer | Not supported (CPU/CUDA only). CPU is fast enough. |
| SpeechBrain ECAPA-TDNN | **Untested by upstream.** CPU is recommended for reliability. MPS may work but is not guaranteed. |
| ECAPA Standalone | Same as SpeechBrain. |

### Recommendation: Use CPU on Apple Silicon

For speaker embedding inference on Apple Silicon:
- The ECAPA-TDNN model (~14.7M parameters, pure convolutions) runs fast on CPU.
- Apple Silicon's unified memory architecture and NEON SIMD provide good CPU inference
  performance without needing GPU acceleration.
- Estimated 30-80ms per 3-5s segment on M1/M2/M3 CPU is well within our <100ms target.
- Avoiding MPS eliminates an entire class of compatibility issues.
- If future PyTorch MPS improvements make it stable, switching is a one-line change
  (`device="mps"`).

---

## Integration Plan with Existing Architecture

Based on the existing `SpeakerDiarizationClient` in
`src/audio_assist/diarization.py` and the service contract in
`docs/speaker-detection-plan.md`:

### Embedding Service Architecture

```
audio-assist (capture) --> speaker-service (embedding + matching)
                              |
                              ├── SpeechBrain ECAPA-TDNN (embedding extraction)
                              ├── Enrollment store (known speaker embeddings)
                              └── Cosine similarity matching
```

### Integration Steps

1. **Install SpeechBrain** in the speaker-service environment:
   ```bash
   pip install speechbrain
   ```

2. **Extract embeddings** using the high-level API:
   ```python
   from speechbrain.inference.speaker import EncoderClassifier

   encoder = EncoderClassifier.from_hparams(
       source="speechbrain/spkrec-ecapa-voxceleb",
       savedir="/tmp/speechbrain_cache",
       run_opts={"device": "cpu"},
   )

   # Extract 192-dim embedding from audio
   embedding = encoder.encode_batch(waveform)  # shape: (1, 1, 192)
   ```

3. **Speaker verification** using the built-in API:
   ```python
   from speechbrain.inference.speaker import SpeakerRecognition

   verifier = SpeakerRecognition.from_hparams(
       source="speechbrain/spkrec-ecapa-voxceleb",
       savedir="/tmp/speechbrain_cache",
       run_opts={"device": "cpu"},
   )

   score, prediction = verifier.verify_batch(
       waveform1, waveform2, threshold=0.25
   )
   ```

4. **Enrollment workflow:**
   - `POST /v1/enroll` -> extract embedding, store in enrollment DB
   - `POST /v1/diarize/chunk` -> extract embedding, compare against enrolled
     speakers via cosine similarity, return best match above threshold

---

## Recommendation

### Primary: SpeechBrain ECAPA-TDNN

Use `speechbrain/spkrec-ecapa-voxceleb` as the speaker embedding model.

**Rationale (ordered by criteria priority):**

1. **Apple Silicon compatibility:** Pure PyTorch, runs reliably on CPU. No MPS
   issues. Estimated 30-80ms per segment, well within target.
2. **Embedding quality:** Best-in-class 0.80% EER on VoxCeleb1-O. 3.5x better
   than pyannote (2.8%) and 5.6x better than Resemblyzer (4.5%).
3. **Model size:** 83.3 MB -- well within the 500 MB budget.
4. **Inference latency:** Meets the <100ms target on Apple Silicon CPU.
5. **License:** Apache 2.0, permissive for personal and commercial use. No gated
   access, no tokens required.
6. **API quality:** Excellent high-level API with `encode_batch()` and
   `verify_batch()`. Built-in cosine similarity scoring with configurable threshold.
7. **Maintenance:** Actively maintained, 1.3M+ monthly downloads, strong community.

### Fallback: Resemblyzer

If SpeechBrain's dependency footprint is too heavy for the speaker-service
container, Resemblyzer provides a lightweight alternative with a simpler API
and smaller model, at the cost of significantly lower accuracy (4.5% vs 0.80% EER).
However, the maintenance status is a concern.

### Not Recommended

- **pyannote/embedding:** Gated access friction, worst accuracy among ECAPA variants,
  and known MPS crashes make it unsuitable as the primary embedding model. However,
  pyannote's *diarization* pipeline remains valuable for segmentation (who-spoke-when)
  as a separate concern.
- **ECAPA-TDNN standalone:** Good accuracy but no production API, no active
  maintenance, and more integration work for marginal benefit over SpeechBrain.

---

## Future Considerations

- **ECAPA2:** The next-generation ECAPA model (2024) achieves state-of-the-art
  results and includes compression augmentation in training. Available via Wespeaker.
  Worth evaluating when the speaker-service is mature.
- **Wespeaker toolkit:** Production-oriented speaker embedding toolkit with ECAPA-TDNN
  and newer architectures. Could replace SpeechBrain if we need ONNX export or
  more optimized inference.
- **CoreML/MLX conversion:** For maximum Apple Silicon performance, the ECAPA-TDNN
  model could be converted to CoreML or Apple MLX format. This would enable Metal
  GPU acceleration without MPS compatibility issues.
- **Fine-tuning on enrolled speakers:** Once we have enrollment data, fine-tuning
  the embedding model on our specific speakers could improve verification accuracy
  in our audio environment.
