# 📁 Live Test Proof — Cardozo High School Finishes

> **Evidence package for the SEC RAG Intelligence pipeline's production validation run.**
> This folder contains the unmodified source document, the router's tag discovery output,
> and the complete Supabase extraction ledger — every artifact from the live test run.

---

## 🧾 Files in This Directory

| File | Size | Description |
|------|------|-------------|
| `Cardozo_High_School_Finishes.pdf` | 86 KB | Source specification document — 5-page finish schedule for GCS-SIGAL LLC / HartmanCox Architects renovation project (Washington DC) |
| `CARDOZO_HIGH_SCHOOL_FINISHES_tags.json` | 36 KB | Router Agent output — 84 verified finish tag candidates with source page mapping, confidence scores, and inline metadata extracted during the 3-engine discovery phase |
| `spec_detail_ledger_rows.json` | 76 KB | Final extraction ledger — all 84 structured specification rows written to Supabase, with manufacturer, model, color, dimensions, CSI section, evidence excerpt, page reference, confidence score, and extraction method for each tag |

---

## 🏃 How to Reproduce This Run

### Prerequisites
- AWS account with the pipeline deployed (see [DESIGN.md](../DESIGN.md) for full setup)
- Environment variables configured (see [README.md](../README.md#installation))
- Pinecone index `construction-vertex-index` initialised

### Steps
1. Upload the source PDF to S3:
   ```bash
   aws s3 cp Cardozo_High_School_Finishes.pdf \
     s3://construction-ingestion/uploads/CARDOZO_HIGH_SCHOOL_FINISHES.pdf
   ```
2. EventBridge fires automatically, triggering the ingestion pipeline.
3. Monitor pipeline state in Supabase:
   ```sql
   SELECT * FROM pipeline_state WHERE project_id = 'CARDOZO_HIGH_SCHOOL_FINISHES';
   ```
4. Once complete, retrieve extraction results:
   ```sql
   SELECT finish_tag, manufacturer, finish_color, extraction_method, confidence, status
   FROM spec_detail_ledger
   WHERE project_id = 'CARDOZO_HIGH_SCHOOL_FINISHES'
   ORDER BY finish_tag;
   ```
5. Compare against `spec_detail_ledger_rows.json` in this directory.

---

## 📊 Validated Results Summary

| Metric | Result |
|--------|--------|
| Source Document | `Cardozo_High_School_Finishes.pdf` (5 pages) |
| Router — Tags Discovered | **84 unique candidates** |
| Extractor — Tags Resolved | **84 / 84 (100% coverage)** |
| Logical Accuracy | **83 / 84 (98.8%)** |
| Tier 1 — Pinecone Direct | 62 tags (73.8%) |
| Tier 2 — LLM Text | 19 tags (22.6%) |
| Tier 3 — Vision API | 2 tags (2.4%) — `CONC`, `SLATE` |
| Infrastructure Fallback | 1 tag (1.2%) — `WDM`, dry_run, retryable |
| Zero Hallucination Events | ✅ Confirmed |
| End-to-End Processing Time | **75.86 seconds** |
| Total Cost | **< $0.12** |

---

## 🔍 How to Read the Ledger JSON

Each entry in `spec_detail_ledger_rows.json` follows this schema:

```json
{
  "project_id": "CARDOZO_HIGH_SCHOOL_FINISHES",
  "finish_tag": "SPORT-1",
  "manufacturer": "TO MARKET ATMOSPHERE RECYCLED FLOORING",
  "model_series": "ACROBAT",
  "finish_color": "POODLE SKIRT TM606",
  "size": "8mm thick 37\"x37\" interlocking tiles",
  "area_scope": "Gym - Fitness Room",
  "csi_section": "09 65 00",
  "evidence_excerpt": "| RUBBER FLOOR TILE | SP0RT‐1 | RUBBER SPORTS FLOOR TILE | TO MARKET ATMOSPHERE RECYCLED FLOORING | ACROBAT | POODLE SKIRT TM606 |",
  "page_reference": 4,
  "extraction_method": "pinecone_direct",
  "confidence": 0.636,
  "status": "confirmed"
}
```

**Key field notes:**
- `evidence_excerpt` — verbatim copy from the source document row (never paraphrased)
- `extraction_method` — `pinecone_direct`, `text`, `vision`, or `dry_run`
- `confidence` — honest 0.0–1.0 score; not inflated. `< 0.5` tags are flagged `verification_needed`
- `N/S` values — represent verified absence of data in the source, not extraction failures

---

## 📋 Full Accuracy Report

See [TEST_METRICS.md](../TEST_METRICS.md) for the complete empirical accuracy report including:
- 5 deep-dive case studies (zero-hallucination guard, OCR normalization, Vision fallback, schema integrity, cross-page inference)
- Full 84-tag extraction table with method, confidence, and status
- SQS DLQ audit analysis
- Business impact quantification

---

*Proprietary and Confidential. Copyright © 2026 Mujeeb Ahmad. All rights reserved.*
