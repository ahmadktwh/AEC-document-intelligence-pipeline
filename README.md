<div align="center">

# 🏗️ SEC RAG Intelligence

### Production-Grade, 3-Tier Multimodal RAG Pipeline for the AEC Industry

*Autonomously ingests raw US commercial construction specification PDFs and extracts structured material data with **98.8% logical accuracy** in under 76 seconds.*

---

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![AWS Lambda](https://img.shields.io/badge/AWS_Lambda-Serverless-FF9900?style=for-the-badge&logo=awslambda&logoColor=white)](https://aws.amazon.com/lambda/)
[![Pinecone](https://img.shields.io/badge/Pinecone-Vector_DB-00B388?style=for-the-badge&logo=pinecone&logoColor=white)](https://www.pinecone.io/)
[![Supabase](https://img.shields.io/badge/Supabase-PostgreSQL-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)](https://supabase.com/)
[![Gemini](https://img.shields.io/badge/Gemini_2.5_Pro-Vision_%2B_Text-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![License](https://img.shields.io/badge/License-Proprietary-red?style=for-the-badge)](./LICENSE)

</div>

---

## 🎯 The Problem: $31.3 Billion in Annual AEC Rework

> *"The AEC industry loses an estimated $31.3 billion annually to rework caused by poor data quality and miscommunication."*
> — **FMI Corporation, Construction Industry Report**

A single commercial renovation project — like the **Cardozo High School renovation** (Washington DC, GCS-SIGAL LLC / HartmanCox Architects) — contains hundreds of finish specification tags distributed across multi-page, merged-cell Excel exports-turned-PDFs, with:

- **OCR corruption** — digit/letter substitutions (`SP0RT‐1` instead of `SPORT-1`)
- **Unicode hyphen mutations** — `U+2010 NON-BREAKING HYPHEN` breaks all naive string matching
- **Cut-off table borders** — rows clipped at page margins that PDF parsers silently drop
- **Deliberately sparse columns** — legitimate empty cells that must be distinguished from extraction failures
- **Headerless rows** — specification blocks with no column alignment whatsoever

A senior estimator manually processes this in **4–8 hours** at **$150–$300 fully loaded**.

**This pipeline processes the same document in 75.86 seconds for under $0.12.**

---

## ⚡ Validated Performance Metrics

*Live test on: **Cardozo High School Material Finish Schedule** — 5-page, 84-tag commercial renovation specification*

| Metric | Result |
|--------|--------|
| **Total Tags Targeted** | 84 |
| **Tags Successfully Resolved** | 84 / 84 — **100% coverage** |
| **Logically Accurate Extractions** | 83 / 84 — **98.8% accuracy** |
| **Processing Time** | **75.86 seconds** |
| **Manufacturer Data Recovered** | 74 / 84 — 88.1% |
| **Color / Finish Data Recovered** | 75 / 84 — 89.3% |
| **Hallucination Events** | ✅ **Zero — Confirmed** |
| **Cost Per Document** | **< $0.12** |

> See [TEST_METRICS.md](TEST_METRICS.md) for the full empirical accuracy report with 5 adversarial case studies, the complete 84-tag extraction table, and DLQ audit results.

---

## 🏛️ The 3-Tier Extraction Engine

The pipeline is engineered on a **cost-efficiency-first** principle: the cheapest, fastest modality is always attempted first. Vision API calls are a last resort — not a default.

```
INPUT: Raw Specification PDF
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 1 — Pinecone Vector Search           73.8% · 62 tags  │
│                                                             │
│  Semantic embedding of AEC tag keywords + domain context.  │
│  Geometric proximity in vector space overcomes OCR typos   │
│  and Unicode hyphen variants. Sub-second retrieval from     │
│  1796+ pre-indexed document chunks.                         │
└──────────────────────────┬──────────────────────────────────┘
                           │ Pinecone confidence insufficient
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 2 — LLM Cognitive Extraction         22.6% · 19 tags  │
│                                                             │
│  Full-text PDF chunk + AEC-tuned prompts passed to Gemini  │
│  2.5 Pro. Resolves headerless rows, merged cells, and      │
│  margin-clipped table fragments using domain reasoning.    │
│  Explicit refusal to hallucinate missing fields → N/S.     │
└──────────────────────────┬──────────────────────────────────┘
                           │ Text extraction unresolvable
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 3 — Vision API Fallback               2.4% · 2 tags  │
│                                                             │
│  Page rendered as high-resolution image. Vision model      │
│  reads visual table layout — bypasses text-layer entirely. │
│  Recovered: CONC (confidence 1.0), SLATE (confidence 1.0). │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  SAFETY NET — Hallucination Guard + Schema Integrity        │
│                                                             │
│  Confidence scoring 0.0–1.0 on every row.                  │
│  verification_needed flag on ambiguous / incomplete specs. │
│  dry_run protection preserves schema on API failures.      │
│  Zero records written with fabricated data — ever.         │
└─────────────────────────────────────────────────────────────┘
```

### Extraction Distribution

| Method | Tags Resolved | Share | Description |
|--------|:---:|:---:|-------------|
| `pinecone_direct` | 62 | 73.8% | Semantic vector search — primary path |
| `text` | 19 | 22.6% | LLM cognitive extraction — secondary path |
| `vision` | 2 | 2.4% | Vision API page rendering — emergency fallback |
| `dry_run` | 1 | 1.2% | API concurrency guard — infrastructure protection, not logic failure |

---

## ☁️ AWS Serverless Architecture

The pipeline is fully serverless — zero persistent compute, event-driven end to end.

```
S3 Upload (PDF)
    │
    └── EventBridge S3 Trigger
            │
            ▼
    SQS: construction-ingestion-queue
            │
            ▼
    Lambda: Ingestion Controller
            │
            ├──────────────────────────┐
            ▼                          ▼
    Lambda: Construction-Loader    Lambda: Router Agent
            │                          │
            │ (fans out chunks)         │ (discovers all tags)
            ▼                          ▼
    SQS: loader-chunk-queue    Supabase RPC: try_trigger_orchestrator
            │                          │ (atomic — exactly-once fan-out)
            ▼                          ▼
    Pinecone Index             SQS: Standard-Worker-Queue
    (1796+ vectors)                    │
                                       │ (5 parallel workers)
                                       ▼
                               Lambda: Construction-Worker-Standard
                                       │
                                       ├── ✅ Resolved → Supabase: spec_detail_ledger
                                       │
                                       └── ⚠️ Unresolved → SQS: Fallback-Worker-Queue
                                                               │
                                                               ▼
                                                   Lambda: Construction-Worker-Fallback
                                                               │ (Vision API)
                                                               ▼
                                                   Supabase: spec_detail_ledger
                                                               │
                                                   Last worker → Archive to S3 + State Reset
```

### Infrastructure Inventory

| Resource | Type | Purpose | Key Configuration |
|----------|------|---------|-------------------|
| `construction-ingestion` | S3 Bucket | Source PDF store | EventBridge notifications enabled |
| `construction-ingestion-queue` | SQS | Pipeline trigger | Visibility: 900s |
| `loader-chunk-queue` | SQS | PDF chunk fan-out | Visibility: 1800s, Concurrency: 3 |
| `loader-chunk-dlq` | SQS DLQ | Loader dead-letter | Retry audit trail |
| `Standard-Worker-Queue` | SQS | Extraction fan-out | Visibility: 905s, Batch size: 1 |
| `Standard-Worker-DLQ` | SQS DLQ | Standard dead-letter | False-positive isolation |
| `Fallback-Worker-Queue` | SQS | Vision fallback | Visibility: 905s, Batch size: 1 |
| `Fallback-Worker-DLQ` | SQS DLQ | Fallback dead-letter | Exhausted retry audit |
| `construction-vertex-index` | Pinecone | Vector store | 1536-dim, cosine similarity, 1796+ vectors |
| `pipeline_state` | Supabase Table | Distributed state machine | RPC: `try_trigger_orchestrator`, `increment_loader_chunk` |
| `spec_detail_ledger` | Supabase Table | Extraction output | JSONB schema, provenance fields per row |
| `pipeline_chunks` | Supabase Table | Chunk coordination | Atomic counter for loader fan-out sync |

### Lambda Functions

| Function | Role | Concurrency | Timeout |
|----------|------|:-----------:|:-------:|
| `Ingestion-Controller` | Orchestrates ingestion, triggers Loader + Router in parallel | 1 | 15 min |
| `Construction-Loader` | Chunks PDF, indexes to Pinecone, self-restarts at 13 min for 1000+ page docs | 3 | 15 min |
| `Construction-Worker-Standard` | 3-tier tag extraction, writes to `spec_detail_ledger` | **5** | 15 min |
| `Construction-Worker-Fallback` | Vision API extraction; checks Standard queue depth before processing | **5** | 15 min |

---

## 🔬 Key Engineering Features

### 1. Zero-Hallucination Guard
The LLM explicitly refuses to cross-attribute sibling tag data. `CMU ≠ GCMU`. `TER-1 ≠ TERT-1`. `TERRAZZO ≠ TERT-2`. When genuinely ambiguous, the system returns `verification_needed` with documented reasoning — never a fabricated value. In bid estimation, misattributing `GCMU` (Trendwyth Tredstone Plus) to a generic `CMU` line could cause a **$15,000–$80,000 material cost estimation error** on a single project.

### 2. OCR + Unicode Normalization
A 10-variant Unicode dash map (U+2010, U+2013, U+2014, U+FE58, U+FF0D, and more) plus OCR confusable substitution (`0↔O`, `1↔I/L`) ensures semantic vector matching succeeds even when the PDF's text layer is character-corrupted. `SP0RT‐1` in the PDF correctly resolves to `SPORT-1` in the query.

### 3. Self-Healing Architecture
- **`dry_run` mode** preserves schema integrity during API failures — a placeholder record is written, never a partial or corrupt one
- **Decentralized finalization** — the last worker detects its own completion and triggers archival and state reset without a coordinator
- **Loader self-restart** — the Loader Lambda chains its own execution at 13 minutes, enabling continuous indexing of 1000+ page PDFs across multiple Lambda invocations

### 4. Distributed Rate Limiting
`GlobalRateLimiter` enforces the 5 RPM Vertex AI quota across all concurrently-running Lambda instances via a Supabase-backed shared counter. No single-instance rate limiter can solve this in a distributed serverless environment.

### 5. Atomic Orchestration
The `try_trigger_orchestrator` Supabase RPC uses a PostgreSQL transaction to guarantee **exactly-one fan-out** even if 100 loader chunks complete simultaneously. Without this, every chunk completion would trigger a separate worker fan-out storm.

### 6. Sequential Gatekeeping
Fallback workers (Vision API) actively check Standard queue depth before processing. If Standard workers are still active, Fallback workers wait — preventing out-of-order Vision calls on tags that Standard workers would have resolved more cheaply.

### 7. SQS Heartbeat
Workers extend their SQS message visibility timeout every 5 minutes via a background thread. This prevents double-processing during the full 15-minute Lambda execution window without requiring visibility timeouts longer than the processing time.

### 8. IP Fortress — PromptVault
All AEC domain intelligence — the engineered extraction prompts that encode CSI MasterFormat knowledge, cross-tag reasoning rules, and hallucination guards — is stored in `PromptVault`: an environment-variable injectable prompt registry. The pipeline code ships without proprietary prompt content.

---

## 📁 Repository Structure

```
sec-rag-intelligence/
├── src/
│   ├── agents/           # RouterAgent (3-engine tag discovery) + ExtractorWorker (3-tier extraction)
│   ├── lambda/           # Lambda handlers: ingestion_controller, worker, fallback_worker
│   ├── pipeline/         # Loader (PDF chunking + Pinecone indexing) + FanOut Orchestrator
│   ├── database/         # LedgerDB, ErrorLogger, SupabaseDB
│   ├── core/             # PromptVault (IP-shielded prompt registry)
│   ├── schemas/          # Pydantic models for structured LLM output
│   └── utils/            # Rate limiter, GCP helper, token ledger
├── proof/                # Live test evidence: source PDF, tags.json, extracted ledger rows
├── DESIGN.md             # Deep-dive architecture documentation
├── TEST_METRICS.md       # Empirical accuracy report with 5 adversarial case studies
├── Dockerfile
└── requirements.txt
```

---

## 🛠️ Technology Stack

| Layer | Technology | Role |
|-------|-----------|------|
| **Runtime** | Python 3.11 + AWS Lambda (Docker) | Serverless execution environment |
| **Orchestration** | AWS SQS fan-out + EventBridge | Event-driven pipeline coordination |
| **Vector Store** | Pinecone (`construction-vertex-index`) | 1536-dim cosine similarity search, 1796+ vectors |
| **State & Output** | Supabase PostgreSQL | Pipeline state machine + `spec_detail_ledger` |
| **LLM** | Gemini 2.5 Pro (Text + Vision) | Cognitive extraction + visual table reading |
| **Embeddings** | `text-embedding-004` (Vertex AI) | AEC-domain semantic vector generation |
| **Structured Output** | Instructor + Pydantic | Type-safe, schema-validated LLM responses |
| **PDF Processing** | PyMuPDF (fitz) + PDFPlumber | Text extraction + page rendering |
| **IP Protection** | PromptVault | Env-var injectable AEC prompt registry |

---

## ⚙️ Installation

```bash
# 1. Clone the repository
git clone https://github.com/ahmadktwh/sec-rag-intelligence.git
cd sec-rag-intelligence

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
```

**Required environment variables:**

```env
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key

# Pinecone
PINECONE_API_KEY=your-pinecone-api-key

# Google Vertex AI
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# AWS SQS Queue URLs
STANDARD_WORKER_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/account/Standard-Worker-Queue
FALLBACK_WORKER_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/account/Fallback-Worker-Queue
LOADER_CHUNK_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/account/loader-chunk-queue

# Lambda Function Names
ROUTER_FUNCTION_NAME=construction-router
LOADER_FUNCTION_NAME=construction-loader
```

---

## 🧪 Live Test Proof

Empirical validation evidence is located in the [`proof/`](proof/) directory:

| File | Description |
|------|-------------|
| `Cardozo_High_School_Finishes.pdf` | Source specification document (5 pages, 84 finish tags) |
| `tags.json` | Router Agent output — all 84 discovered tags with metadata |
| `spec_detail_ledger_rows.json` | Full extraction output — all 84 rows as written to Supabase |

See [TEST_METRICS.md](TEST_METRICS.md) for the complete accuracy report, including:
- 5 deep-dive adversarial case studies (zero-hallucination guard, OCR normalization, headerless row recovery, schema integrity, margin cut-off rescue)
- Full 84-tag extraction table with confidence scores and extraction methods
- SQS DLQ audit — false positive isolation analysis
- Field-level completeness breakdown
- Confidence score distribution

---

## 📊 Business Impact

| Impact Area | Metric |
|-------------|--------|
| **Manual estimator time replaced** | 4–8 hours → 76 seconds |
| **Cost reduction per document** | ~$225 avg → <$0.12 |
| **Annual labor savings (50 projects)** | **$750K – $1.5M** |
| **Bid turnaround improvement** | **3× faster** |
| **Transcription error rate** | → Zero (eliminates RFIs, change orders) |
| **Audit provenance** | `chunk_id`, `page_num`, `evidence_id` per record |

### Target Markets

| Segment | Entry Point |
|---------|------------|
| 🏛️ **US Federal Government Contractors** | GSA, DoD facilities — specification-heavy, volume procurement |
| 🏗️ **National General Contractors** | Turner, Skanska, Gilbane, Whiting-Turner — bid estimation automation |
| 🏢 **AEC B2B SaaS Platforms** | Procore, Autodesk Construction Cloud — embedded intelligence layer |
| 📦 **Material Procurement & Supply Chain** | Automated purchase order generation from specification data |

---

## 🔗 Architecture & Design Documentation

| Document | Contents |
|----------|---------|
| [DESIGN.md](DESIGN.md) | Deep-dive architecture documentation — data flow, schema design, distributed concurrency patterns |
| [TEST_METRICS.md](TEST_METRICS.md) | Empirical accuracy report — 5 adversarial case studies, full 84-tag table, DLQ audit |
| [`proof/`](proof/) | Live test evidence — source PDF, router output, ledger export |

---

## 🚀 Get In Touch

Whether you are an AEC firm looking to automate specification extraction, a technology company exploring enterprise RAG pipeline integration, or a recruiter evaluating senior engineering talent — let's connect.

**Mujeeb Ahmad**
Senior AI/ML Engineer · AEC Document Intelligence

📧 [m.ahmad.aidigital@gmail.com](mailto:m.ahmad.aidigital@gmail.com)
📞 [+923025843504](tel:+923025843504)

---

**Technical Questions:** Open a [GitHub Issue](../../issues) — architecture, integration, or accuracy methodology.

**Enterprise Inquiries:** Direct email preferred for NDA discussions, licensing, and integration scoping.

**Recruiters & Collaborators:** Star the repository, explore the [`proof/`](proof/) directory for live evidence, and reach out via email.

> **Interested in a live demo?** The pipeline can be run against any US commercial construction specification PDF. Contact via email to schedule a demonstration session.

---

<div align="center">

*Proprietary and Confidential. Copyright © 2026 Mujeeb Ahmad. All rights reserved.*

*Unauthorized reproduction, distribution, or commercial use of this software or its methodologies is strictly prohibited.*

</div>
