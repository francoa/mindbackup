# Whisper model benchmark

Reproducible comparison of `faster-whisper` model sizes against a real
recording, used to pick the shipped default.

```bash
python tests/benchmark_models.py tiny base small medium large-v3 --language es
python tests/benchmark_models.py small medium --no-vocab      # ablation
```

Results land in `tests/benchmark_results.json` (full transcripts included).

## The test recording

`tests/recordings/transcript_1_v1.wav` — 81 s, Spanish, 8 kHz mono telephone
quality. Deliberately hostile: filler sounds (*"eee"*), self-corrections
(*"de vaca, de ternera, bueno, vaca"*), code-switching (*"Age of Empires"*,
*"cyber seguridad"*, *"gym"*, *"core"*, *"MBA"*), and domain jargon
(*"padel"*, *"lumbares"*, *"bandeja"*, *"milanesas"*). Reference transcript in
`tests/transcripts/transcript_1.txt`.

## Metrics

- **WER** — word error rate vs. the reference, punctuation-insensitive.
  A general accuracy signal, but it punishes harmless comma/period choices as
  hard as real content loss, so it is *not* the deciding metric.
- **Term recall** — the fraction of 13 content words that survived. This is
  what actually determines whether a memo can be retrieved later, so it is the
  metric that decides. Accent- and variant-tolerant: `pádel` counts as `padel`,
  and `ciberseguridad` counts for the spoken `cyber seguridad`, because both
  remain searchable.

## Results (`--language es`, with the vocabulary hint)

| model | disk | peak RSS | WER | **term recall** | 81 s audio | ~30 s memo |
|---|---|---|---|---|---|---|
| tiny | 75 M | — | 0.393 | 0.54 | 1.9 s | ~0.6 s |
| base | 142 M | 445 MB | 0.274 | 0.69 | 3.0 s | ~1.2 s |
| small | 464 M | 893 MB | **0.119** | 0.85 | 12.6 s | ~4.8 s |
| **medium** | 1.5 G | 2570 MB | 0.131 | **0.92** | 21.3 s | ~7.8 s |
| large-v3 | 2.9 G | 4559 MB | 0.125 | 0.85 | 32.8 s | ~12.3 s |

Deterministic: repeated runs produced byte-identical output.

## Why `medium`, not the best WER

`small` wins on WER (0.119 vs 0.131) but **loses on content**. The WER gap is
an artefact of punctuation style — `medium` comma-splices where the reference
uses periods — while `small`'s errors destroy meaning:

| | reference | `small` | `medium` |
|---|---|---|---|
| food | "milanesas y papas fritas" | "miran estas y papas **pericas**" ✗ | "milanesas y papas fritas" ✓ |
| work | "cyber seguridad" | "el **server** seguridad" ✗ | "ciberseguridad" ✓ |

A memo you cannot find is worse than a memo with odd commas. `medium` recovers
11 of 13 key terms where `small` gets 9.

`large-v3` is not better: same 0.85 recall as `small`, 1.5× slower than
`medium`, 4.6 GB peak memory, and it uniquely dropped *"hacking"* and
hallucinated *"Leisure Vampire"* for *"Age of Empires"*. Bigger is not
monotonically better on noisy 8 kHz input.

Nothing recovered *"Age of Empires"* — at 8 kHz it is genuinely ambiguous
("Ace of Empire", "age vampire", "Leisure Vampire"). Add such names to
`MINDBACKUP_VOCABULARY` if they matter to you.

## The vocabulary hint earns its place at every size

| model | recall without | recall with |
|---|---|---|
| tiny | 0.39 | 0.54 |
| base | 0.54 | 0.69 |
| small | 0.69 | 0.85 |
| medium | 0.85 | 0.92 |

Roughly one extra model size worth of accuracy, for free. Language forcing
(`--language es`) made no measurable difference vs. auto-detect on this file,
so the default stays auto-detect — better for code-switching.

## Caveat

**One recording, one speaker, one language.** These numbers justify the default
but are not a general benchmark. Re-run when you add recordings; the harness
scores every `.wav` you point it at.
