import json
import os
import logging
import traceback
from src.agents.extractor_worker import ExtractorWorker
from src.utils.self_healing_json import SelfHealingJSONParser
from src.database.ledger_db import ledger_db
from src.database.token_ledger import token_ledger_db

# Production-grade logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

parser = SelfHealingJSONParser()

def _retrieve_evidence(tag: str, project_id: str) -> list:
    """
    Worker-owned Pinecone retrieval.
    Moved here from Orchestrator to prevent Lambda timeout.
    Uses GLOBAL_GEMINI_LOCK to share the 5 RPM budget with FallbackWorkers.
    """
    from google import genai
    from google.genai import types
    from pinecone import Pinecone
    from src.utils.gcp_helper import setup_gcp_credentials
    from src.utils.rate_limiter import GlobalRateLimiter

    setup_gcp_credentials()
    
    limiter = GlobalRateLimiter("GLOBAL_GEMINI_LOCK", rpm_limit=5, token_limit=20000)
    limiter.acquire()  # Shared lock with FallbackAgent

    genai_client = genai.Client(
        vertexai=True,
        project=os.environ.get("GCP_PROJECT_ID"),
        location=os.environ.get("GCP_LOCATION", "us-central1"),
    )
    res = genai_client.models.embed_content(
        model="text-embedding-004",
        contents=[tag.replace("\n", " ")],
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=768
        )
    )
    
    pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
    index = pc.Index(os.environ.get("PINECONE_INDEX", "construction-vertex-index"))
    results = index.query(
        vector=res.embeddings[0].values,
        filter={"project_id": project_id},
        top_k=5,
        include_metadata=True
    )
    return [
        {
            "text": m.metadata.get("text", ""),
            "page_num": int(m.metadata.get("page_num", 0)),
            "chunk_id": m.id
        }
        for m in results.matches if m.score > 0.6
    ]

