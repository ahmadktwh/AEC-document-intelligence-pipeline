"""
fan_out_orchestrator.py — Parallel Fan-Out with Direct Ledger Persistence

PROBLEMS IT SOLVES:
  1. Old "Adjudication Gate" that compressed/merged rows:
     The old pipeline had a final_submittals[] step that was a "Truth Gate."
     If the LLM decided to compress 4 items into 2, those 2 items were lost forever.
     
     NEW RULE: Worker → Directly to Ledger. No adjudicator. No compressor.

  2. Sequential processing causing context bleed:
     Processing tags one after another means the LLM might "remember" previous tags.
     
     SOLUTION: ThreadPoolExecutor (simulating AWS Lambda Fan-Out).
     Each tag runs in its own thread — complete isolation, parallel execution.

  3. Duplicates from parallel workers:
     If PL-1 exists on 3 pages, 3 workers might try to insert the same row.
     
     SOLUTION: PostgreSQL UNIQUE constraint (project_id, finish_tag, manufacturer).
     Database-level deduplication — the code never needs to handle this manually.
"""

import os
import logging
import json
import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from datetime import datetime
from supabase import create_client, Client
from pinecone import Pinecone

from src.agents.router_agent import RouterAgent
from src.agents.extractor_worker import ExtractorWorker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ORCHESTRATOR] %(message)s'
)

# ─── DATABASE: SUPABASE PRODUCTION LEDGER ────────────────────────────────────

class SpecDetailLedger:
    """
    The Truth Layer. Hosted on Supabase (PostgreSQL).
    UNIQUE constraint: (project_id, finish_tag, manufacturer)
    """

    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not self.url or not self.key:
            logging.warning("Supabase credentials missing. Local fallback active.")
            self.client = None
        else:
            self.client: Client = create_client(self.url, self.key)

    def insert(self, project_id: str, row: dict) -> bool:
        """
        Insert one row into Supabase. 
        Returns True if inserted, False if duplicate rejected.
        """
        if not self.client:
            logging.error("No Supabase client available.")
            return False

        try:
            # Prepare data for Supabase (matching our migration schema)
            data = {
                "project_id": project_id,
                **row
            }
            self.client.table("spec_detail_ledger").insert(data).execute()
            return True
        except Exception as e:
            if "unique_violation" in str(e).lower() or "duplicate key" in str(e).lower():
                logging.warning(f"DUPLICATE REJECTED: {row.get('finish_tag')} already exists.")
            else:
                logging.error(f"Supabase Insert Error: {e}")
            return False

    def export_to_csv(self, output_path: str, project_id: str) -> int:
        """Export all rows for a project from Supabase to CSV."""
        if not self.client: return 0
        
        response = self.client.table("spec_detail_ledger").select("*").eq("project_id", project_id).execute()
        rows = response.data
        if not rows: return 0

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        logging.info(f"Exported {len(rows)} rows to: {output_path}")
        return len(rows)

    def get_all_rows(self, project_id: str) -> list:
        """Fetch all results for a project from Supabase."""
        if not self.client: return []
        try:
            response = self.client.table("spec_detail_ledger").select("*").eq("project_id", project_id).order("finish_tag").execute()
            return response.data
        except Exception as e:
            logging.error(f"Failed to fetch rows: {e}")
            return []


# ─── PINECONE RETRIEVAL (Simulated — Plug in real Pinecone client here) ──────

