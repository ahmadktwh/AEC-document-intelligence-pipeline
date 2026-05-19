import os
import re
import logging
import fitz  # PyMuPDF
import base64
import time
import json
import random
import pdfplumber
import boto3
import threading
from botocore.exceptions import ClientError
from typing import Optional, List, Dict, Any
from google import genai
from google.genai import types
from pinecone import Pinecone
from dotenv import load_dotenv
from src.utils.rate_limiter import GlobalRateLimiter
from src.database.token_ledger import token_ledger_db
from src.database.error_logger import error_logger
from src.utils.gcp_helper import setup_gcp_credentials
from src.database.supabase_db import (
    supabase,
    increment_chunk,
    try_trigger_orchestrator,
    init_pipeline_state
)


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()
logger.setLevel(logging.INFO)


# Global Rate Limiter for all Vertex AI services (Embeddings + Vision)
# Shared with Router and Worker to strictly enforce 5 RPM / 20k tokens project-wide.
_global_gemini_limiter = GlobalRateLimiter("GLOBAL_GEMINI_LOCK", rpm_limit=5, token_limit=20000)

def convert_table_row_to_natural_language(pipe_row: str) -> str:
    import re
    parts = [p.strip() for p in pipe_row.split("|")]
    parts = [p for p in parts if p]
    if len(parts) >= 2 and re.match(r'^[A-Z]{1,6}-\d', parts[0]):
        tag = parts[0]
        rest = ", ".join(parts[1:])
        return f"Finish tag {tag}: {rest}"
    return pipe_row

