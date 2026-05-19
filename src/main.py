from src.agents.router_agent import RouterAgent
from src.agents.extractor_worker import ExtractorWorker
from src.database.ledger_db import LedgerDB
import os

def run_pipeline(project_id: str, raw_document_texts: list):
    """
    Main orchestration loop for the Blueprint-AI Extraction Engine.
    Implements the Fan-Out architecture to prevent data corruption.
    """
    print(f"--- Starting Pipeline for Project: {project_id} ---")
    
    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)
    
    # 1. DISCOVERY PHASE: Find all unique finish tags
    router = RouterAgent()
    discovered_tags = router.discover_all_tags(raw_document_texts)
    print(f"Router discovered {len(discovered_tags)} tags to extract.")
    
    # 2. PERSISTENCE SETUP: Connect to the Truth Ledger
    db = LedgerDB()
    
    # 3. EXTRACTION PHASE (The Fan-Out): One worker per tag
    worker = ExtractorWorker()
    extracted_count = 0
    
    for tag in discovered_tags:
        print(f"Processing Tag: {tag}...")
        
        # Step A: (Simulated) Retrieve ONLY evidence related to this specific tag from Pinecone
        # In production, this would be: evidence = pinecone.query(filter={"tag": tag})
        simulated_evidence = f"Source text containing detailed specs for {tag} by Wilsonart/Mohawk..."
        
        # Step B: Extract in isolation
        row_data = worker.extract_details(tag, simulated_evidence)
        
        # Step C: Save directly to the Ledger (truth layer)
        success = db.insert_row(project_id, row_data)
        if success:
            extracted_count += 1
            
    print(f"--- Pipeline Finished ---")
    print(f"Successfully extracted and persisted {extracted_count} first-class spec rows.")
    print("Database location: data/ledger.db")

if __name__ == "__main__":
    # Test sample with multiple tags to demonstrate fan-out
    sample_text = [
        "Finish Schedule Table: PL-1, PL-2, LVT-1, CPT-1 are required for the lobby.",
        "Note: PL-400 is only for adhesive use in rough carpentry."
    ]
    run_pipeline("AW_HALL_001", sample_text)