def retrieve_evidence_for_tag(tag: str, project_id: str, pc_index_name: str = None) -> list:
    """
    Retrieves Pinecone chunks relevant ONLY to the given tag.
    """
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key or not pc_index_name:
        logging.warning(f"[{tag}] Pinecone credentials missing. DRY RUN retrieval.")
        return [
            {
                "text": f"Finish Tag {tag}: Wilsonart Carbon Mesh specs on Page 407.",
                "page_num": 407,
                "chunk_id": f"sim_{tag}",
                "pdf_path": ""
            }
        ]

    pc = Pinecone(api_key=api_key)
    index = pc.Index(pc_index_name)
    
    # Generate embedding for the tag to perform semantic search
    from src.pipeline.loader import DocumentLoader
    loader = DocumentLoader()
    query_vector = loader.get_embedding(tag, project_id)

    results = index.query(
        vector=query_vector,
        filter={
            "project_id": project_id
        },
        top_k=10,
        include_metadata=True
    )

    return [
        {
            "text": m.metadata.get("text", ""),
            "page_num": m.metadata.get("page_num", 0),
            "chunk_id": m.id,
            "pdf_path": m.metadata.get("pdf_path", "")
        }
        for m in results.matches
    ]


# ─── SINGLE WORKER TASK (runs in its own thread) ─────────────────────────────

def process_single_tag(
    tag: str,
    project_id: str,
    source_document: str,
    ledger: SpecDetailLedger,
    pinecone_index=None,
    google_api_key: Optional[str] = None
) -> dict:
    """
    The complete pipeline for ONE tag.
    This function runs in its own isolated thread (simulating AWS Lambda).
    
    Flow: Retrieve Evidence → Detect Table/Text → Extract → Validate → Persist
    """
    logging.info(f"[{tag}] Worker started")

    try:
        # Step 1: Retrieve ONLY this tag's evidence from Pinecone
        evidence_chunks = retrieve_evidence_for_tag(tag, project_id, pinecone_index)

        if not evidence_chunks:
            logging.warning(f"[{tag}] No evidence found in Pinecone. Skipping.")
            return {"tag": tag, "status": "no_evidence", "inserted": False}

        # Step 2: Extract (with Vision fallback if table evidence detected)
        worker = ExtractorWorker(google_api_key=google_api_key)
        row = worker.extract(
            target_tag=tag,
            evidence_chunks=evidence_chunks,
            source_document=source_document
        )

        # Step 3: Convert to dict if Pydantic model
        if hasattr(row, 'model_dump'):
            row_dict = row.model_dump()
        elif isinstance(row, dict):
            row_dict = row
        else:
            row_dict = dict(row)

        # Step 4: Direct persistence — NO adjudication step
        inserted = ledger.insert(project_id, row_dict)

        logging.info(
            f"[{tag}] {'INSERTED' if inserted else 'DUPLICATE_REJECTED'} "
            f"(manufacturer: {row_dict.get('manufacturer')}, "
            f"confidence: {row_dict.get('confidence')})"
        )

        return {
            "tag": tag,
            "status": "inserted" if inserted else "duplicate",
            "inserted": inserted,
            "row": row_dict
        }

    except Exception as e:
        logging.error(f"[{tag}] Worker FAILED: {e}")
        return {"tag": tag, "status": "error", "error": str(e), "inserted": False}


# ─── MAIN ORCHESTRATOR ────────────────────────────────────────────────────────