def handler(event, context):
    """
    Production-Ready AWS Lambda Handler.
    Crash-proof architecture to ensure 502 errors do not occur.
    """
    logger.info("=== STANDARD WORKER INVOCATION STARTED ===")
    
    try:
        # 1. Safely Parse Input
        if 'body' in event:
            body_str = event['body']
        elif 'Records' in event and len(event['Records']) > 0:
            body_str = event['Records'][0]['body']
        else:
            raise ValueError("Invalid event format.")

        body = json.loads(body_str) if isinstance(body_str, str) else body_str
        
        target_tag = body.get('tag')
        target_pages = body.get('target_pages', [])   # ADD — needed for fallback routing
        s3_path = body.get('s3_path', '')             # ADD — needed for fallback routing
        project_id = body.get('project_id', 'UNKNOWN_PROJECT')
        source_doc = body.get('source_doc', 'UNKNOWN_DOC')

        if not target_tag:
            raise ValueError("Missing 'tag' in request payload.")

        # Worker retrieves its own evidence. No longer handed to us.
        try:
            evidence_chunks = _retrieve_evidence(target_tag, project_id)
            logger.info(f"[{target_tag}] Retrieved {len(evidence_chunks)} evidence chunks.")
        except Exception as e:
            logger.error(f"[{target_tag}] Pinecone retrieval failed: {e}. Routing to Fallback.")
            evidence_chunks = []

        # CRITICAL ROUTING DECISION: Empty evidence = Fallback Queue, not local retry
        if not evidence_chunks:
            try:
                import boto3
                fallback_queue_url = os.environ.get("FALLBACK_QUEUE_URL")
                if not fallback_queue_url:
                    raise ValueError("FALLBACK_QUEUE_URL environment variable not set.")
                
                boto3.client('sqs').send_message(
                    QueueUrl=fallback_queue_url,
                    MessageBody=json.dumps({
                        "tag": target_tag,
                        "project_id": project_id,
                        "target_pages": target_pages,
                        "s3_path": s3_path,
                        "source_doc": source_doc,
                        "is_fallback": True,
                        "total_tags": body.get("total_tags", 0),
                        "tags_key": body.get("tags_key", ""),
                        "approved_tags": body.get("approved_tags", []),
                        "router_metadata": body.get("router_metadata", {})
                    })
                )
                logger.info(f"[{target_tag}] No evidence found. Routed to FallbackWorker queue.")
                return {"statusCode": 200, "body": json.dumps({"status": "routed_to_fallback", "tag": target_tag})}
            except Exception as e:
                logger.error(f"[{target_tag}] Fallback routing failed: {e}")
                raise  # Let Lambda retry this message

        logger.info(f"[{target_tag}] Starting extraction.")

        # 2. Run the Extractor Worker
        worker = ExtractorWorker(google_api_key=os.environ.get("GEMINI_API_KEY"))
        result_row = worker.extract(
            target_tag=target_tag,
            evidence_chunks=evidence_chunks,
            source_document=source_doc
        )

        # 3. Clean and Validate using Self-Healing Engine
        raw_data = result_row.model_dump() if hasattr(result_row, 'model_dump') else result_row
        healed_data = parser.parse(json.dumps(raw_data))

        # 4. BUSINESS AUDIT: Persist Data and Log Usage
        # Extract row data from parser output
        final_row = healed_data.get("data", raw_data)
        
        # Save to DB
        db_success = ledger_db.insert_row(project_id, final_row)
        
        # Log Tokens (Note: ExtractorWorker uses Instructor which doesn't directly expose tokens easily, 
        # but we estimate or pass them if available. For now, we ensure persistence is primary.)
        if db_success:
            logger.info(f"[{target_tag}] Data successfully persisted to LedgerDB.")
        else:
            raise RuntimeError(f"[{target_tag}] Failed to persist data to LedgerDB.")

        # --- FINALIZATION CHECK ---
        total_tags = body.get("total_tags", 0)
        tags_key = body.get("tags_key", "")
        approved_tags = body.get("approved_tags", [])
        if total_tags > 0:
            from src.database.supabase_db import check_worker_finalization, init_pipeline_state, reconcile_tags
            if check_worker_finalization(project_id, total_tags):
                logger.info(f"[{target_tag}] THIS IS THE LAST WORKER (Standard). Triggering S3 Archive and State Reset.")
                
                # 0. Reconcile tags
                try:
                    reconciliation = reconcile_tags(project_id, approved_tags)
                    logger.info(f"[{target_tag}] Tag Reconciliation Result: {reconciliation}")
                except Exception as rec_e:
                    logger.error(f"[{target_tag}] Tag Reconciliation failed: {rec_e}")

                # 1. Archive S3 files
                bucket = s3_path.replace("s3://", "").split("/")[0] if s3_path else None
                s3_key = "/".join(s3_path.replace("s3://", "").split("/")[1:]) if s3_path else None
                
                if bucket and s3_key and tags_key:
                    import boto3
                    from datetime import datetime
                    archive_prefix = f"archive/{datetime.now().strftime('%Y-%m-%d')}/{project_id}/"
                    for k in [s3_key, tags_key]:
                        try:
                            dest_key = archive_prefix + os.path.basename(k)
                            boto3.client('s3').copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': k}, Key=dest_key)
                            boto3.client('s3').delete_object(Bucket=bucket, Key=k)
                            logger.info(f"Archived {k} to {dest_key}")
                        except Exception as e:
                            logger.error(f"Archive failed for {k}: {e}")
                            
                # 2. Reset Pipeline State for Future Uploads
                init_pipeline_state(project_id, total_chunks=0)
                logger.info(f"[{target_tag}] Pipeline state reset complete.")

        response_data = {
            "status": "success",
            "tag": target_tag,
            "persisted": db_success,
            "data": final_row
        }

        logger.info(f"[{target_tag}] Extraction process finished.")
        
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response_data)
        }

    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}\n{traceback.format_exc()}")
        raise e