class DocumentLoader:
    """
    Advanced Ingestion Pipeline (Branch A)
    - Supports Tesseract OCR for Scanned PDFs
    - Supports gemini-2.5-pro (Vision) for converting broken tables into highly structured text
    - Pushes flawless embedded data to Pinecone.
    """
    def __init__(self, index_name: Optional[str] = None):
        self.pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
        # Using the new construction-vertex-index (768 dim)
        idx = index_name or os.environ.get("PINECONE_INDEX", "construction-vertex-index")
        self.index = self.pc.Index(idx)
        
        # Setup GCP Credentials for Vertex AI
        setup_gcp_credentials()
        
        self.client = genai.Client(
            vertexai=True,
            project=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "us-central1")
        )

    def _embed_with_retry(self, texts: list, max_retries: int = 3) -> list:
        """
        Max 3 retries. Max total sleep = ~45 seconds per batch.
        With jitter to prevent synchronized retries across parallel Lambdas.
        """
        for attempt in range(max_retries):
            try:
                # Acquire rate limit token BEFORE calling API (shared 5 RPM lock)
                _global_gemini_limiter.acquire()
                
                res = self.client.models.embed_content(
                    model="text-embedding-004",
                    contents=texts,
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                        output_dimensionality=768
                    )
                )
                return res.embeddings

            except Exception as e:
                error_str = str(e)
                is_rate_limit = (
                    "429" in error_str or 
                    "RESOURCE_EXHAUSTED" in error_str or
                    "quota" in error_str.lower()
                )
                
                if is_rate_limit and attempt < max_retries - 1:
                    # Jittered backoff: 2^attempt * 10 + jitter
                    wait = (2 ** attempt) * 10 + random.uniform(0, 10)
                    logger.warning(
                        f"[RATE LIMIT] text-embedding-004 429 hit. "
                        f"Attempt {attempt+1}/{max_retries}. "
                        f"Waiting {wait:.1f}s..."
                    )
                    time.sleep(wait)
                    continue
                
                if attempt == max_retries - 1:
                    logger.error(
                        f"FATAL: Embedding failed after {max_retries} attempts. "
                        f"Batch skipped. Error: {e}"
                    )
                    raise
                
                # Non-rate-limit error: short wait and retry
                wait = (attempt + 1) * 3
                logger.warning(f"Embed error retry {attempt+1}: {e}")
                time.sleep(wait)

    def get_embedding(self, text: str, project_id: str):
        """Generates 768-dim embedding for construction-vertex-index."""
        text = text.replace("\n", " ")
        embeddings = self._embed_with_retry([text])
        return embeddings[0].values

    def _extract_page_as_base64(self, doc: fitz.Document, page_num: int) -> str:
        """Helper to get a Base64 image of the PDF page."""
        page = doc[page_num]
        mat = fitz.Matrix(2.0, 2.0) # High-res
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("utf-8")

    def _process_with_vision(self, pdf_path: str, page_num: int, doc: fitz.Document, project_id: str) -> str:
        """Passes the page image to Gemini 3.1 Pro Preview to extract structural data perfectly."""
        b64_image = self._extract_page_as_base64(doc, page_num)
        
        prompt = (
            "You are an Elite Data Extraction AI. The attached image is a construction document page. "
            "If it contains a table (e.g., Finish Schedule, Material Legend), extract ALL rows and columns "
            "perfectly. Keep the relationships between Tag, Manufacturer, Model, and Color completely intact. "
            "Output the data in clear, structured plain text or markdown lists so a downstream retrieval system "
            "will have 0% data loss. Do not summarize, extract everything VERBATIM."
        )

        # Quota protection with jittered retry
        max_vision_retries = 5
        for attempt in range(max_vision_retries):
            try:
                # MANDATORY: Acquire Global Lock before Vision call to prevent 429
                logger.info(f"Page {page_num+1} Vision: Waiting for GLOBAL_GEMINI_LOCK...")
                _global_gemini_limiter.acquire()

                response = self.client.models.generate_content(
                    model=os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro"),
                    contents=[
                        types.Part.from_bytes(data=base64.b64decode(b64_image), mime_type='image/png'),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        http_options=types.HttpOptions(timeout=120000)
                    )
                )
                break
            except Exception as e:
                if "429" in str(e) and attempt < max_vision_retries - 1:
                    wait_time = (2 ** (attempt + 1)) + random.uniform(0, 2)
                    logger.warning(f"Vision 429 hit (Page {page_num+1}), retrying in {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue
                raise e

        # Log tokens safely (Best-effort)
        try:
            usage = getattr(response, 'usage_metadata', None)
            if usage:
                in_tokens = getattr(usage, 'prompt_token_count', 0)
                out_tokens = getattr(usage, 'candidates_token_count', 0)
                logger.info(f"Page {page_num+1} Vision Tokens: In={in_tokens}, Out={out_tokens}")
                token_ledger_db.log_usage(
                    project_id=project_id,
                    agent_name="Ingestion_Vision",
                    model_name="gemini-2.5-pro",
                    input_tokens=in_tokens,
                    output_tokens=out_tokens
                )
        except Exception as log_err:
            logger.warning(f"Vision token logging failed for p{page_num+1}: {log_err}")

        return response.text

    def _get_already_processed_pages(self, project_id: str, page_nums: list) -> set:
        """Batch fetch all page IDs at once. Returns a set of already-done vector IDs."""
        try:
            vector_ids = [f"{project_id}_p{n}" for n in page_nums]
            # Pinecone fetch supports up to 1000 IDs per call
            result = self.index.fetch(ids=vector_ids)
            return set(result.vectors.keys())
        except Exception:
            return set()  # If check fails, process all pages (safe default)

    def _extract_tables_with_row_recovery(
        self,
        page: "pdfplumber.page.Page",
        project_id: str,
        page_num: int
    ) -> tuple[str, bool]:
        """
        Bulletproof table extraction that recovers truncated bottom rows.

        WHY THE PROBLEM EXISTS:
          pdfplumber's default strategy closes a row only when it sees a bottom
          horizontal border line. When the last row of a table sits flush against
          the page bottom (e.g. STN-2 in Cardozo), that closing line is absent
          and the entire row is silently dropped.

        WHY A NAIVE FIX FAILS:
          Injecting a virtual line at the absolute page bottom margin merges any
          non-table paragraphs below a mid-page table into its last row, corrupting data.

        THIS SOLUTION:
          1. Use find_tables() to get real table bounding boxes + existing h-lines.
          2. For each table, measure how far below the last detected h-line there
             is still text inside the table's x-range (orphaned chars).
          3. Inject a virtual closing line precisely at the BOTTOM OF THOSE CHARS,
             not at the page margin — this closes the row surgically.
          4. Re-extract the table on a cropped region that includes only the table area.
          5. Fallback: if re-extraction still does not add a row, reconstruct the
             missing row at the char level using vertical-line column boundaries.
        """
        BASE_SETTINGS = {
            "vertical_strategy":   "lines",
            "horizontal_strategy": "lines",
            "snap_tolerance":      3,
            "join_tolerance":      3,
            "edge_min_length":     3,
            "min_words_vertical":  1,
            "min_words_horizontal":1,
        }

        tables = page.find_tables(table_settings=BASE_SETTINGS)
        if not tables:
            return "", False

        all_blocks: list[str] = []

        for table in tables:
            # pdfplumber bbox: (x0, top, x1, bottom) — all in page-space
            # where top/bottom are distances from the TOP of the page.
            x0, tbl_top, x1, tbl_bottom = table.bbox

            # What pdfplumber managed to extract without any fix
            initial_rows = table.extract()
            final_rows   = initial_rows  # default — may be overridden below

            # ── Detect orphaned chars below the last h-line ─────────────────
            h_edges_in_table = [
                e for e in page.horizontal_edges
                if (x0 - 3 <= e["x0"] and e["x1"] <= x1 + 3
                    and tbl_top - 3 <= e["top"] <= tbl_bottom + 3)
            ]

            if h_edges_in_table:
                last_line_top = max(e["top"] for e in h_edges_in_table)

                # How high is a typical row in this table?
                row_height_estimate = (
                    (tbl_bottom - tbl_top) / max(len(initial_rows), 1)
                )
                # We search at most 2 row-heights below the last line —
                # this prevents us from swallowing text from the next section.
                search_ceiling = last_line_top + 1           # just below the line
                search_floor   = last_line_top + row_height_estimate * 2

                orphaned = [
                    c for c in page.chars
                    if (x0 - 3 <= c["x0"] <= x1 + 3
                        and search_ceiling < c["top"] < search_floor
                        and c["text"].strip())  # ignore whitespace
                ]

                if orphaned:
                    orphaned_text_sample = "".join(
                        c["text"] for c in sorted(orphaned, key=lambda c: c["x0"])
                    )[:60]
                    logger.info(
                        f"[{project_id}] Page {page_num+1}: "
                        f"{len(orphaned)} orphaned chars below last h-line "
                        f"(y≈{last_line_top:.1f}). Sample: '{orphaned_text_sample}'. "
                        f"Attempting dynamic row recovery..."
                    )

                    # ── Primary recovery: crop + explicit closing line ───────
                    orphaned_bottom_y = max(c["bottom"] for c in orphaned)
                    virtual_line_y    = orphaned_bottom_y + 2.0  # 2pt padding

                    crop_x0     = max(0,            x0  - 2)
                    crop_top    = max(0,             tbl_top - 2)
                    crop_x1     = min(page.width,   x1  + 2)
                    crop_bottom = min(page.height,   virtual_line_y + 4)

                    cropped = page.crop((crop_x0, crop_top, crop_x1, crop_bottom))

                    # Translate the virtual line into the cropped page's coordinate space
                    virtual_line_in_crop = virtual_line_y - crop_top

                    recovery_settings = {
                        **BASE_SETTINGS,
                        "explicit_horizontal_lines": [virtual_line_in_crop],
                    }

                    try:
                        recovered_tables = cropped.find_tables(
                            table_settings=recovery_settings
                        )
                        if recovered_tables:
                            recovered_rows = recovered_tables[0].extract()
                            if len(recovered_rows) > len(initial_rows):
                                final_rows = recovered_rows
                                logger.info(
                                    f"[{project_id}] Page {page_num+1}: "
                                    f"PRIMARY recovery SUCCESS — "
                                    f"{len(initial_rows)} → {len(recovered_rows)} rows "
                                    f"(virtual line at y={virtual_line_y:.1f})."
                                )
                            else:
                                # ── Fallback: char-level column reconstruction ─
                                final_rows = self._reconstruct_last_row(
                                    initial_rows, orphaned, page, table
                                )
                                logger.info(
                                    f"[{project_id}] Page {page_num+1}: "
                                    f"FALLBACK char-reconstruction applied "
                                    f"({len(final_rows)} rows)."
                                )
                    except Exception as exc:
                        logger.warning(
                            f"[{project_id}] Page {page_num+1}: "
                            f"Row recovery failed ({exc}). Using initial rows."
                        )

            # ── Convert rows → pipe-delimited strings ───────────────────────
            for row in final_rows:
                if row and any(c for c in row if c and str(c).strip()):
                    clean = [
                        str(c).replace("\n", " ").strip() if c else ""
                        for c in row
                    ]
                    all_blocks.append(" | ".join(clean))

        return "\n".join(all_blocks), bool(all_blocks)

    def _reconstruct_last_row(
        self,
        initial_rows: list,
        orphaned_chars: list,
        page: "pdfplumber.page.Page",
        table: "pdfplumber.table.Table"
    ) -> list:
        """
        Char-level fallback: maps orphaned chars to columns using the
        table's vertical edge x-positions, then appends the reconstructed
        row to initial_rows.

        Called only when the primary crop+re-extract recovery added no rows.
        """
        x0, tbl_top, x1, tbl_bottom = table.bbox

        # Vertical edges that span this table — gives us column boundaries
        col_xs = sorted(set(
            round(e["x0"])
            for e in page.vertical_edges
            if (x0 - 5 <= e["x0"] <= x1 + 5
                and tbl_top - 5 <= e["top"] <= tbl_bottom + 20)
        ))

        if len(col_xs) >= 2:
            num_cols  = len(col_xs) - 1
            col_texts = [""] * num_cols

            for char in sorted(orphaned_chars, key=lambda c: (round(c["top"]), c["x0"])):
                for col_i in range(num_cols):
                    if col_xs[col_i] - 3 <= char["x0"] <= col_xs[col_i + 1] + 3:
                        col_texts[col_i] += char["text"]
                        break

            reconstructed = [t.strip() for t in col_texts]
        else:
            # No vertical lines — treat as single-column row
            raw = "".join(
                c["text"]
                for c in sorted(orphaned_chars, key=lambda c: (c["top"], c["x0"]))
            )
            reconstructed = [raw.strip()]

        return list(initial_rows) + [reconstructed]

    def _extract_page_smart(
        self,
        pdf_path: str,
        page_num: int,
        plumber_pdf: "pdfplumber.PDF",
        doc: "fitz.Document",
        project_id: str
    ) -> tuple[str, str]:
        """
        Returns (extracted_text, method_used).
        method_used: 'pdfplumber_table' | 'pdfplumber_text' | 'vision_scanned' | 'fitz_fallback'
        """
        logging.debug(f"[{project_id}] Extracting Page {page_num+1}...")
        page = plumber_pdf.pages[page_num]

        # ── Table extraction (with dynamic bottom-row recovery) ───────────
        table_text, tables_found = self._extract_tables_with_row_recovery(
            page, project_id, page_num
        )
        if tables_found:
            logger.info(
                f"[{project_id}] Page {page_num+1}: "
                f"pdfplumber_table ({table_text.count(chr(10))+1} rows extracted)."
            )
            prose = page.extract_text() or ""
            full_text = f"{prose}\n\n[TABLE DATA]\n{table_text}".strip()
            return full_text, "pdfplumber_table"

        # ── Plain text ────────────────────────────────────────────────────
        text = page.extract_text() or ""
        if text and len(text.strip()) > 20:
            logger.info(
                f"[{project_id}] Page {page_num+1}: "
                f"Extracted {len(text)} chars via pdfplumber_text."
            )
            return text.strip(), "pdfplumber_text"

        # ── Vision fallback for scanned pages ─────────────────────────────
        fitz_page = doc[page_num]
        fitz_text = fitz_page.get_text().strip()
        has_images = len(fitz_page.get_images()) > 0

        if (not fitz_text or len(fitz_text.strip()) < 50) and has_images:
            logger.warning(
                f"[{project_id}] Page {page_num+1}: "
                f"Scanned page detected. Triggering Vertex Vision..."
            )
            vision_text = self._process_with_vision(pdf_path, page_num, doc, project_id)
            logger.info(
                f"[{project_id}] Page {page_num+1}: "
                f"Vision extraction complete ({len(vision_text)} chars)."
            )
            return vision_text, "vision_scanned"

        logger.info(f"[{project_id}] Page {page_num+1}: Used fitz_fallback.")
        return fitz_text, "fitz_fallback"

    def process_pdf(self, pdf_path: str, project_id: str, context, bucket, key, start_page: int = 0, end_page: Optional[int] = None, total_chunks: int = 1) -> bool:
        """Extract text and embed in batches. Supports page ranges for parallel fan-out."""
        if not os.path.exists(pdf_path):
            logger.error(f"PDF not found: {pdf_path}")
            return False

        logger.info(f"Starting Ingestion for {project_id} (Pages {start_page} to {end_page or 'End'})...")
        doc = fitz.open(pdf_path)
        plumber_pdf = pdfplumber.open(pdf_path)
        try:
            total_pages = len(doc)
            end_page = min(end_page, total_pages) if end_page else total_pages
            
            # Single batch call instead of one call per page
            page_range = list(range(start_page + 1, end_page + 1))
            already_processed = self._get_already_processed_pages(project_id, page_range)

            extracted_pages = []
            start_time = time.time()
            for page_num in range(start_page, end_page):
                # --- TIMEOUT GUARD (13 minutes) ---
                # If we are approaching the 15-minute Lambda limit, we save progress and re-trigger
                if time.time() - start_time > 780: # 13 minutes
                    logger.info(f"Approaching Lambda timeout. Re-triggering for remaining pages {page_num+1}-{end_page}...")
                    
                    if extracted_pages:
                        logger.info(f"[{project_id}] Flushing {len(extracted_pages)} pages before timeout...")
                        batch_size = 15
                        for i in range(0, len(extracted_pages), batch_size):
                            batch = extracted_pages[i:i + batch_size]
                            batch_texts = [p["text"] for p in batch]
                            try:
                                embeddings = self._embed_with_retry(batch_texts)
                                batch_vectors = []
                                for idx, p_data in enumerate(batch):
                                    batch_vectors.append({
                                        "id": p_data["id"],
                                        "values": embeddings[idx].values,
                                        "metadata": {
                                            "project_id": project_id,
                                            "text": p_data["text"][:7500],
                                            "page_num": p_data["page_num"],
                                            "pdf_path": os.path.abspath(pdf_path),
                                            "extraction_method": p_data["extraction_method"],
                                            "chunk_type": p_data["chunk_type"]
                                        }
                                    })
                                if batch_vectors:
                                    self.index.upsert(vectors=batch_vectors)
                            except Exception as e:
                                logger.error(f"[{project_id}] FATAL ERROR flushing batch: {e}")
                    
                    payload = {
                        "is_chunk": True,
                        "start_page": page_num,
                        "end_page": end_page,
                        "bucket": bucket,
                        "key": key,
                        "total_chunks": total_chunks
                    }
                    queue_url = os.environ.get("LOADER_CHUNK_QUEUE_URL")
                    if queue_url:
                        boto3.client('sqs').send_message(
                            QueueUrl=queue_url,
                            MessageBody=json.dumps(payload)
                        )
                        logger.info(f"Timeout guard: queued remaining pages {page_num+1}-{end_page} to SQS.")
                    else:
                        boto3.client('lambda').invoke(
                            FunctionName=context.function_name,
                            InvocationType='Event',
                            Payload=json.dumps(payload)
                        )
                    return False # Exit this execution without marking as done
                
                vector_id = f"{project_id}_p{page_num + 1}"
                if vector_id in already_processed:
                    logger.info(f"Page {page_num+1}: [SKIP - already processed]")
                    continue
                
                try:
                    final_text, method = self._extract_page_smart(pdf_path, page_num, plumber_pdf, doc, project_id)
                    logger.info(f"Page {page_num+1}: [{method.upper()}]")

                    if final_text and len(final_text.strip()) > 10:
                        # 1. Store the full page as before
                        extracted_pages.append({
                            "id": f"{project_id}_p{page_num+1}",
                            "text": final_text,
                            "page_num": page_num + 1,
                            "extraction_method": method,
                            "chunk_type": "page"
                        })
                        
                        # FIX 2: Overflow check if text exceeds 7500 chars
                        if len(final_text) > 7500:
                            overflow_text = final_text[7000:]  # 500-char overlap
                            extracted_pages.append({
                                "id": f"{project_id}_p{page_num+1}_overflow",
                                "text": overflow_text,
                                "page_num": page_num + 1,
                                "extraction_method": method,
                                "chunk_type": "overflow"
                            })
                        
                        # 2. FIX 1: If it's a table or vision-scanned (which often has tables), 
                        # save individual table rows as separate vectors for higher retrieval signal
                        if method in ["pdfplumber_table", "vision_scanned"]:
                            table_rows = [line for line in final_text.split("\n") if " | " in line and len(line) > 10]
                            for row_idx, row_text in enumerate(table_rows):
                                nl_text = convert_table_row_to_natural_language(row_text)
                                extracted_pages.append({
                                    "id": f"{project_id}_p{page_num+1}_row{row_idx}",
                                    "text": nl_text,
                                    "page_num": page_num + 1,
                                    "extraction_method": "table_row_nl",
                                    "chunk_type": "table_row"
                                })
                except Exception as e:
                    logger.error(f"Error extracting page {page_num+1}: {e}")

            # --- BATCH EMBEDDING ---
            if not extracted_pages:
                logger.info(f"[{project_id}] No new pages to process. Either the chunk was completely empty, or this is a retry/ghost worker and all pages were skipped via idempotency.")
                # We return True so the pipeline check-in can still increment chunks_done
                # if this was genuinely an empty chunk. Ghost workers are now blocked by
                # the 30-minute SQS Visibility Timeout.
                return True

            logger.info(f"[{project_id}] Starting embedding for {len(extracted_pages)} pages...")
            batch_size = 15
            for i in range(0, len(extracted_pages), batch_size):
                batch = extracted_pages[i:i + batch_size]
                batch_texts = [p["text"] for p in batch]
                
                logger.info(f"[{project_id}] Embedding Batch {i//batch_size + 1} ({len(batch)} pages)...")
                try:
                    embeddings = self._embed_with_retry(batch_texts)
                    logger.info(f"[{project_id}] Batch {i//batch_size + 1}: Embeddings generated.")
                    
                    batch_vectors = []
                    for idx, p_data in enumerate(batch):
                        batch_vectors.append({
                            "id": p_data["id"],
                            "values": embeddings[idx].values,
                            "metadata": {
                                "project_id": project_id,
                                "text": p_data["text"][:7500],
                                "page_num": p_data["page_num"],
                                "pdf_path": os.path.abspath(pdf_path),
                                "extraction_method": p_data["extraction_method"],
                                "chunk_type": p_data["chunk_type"]
                            }
                        })
                    
                    if batch_vectors:
                        logger.info(f"[{project_id}] Upserting {len(batch_vectors)} vectors to Pinecone...")
                        self.index.upsert(vectors=batch_vectors)
                        logger.info(f"[{project_id}] SUCCESS: Batch {i//batch_size + 1} upserted to Pinecone.")
                except Exception as e:
                    logger.error(f"[{project_id}] FATAL ERROR in Batch {i//batch_size + 1}: {e}")
                    raise e
            
            logger.info(f"[{project_id}] --- PDF PROCESSING COMPLETE --- (Pages {start_page}-{end_page})")
            return True
        finally:
            doc.close()
            plumber_pdf.close()

def lambda_handler(event, context):
    """
    AWS Lambda entry point with Parallel Fan-Out for 1300+ page support.
    """
    try:
        print(f"DEBUG: lambda_handler started with event: {json.dumps(event)}")
        logger.info(f"Raw Lambda Event: {json.dumps(event)}")
        bucket = None
        key = None
        sqs_triggered = False
        
        if 'Records' in event:
            if len(event['Records']) > 1:
                logger.error(f"CRITICAL WARNING: Received {len(event['Records'])} records in batch! Only processing the first one. To prevent data loss of other files, configure AWS Lambda/SQS Trigger Batch Size to 1.")
            record = event['Records'][0]
            if 's3' in record:
                # Direct S3 trigger
                bucket = record['s3']['bucket']['name']
                key = record['s3']['object']['key']
            elif 'body' in record:
                # SQS trigger
                sqs_triggered = True
                body_str = record['body']
                body = json.loads(body_str) if isinstance(body_str, str) else body_str
                
                bucket = body.get('bucket')
                key = body.get('key')
                
                # Fallback: check for nested S3 event
                if not bucket or not key:
                    s3_data = body.get('detail', {}).get('object') or body.get('s3', {}).get('object')
                    if not s3_data and 'Records' in body:
                        s3_data = body['Records'][0].get('s3', {}).get('object')
                    key = s3_data.get('key') if s3_data else None
                    
                    bucket = body.get('detail', {}).get('bucket', {}).get('name') or body.get('s3', {}).get('bucket', {}).get('name')
                    if not bucket and 'Records' in body:
                        bucket = body['Records'][0].get('s3', {}).get('bucket', {}).get('name')
                event = body 
        
        # --- NEW: Handle EventBridge Direct (No Records) ---
        if not bucket or not key:
            if 'detail' in event and 'bucket' in event['detail']:
                bucket = event['detail'].get('bucket', {}).get('name')
                key = event['detail'].get('object', {}).get('key')
            else:
                # Last resort: direct top-level keys
                bucket = bucket or event.get('bucket')
                key = key or event.get('key')

        logger.info(f"Final Parsed: bucket={bucket}, key={key}, sqs={sqs_triggered}")

        if not bucket or not key:
            logger.error(f"FATAL: Malformed event - could not find bucket or key in event structure.")
            return {"statusCode": 400, "body": "Missing bucket or key"}

        # --- LOOP PREVENTION ---
        if not key.lower().endswith(".pdf") or "tags/" in key:
            return {"statusCode": 200, "body": "Ignored non-PDF/tag file."}

        start_page = event.get("start_page", 0)
        end_page = event.get("end_page")
        is_chunk = event.get("is_chunk", False)
        
        project_id = key.split("/")[-1].split(".")[0].upper()

        # RACE CONDITION FIX: Avoid container reuse collisions for files with same name or updated files
        safe_filename = key.replace("/", "_")
        local_path = f"/tmp/{safe_filename}"
        
        s3 = boto3.client('s3')
        
        # If this is a fresh S3 upload (not a fan-out chunk), ALWAYS force a fresh download 
        # to prevent using stale cached files from a previous run.
        if not is_chunk and os.path.exists(local_path):
            os.remove(local_path)

        if not os.path.exists(local_path):
            logger.info(f"Downloading s3://{bucket}/{key} to {local_path} (Project: {project_id})...")
            try:
                s3.download_file(bucket, key, local_path)
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                if error_code == '404':
                    logger.warning(f"File not found in S3 (404 HeadObject): {bucket}/{key}. It may have been deleted or moved.")
                    return {"statusCode": 404, "body": f"File {key} not found"}
                else:
                    logger.error(f"S3 ClientError downloading {key}: {str(e)}")
                    raise e
            logger.info(f"Download complete: {local_path} ({os.path.getsize(local_path)} bytes)")
        
        logger.info(f"Opening PDF with fitz: {local_path}")
        doc = fitz.open(local_path)
        try:
            total_pages = len(doc)
            logger.info(f"PDF opened successfully. Total pages: {total_pages}")
        finally:
            doc.close()
            
        MAX_PAGES_PER_LAMBDA = 80 # Reduced from 100 for 20k token limit safety
        num_chunks = (total_pages + MAX_PAGES_PER_LAMBDA - 1) // MAX_PAGES_PER_LAMBDA

        # --- DB STATE RESET FOR NEW RUNS ---
        if not is_chunk:
            logger.info(f"New Full PDF detected. Checking concurrency guard for {project_id}")
            if not init_pipeline_state(project_id, total_chunks=num_chunks):
                logger.warning(f"Pipeline for {project_id} is already running. Ignoring duplicate S3 event.")
                return {"statusCode": 200, "body": "Ignored duplicate S3 event."}

        # 2. FAN-OUT LOGIC (If not already a chunk)
        if not is_chunk:
            logger.info(f"Starting Fan-out for {project_id}...")
            
            if total_pages > MAX_PAGES_PER_LAMBDA:
                logger.info(
                    f"Fanning out {total_pages} pages into {num_chunks} chunks "
                    f"of {MAX_PAGES_PER_LAMBDA} via SQS..."
                )
                sqs = boto3.client('sqs')
                queue_url = os.environ.get("LOADER_CHUNK_QUEUE_URL")
                
                if not queue_url:
                    logger.error("LOADER_CHUNK_QUEUE_URL env var not set.")
                    # Fallback to direct invoke if URL not set for some reason
                    lambda_client = boto3.client('lambda')
                
                for i in range(num_chunks):
                    chunk_start = i * MAX_PAGES_PER_LAMBDA
                    chunk_end = min((i + 1) * MAX_PAGES_PER_LAMBDA, total_pages)
                    
                    payload = {
                        "is_chunk": True,
                        "bucket": bucket,
                        "key": key,
                        "start_page": chunk_start,
                        "end_page": chunk_end,
                        "total_chunks": num_chunks
                    }
                    
                    if queue_url:
                        sqs.send_message(
                            QueueUrl=queue_url,
                            MessageBody=json.dumps(payload)
                        )
                    else:
                        lambda_client.invoke(
                            FunctionName=context.function_name,
                            InvocationType='Event',
                            Payload=json.dumps(payload)
                        )
                
                msg = f"Queued {num_chunks} chunks to SQS." if queue_url else f"Fanned out {num_chunks} chunks via Invoke."
                return {"statusCode": 200, "body": msg}

        # 3. Process the PDF (Full or Chunk)
        total_chunks = event.get("total_chunks", 1)
        loader = DocumentLoader()
        completed = loader.process_pdf(local_path, project_id, context, bucket, key, start_page, end_page, total_chunks)
        
        if not completed:
            logger.info(f"[{project_id}] Chunk processing partially complete. Re-queued for timeout. Skipping check-in.")
            return {"statusCode": 200, "body": "Timeout re-queued."}
        
        if os.path.exists(local_path):
            os.remove(local_path)

        # 4. Check-in (Handle multi-chunk sync)
        check_in_pipeline_multi(project_id, "loader", bucket, key, total_chunks)
        
        return {"statusCode": 200, "body": "Chunk processed."}

    except Exception as e:
        from src.database.error_logger import error_logger
        error_logger.log_error(project_id if 'project_id' in locals() else "UNKNOWN", "Loader", e)
        return {"statusCode": 500, "body": str(e)}

def check_in_pipeline_multi(project_id: str, component: str, bucket: str, key: str, total_chunks: int):
    """
    Sync Machine — atomic, race-condition-free chunk tracking.
    """
    try:
        # Step 1 & 2: Atomic sync via helpers
        increment_chunk(project_id, total_chunks)
        
        if try_trigger_orchestrator(project_id):
            # Step 3: Trigger fire (Async Lambda Invoke)
            try:
                orchestrator_lambda = os.environ.get("ORCHESTRATOR_FUNCTION_NAME", "Construction-Orchestrator")
                client = boto3.client('lambda')
                
                payload = {
                    "project_id": project_id,
                    "s3_path": f"s3://{bucket}/{key}"
                }
                
                client.invoke(
                    FunctionName=orchestrator_lambda,
                    InvocationType='Event',
                    Payload=json.dumps(payload).encode()
                )
                logger.info(f"Orchestrator successfully triggered via async invoke for {project_id}")
            except Exception as e:
                logger.error(f"Failed to trigger Orchestrator via async invoke: {e}")

    except Exception as e:
        error_logger.log_error(project_id, "Loader_CheckIn", e)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        PDF_PATH = sys.argv[1]
        project_id = sys.argv[2] if len(sys.argv) > 2 else "LOCAL_TEST_001"
        
        # Create a mock context for local testing
        class _MockContext:
            function_name = "local-test"
            def get_remaining_time_in_millis(self):
                return 900000  # 15 minutes
        
        loader = DocumentLoader()
        loader.process_pdf(
            pdf_path=PDF_PATH,
            project_id=project_id,
            context=_MockContext(),
            bucket="local-test-bucket",
            key=f"uploads/{os.path.basename(PDF_PATH)}",
            start_page=0,
            end_page=None,
            total_chunks=1
        )
    else:
        print("Usage: python loader.py <path_to_pdf> [project_id]")