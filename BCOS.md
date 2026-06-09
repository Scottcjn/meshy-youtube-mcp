# BCOS — Blockchain Certified Open Source

[![BCOS Ready](https://img.shields.io/badge/BCOS-Ready-yellowgreen?style=flat)](https://github.com/Scottcjn/Rustchain)

## What is BCOS?

**Blockchain Certified Open Source (BCOS)** is a human-review certification for
open source repositories. It means:

1. **Human eyes have reviewed this code** — a real person has read the source,
   not just an AI. We verify the code does what it claims and nothing more.
2. **Safe for AI agents** — certified repos are safe for autonomous AI agents to
   install, import, and execute. No hidden telemetry, no supply-chain payloads.
3. **On-chain attestation** — each certification is backed by a cryptographic
   attestation on the [RustChain](https://github.com/Scottcjn/Rustchain)
   blockchain: an immutable record of when and by whom the code was reviewed.

## Why it fits this repo

`meshy-youtube-mcp` is built to be **executed autonomously by AI agents** and it
**handles OAuth credentials** that grant YouTube upload access — so the trust
question ("is it safe to let an agent run this with my Google token?") is
front-and-center. This repo is designed to pass:

| Requirement | How this repo meets it |
|-------------|------------------------|
| **Source readable** | Pure Python, no minified/obfuscated blobs |
| **No hidden network calls** | Only contacts `api.meshy.ai` and Google's YouTube Data API (`googleapis.com`) |
| **No credential harvesting** | OAuth token written `0600`, atomically; keys from env/local files only; never logged or phoned home |
| **Declared dependencies** | All in `requirements.txt` / `pyproject.toml` (`mcp`, `requests`, `google-*`) |
| **Build reproducible** | Deterministic; 21 offline unit tests, ruff-clean |
| **License clear** | MIT (`LICENSE`) |
| **Human reviewed** | Human-directed + adversarially reviewed (see below); formal line-by-line maintainer sign-off is the last step to full **Certified** |

## Review record

| Field | Value |
|-------|-------|
| **Status** | **BCOS-Ready** — meets all technical criteria above; human-directed and adversarially AI-reviewed. Full **Certified** status follows the maintainer's line-by-line read + on-chain attestation. |
| **Maintained & directed by** | Scott Boudreaux ([@Scottcjn](https://github.com/Scottcjn)), Elyan Labs |
| **Adversarial review** | Multi-model review (Codex security audit + Grok regression) before publish, with extra focus on OAuth token handling, redirect/COPPA/encoding safety — see commit history |
| **Chain** | [RustChain](https://github.com/Scottcjn/Rustchain) (Proof-of-Antiquity) |

## Verify

```bash
pip install clawrtc
clawrtc verify-bcos https://github.com/Scottcjn/meshy-youtube-mcp
```

Or check the [RustChain Explorer](https://rustchain.org/explorer) for the
on-chain attestation record.

---

*BCOS is an initiative of Elyan Labs and the
[RustChain](https://github.com/Scottcjn/Rustchain) project.*