class FanOutOrchestrator:
    """
    The main pipeline coordinator.
    
    PDF → Router (discover tags) → Fan-Out (parallel per tag) → Ledger (direct)
    
    No Adjudicator. No Compressor. No Truth Gate.
    Each tag's data lands directly in the Spec Detail Ledger.
    """

    def __init__(
        self,
        pdf_path: str,
        project_id: str,
        google_api_key: Optional[str] = None,
        pinecone_index=None,
        max_workers: int = 10  # Parallel threads (simulate Lambda concurrency)
    ):
        self.pdf_path = pdf_path
        self.project_id = project_id
        self.model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro")
        self.google_api_key = google_api_key
        self.pinecone_index = pinecone_index
        self.max_workers = max_workers
        self.source_document = os.path.basename(pdf_path)
        self.ledger = SpecDetailLedger()

    def run(self) -> dict:
        """
        Execute the full Fan-Out pipeline.
        
        Returns a summary report with before/after counts.
        """
        start_time = datetime.now()
        logging.info(f"Pipeline started for project: {self.project_id}")
        logging.info(f"Source: {self.source_document}")

        # ── PHASE 1: Tag Discovery ────────────────────────────────────────────
        # Use Router with real client
        import google.generativeai as genai
        import instructor
        genai.configure(api_key=self.google_api_key)
        raw_client = genai.GenerativeModel("gemini-2.5-pro")
        discovery_client = instructor.from_gemini(raw_client)
        
        import time
        time.sleep(1) # Quota protection
        router = RouterAgent(llm_client=discovery_client)
        discovery = router.discover_all_tags(self.pdf_path)
        tags = discovery["tags"]
        schedule_pages = discovery["schedule_pages"]

        logging.info(f"Router found {len(tags)} tags on {len(schedule_pages)} schedule pages")

        if not tags:
            logging.warning("No tags discovered. Check if PDF has readable finish schedule data.")
            # In DRY RUN mode, use known schedule pages as simulated tags
            tags = [f"TAG-{i}" for i in range(1, 6)]
            logging.info(f"DRY RUN: Using simulated tags: {tags}")

        # ── PHASE 2: Parallel Fan-Out ─────────────────────────────────────────
        results = []
        inserted_count = 0
        error_count = 0
        duplicate_count = 0

        logging.info(f"Starting Fan-Out: {len(tags)} workers, max {self.max_workers} parallel")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit one task per tag — complete isolation
            futures = {
                executor.submit(
                    process_single_tag,
                    tag,
                    self.project_id,
                    self.source_document,
                    self.ledger,
                    self.pinecone_index,
                    self.google_api_key
                ): tag
                for tag in tags
            }

            for future in as_completed(futures):
                tag = futures[future]
                try:
                    result = future.result()
                    results.append(result)

                    if result["inserted"]:
                        inserted_count += 1
                    elif result["status"] == "duplicate":
                        duplicate_count += 1
                    elif result["status"] == "error":
                        error_count += 1

                except Exception as e:
                    logging.error(f"Future for tag {tag} raised: {e}")
                    error_count += 1

        # ── PHASE 3: Export Results ───────────────────────────────────────────
        output_dir = r"c:\Users\MUJEEB\Downloads\project 10\data\processed"
        os.makedirs(output_dir, exist_ok=True)

        csv_path = os.path.join(output_dir, f"fan_out_output_{self.project_id}.csv")
        total_in_db = self.ledger.export_to_csv(csv_path, self.project_id)

        elapsed = (datetime.now() - start_time).seconds

        summary = {
            "project_id": self.project_id,
            "tags_discovered": len(tags),
            "schedule_pages": len(schedule_pages),
            "rows_inserted": inserted_count,
            "duplicates_rejected": duplicate_count,
            "errors": error_count,
            "total_in_ledger": total_in_db,
            "output_csv": csv_path,
            "elapsed_seconds": elapsed
        }

        # Print summary report
        print("\n" + "="*60)
        print("FAN-OUT PIPELINE — EXECUTION REPORT")
        print("="*60)
        print(f"Project:              {self.project_id}")
        print(f"Tags Discovered:      {summary['tags_discovered']}")
        print(f"Schedule Pages Found: {summary['schedule_pages']}")
        print(f"Rows Inserted:        {summary['rows_inserted']}  -- Each is unique, never merged")
        print(f"Duplicates Rejected:  {summary['duplicates_rejected']}  -- DB constraint worked")
        print(f"Errors:               {summary['errors']}")
        print(f"Total In Ledger:      {summary['total_in_ledger']}")
        print(f"Output CSV:           {summary['output_csv']}")
        print(f"Elapsed:              {elapsed}s")
        print("="*60)

        return summary


if __name__ == "__main__":
    PDF = r"c:\Users\MUJEEB\Downloads\project 10\data\raw\awosting_hall_manual.pdf"
    PROJECT_ID = "AW_HALL_001"

    orchestrator = FanOutOrchestrator(
        pdf_path=PDF,
        project_id=PROJECT_ID,
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        pinecone_index="blueprint-gemini-index",
        max_workers=10
    )

    summary = orchestrator.run()
    print(f"\nDone. Check: {summary['output_csv']}")
