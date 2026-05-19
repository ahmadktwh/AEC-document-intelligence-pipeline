import os
import logging
from supabase import create_client, Client

# Configure logger
logger = logging.getLogger(__name__)

# Initialize Supabase Client globally
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not url or not key:
    logger.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment variables.")
    # We don't raise here to allow importing for other reasons, but we check later
    supabase: Client = None
else:
    supabase: Client = create_client(url, key)

def init_pipeline_state(project_id: str, total_chunks: int = 0):
    """Initializes the pipeline state in the database. Safe for concurrent runs."""
    if not supabase: return
    try:
        # Check if already processing
        state_res = supabase.table("pipeline_state").select("loader_done, chunks_done, total_chunks").eq("project_id", project_id).execute()
        if state_res.data and len(state_res.data) > 0:
            data = state_res.data[0]
            is_processing = not data.get("loader_done", False) and data.get("chunks_done", 0) > 0 and data.get("total_chunks", 0) > 0
            if is_processing:
                logger.warning(f"State for {project_id} exists and is currently processing. Skipping reset.")
                return False

        supabase.table("pipeline_state").upsert({
            "project_id": project_id,
            "loader_done": False,
            "router_done": False,
            "orchestrator_triggered": False,
            "chunks_done": 0,
            "total_chunks": total_chunks
        }, on_conflict="project_id").execute()
        logger.info(f"Initialized pipeline state for {project_id} (Total: {total_chunks})")
        return True
    except Exception as e:
        logger.error(f"Failed to init pipeline state: {e}")
        return False

def check_worker_finalization(project_id: str, total_tags: int) -> bool:
    """
    Checks if all assigned tags have been completed by comparing against spec_detail_ledger.
    If yes, returns True indicating this worker is the last to finish.
    """
    if not supabase: return False
    try:
        res = supabase.table("spec_detail_ledger").select("id", count="exact").eq("project_id", project_id).execute()
        processed_count = res.count if hasattr(res, 'count') and res.count is not None else len(res.data)
        logger.info(f"Worker Finalization Check [{project_id}]: {processed_count}/{total_tags} tags completed.")
        return processed_count >= total_tags and total_tags > 0
    except Exception as e:
        logger.error(f"Failed to check finalization: {e}")
        return False

def increment_chunk(project_id: str, total_chunks: int) -> bool:
    """
    Calls the Supabase RPC to atomically increment chunks_done.
    Returns True if this was the final chunk (loader_done=True).
    """
    if not supabase: return False
    try:
        # The RPC should handle the increment and return the updated row or a boolean
        res = supabase.rpc("increment_loader_chunk", {
            "proj_id": project_id,
            "total": total_chunks
        }).execute()
        
        # After increment, check if we are done
        check = supabase.table("pipeline_state").select("loader_done").eq("project_id", project_id).execute()
        is_done = check.data[0].get("loader_done", False) if check.data and len(check.data) > 0 else False
        
        logger.info(f"Incremented chunk for {project_id}. Done: {is_done}")
        return is_done
    except Exception as e:
        logger.error(f"Failed to increment chunk: {e}")
        return False

def try_trigger_orchestrator(project_id: str) -> bool:
    """
    Atomic check-and-update to see if orchestrator should be triggered.
    Only returns True for exactly ONE lambda instance.
    """
    if not supabase: return False
    try:
        res = (
            supabase.table("pipeline_state")
            .update({"orchestrator_triggered": True})
            .eq("project_id", project_id)
            .eq("loader_done", True)
            .eq("router_done", True)
            .eq("orchestrator_triggered", False)
            .execute()
        )
        return bool(res.data and len(res.data) > 0)
    except Exception as e:
        logger.error(f"Failed to trigger orchestrator: {e}")
        return False

def reconcile_tags(project_id: str, expected_tags: list) -> dict:
    """
    Compares the expected list of tags from S3 against what has actually been 
    written to the database (spec_detail_ledger).
    Logs detailed mismatch info and returns a status dictionary.
    """
    if not supabase: 
        return {"status": "error", "message": "Supabase client not initialized"}
    try:
        logger.info(f"[RECONCILIATION] Starting tag reconciliation for project: {project_id}")
        res = supabase.table("spec_detail_ledger").select("finish_tag").eq("project_id", project_id).execute()
        db_tags = {row["finish_tag"] for row in res.data} if res.data else set()
        
        expected_set = set(expected_tags)
        missing_tags = expected_set - db_tags
        extra_tags = db_tags - expected_set
        
        reconciliation_status = {
            "status": "success" if not missing_tags else "mismatch",
            "total_expected": len(expected_set),
            "total_actual": len(db_tags),
            "missing_tags": list(missing_tags),
            "extra_tags": list(extra_tags),
            "match_percentage": (len(db_tags & expected_set) / len(expected_set) * 100) if expected_set else 100.0
        }
        
        if missing_tags:
            logger.warning(
                f"[RECONCILIATION] MISMATCH DETECTED for project {project_id}: "
                f"{len(missing_tags)} tags missing. Missing tags: {list(missing_tags)}"
            )
        else:
            logger.info(f"[RECONCILIATION] ALL {len(expected_set)} TAGS ACCOUNTED FOR successfully in the database.")
            
        return reconciliation_status
    except Exception as e:
        logger.error(f"[RECONCILIATION] Error during reconciliation: {e}")
        return {"status": "error", "message": str(e)}
