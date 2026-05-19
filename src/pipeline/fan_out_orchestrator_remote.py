import os
from typing import Optional, List, Dict, Any
import logging
import json
import boto3
from botocore.exceptions import ClientError
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from supabase import create_client, Client
import random
from dotenv import load_dotenv

from src.database.token_ledger import token_ledger_db
from src.database.supabase_db import supabase

load_dotenv()

# Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s [MASTER-ORCHESTRATOR] %(message)s')
logger = logging.getLogger("Orchestrator")

class FanOutOrchestratorRemote:
    """
    The "Master Brain" of the AWS Pipeline.
    - Wakes up on S3/Ingestion completion.
    - Performs Pre-flight Deduplication (Supabase).
    - Dispatches Standard Workers (Parallel) & Fallback Agents (Parallel).
    - Performs S3 Isolation/Cleanup on completion.
    """
    def __init__(self, project_id: str, bucket_name: str, s3_key: str):
        self.project_id = project_id
        self.bucket = bucket_name
        self.s3_key = s3_key # The original PDF key
        
        self.s3 = boto3.client('s3')
        self.sqs = boto3.client('sqs')
        self.supabase = supabase

    def _wait_for_readiness(self, tags_key: str, timeout=60):
        logger.info(f"[READINESS] Waiting for tags.json for project {self.project_id}...")
        for i in range(12):
            try:
                self.s3.head_object(Bucket=self.bucket, Key=tags_key)
                logger.info(f"[READINESS] SUCCESS: Found {tags_key} after {i*5}s.")
                return True
            except ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code in ('404', 'NoSuchKey'):
                    logger.debug(f"[READINESS] Still waiting for {tags_key}... (Retry {i+1}/12)")
                    time.sleep(5)
                else:
                    logger.error(f"[READINESS] S3 permission/bucket error: {e}")
                    return False
            except Exception as e:
                logger.error(f"[READINESS] Unexpected error: {e}")
                return False

        logger.error(f"[READINESS] FAILURE: tags.json not found within 60s for {self.project_id}.")
        return False

    def get_already_processed_tags(self):
        try:
            logger.info(f"[DEDUPE] Checking existing records for project: {self.project_id}")
            res = self.supabase.table("spec_detail_ledger").select("finish_tag").eq("project_id", self.project_id).execute()
            count = len(res.data) if res.data else 0
            logger.info(f"[DEDUPE] Found {count} already processed tags.")
            return {row['finish_tag'] for row in res.data}
        except Exception as e:
            logger.error(f"[DEDUPE] FAILED: {e}")
            return set()

    def warm_db_schema(self):
        """Pre-flight database call to warm the PostgREST schema cache."""
        try:
            logger.info("[WARMUP] Warming PostgREST schema cache...")
            self.supabase.table("spec_detail_ledger").select("id").limit(1).execute()
            time.sleep(1.5)
            logger.info("[WARMUP] Database cache warmed successfully.")
        except Exception as e:
            logger.warning(f"[WARMUP] Database warming query failed (non-fatal): {e}")

    def dispatch_task(self, tag: str, pages: list, total_tags: int, tags_key: str, approved_tags: list, router_metadata: dict):
        logger.info(f"[{tag}] PREPARING DISPATCH: Target Pages: {pages}")
        queue_url = os.environ.get("WORKER_QUEUE_URL")
        if not queue_url:
            logger.error(f"[{tag}] DISPATCH ABORTED: WORKER_QUEUE_URL is not set.")
            return None

        payload = {
            "tag": tag,
            "project_id": self.project_id,
            "target_pages": pages,
            "s3_path": f"s3://{self.bucket}/{self.s3_key}",
            "source_doc": os.path.basename(self.s3_key),
            "total_tags": total_tags,
            "tags_key": tags_key,
            "approved_tags": approved_tags,
            "router_metadata": router_metadata
        }

        try:
            logger.info(f"[{tag}] SENDING to Standard Queue: {queue_url}")
            self.sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(payload)
            )
            logger.info(f"[{tag}] DISPATCH SUCCESS: Message in SQS.")
            return tag
        except Exception as e:
            logger.error(f"[{tag}] DISPATCH FAILED: {e}")
            return None

    # The isolate_and_cleanup method was removed. Archiving is now handled by the final worker.

    def run(self):
        """Main Loop."""
        # Warm PostgREST schema cache to prevent PGRST204 under high concurrency
        self.warm_db_schema()

        tags_key = f"tags/{self.project_id}_tags.json"
        
        # 1. Wake-up Wait
        if not self._wait_for_readiness(tags_key):
            logger.error("Orchestrator timed out waiting for data. Aborting.")
            return

        # 2. Load Tags
        response = self.s3.get_object(Bucket=self.bucket, Key=tags_key)
        tags_data = json.loads(response['Body'].read().decode('utf-8'))
        tags = tags_data.get("tags", [])
        tag_to_pages = tags_data.get("page_map_with_buffer", tags_data.get("page_map", {})) # Format: {"PL-1": [3, 4, 5], ...}
        
        # 3. Deduplication
        processed_tags = self.get_already_processed_tags()
        pending_tags = [t for t in tags if t not in processed_tags]
        logger.info(f"Deduplication: {len(processed_tags)} skipped, {len(pending_tags)} to process.")

        # 4. Fan-Out (Parallel Standard + Parallel Fallback)
        results = []
        # Reduced max_workers to 5 to prevent Vertex AI 429 quota exhaustion
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_tag = {
                executor.submit(
                    self.dispatch_task,
                    tag,
                    tag_to_pages.get(tag, []),
                    len(tags),
                    tags_key,
                    tags,
                    tags_data.get("metadata", {}).get(tag, {})
                ): tag
                for tag in pending_tags
            }
            for future in as_completed(future_to_tag):
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(f"[{future_to_tag[future]}] Thread crashed: {e}")
                    results.append(None)

        # 5. Final Cleanup
        successful = [r for r in results if r is not None]
        total = len(pending_tags)
        success_rate = len(successful) / total if total > 0 else 1.0

        # NOTE: success_rate = SQS queue delivery rate.
        # Worker processing success is tracked separately by workers 
        # themselves via Supabase spec_detail_ledger.
        # We archive only if ALL tags were successfully queued.
        MIN_SUCCESS_RATE = 1.0  # All tags must be queued before archiving

        if success_rate >= MIN_SUCCESS_RATE:
            logger.info(
                f"All {total} tags queued to SQS. "
                f"Archiving deferred to final worker completion."
            )
        else:
            failed_count = total - len(successful)
            logger.error(
                f"Dispatch incomplete. {failed_count}/{total} tags failed to queue. "
                f"Check SQS/IAM permissions."
            )

def lambda_handler(event, context):
    """Entry point for AWS Lambda Wake-up Call."""
    # The event might be direct or from a Function URL (body)
    if 'body' in event:
        body_str = event['body']
        event = json.loads(body_str) if isinstance(body_str, str) else body_str
        
    # Startup check — correct variable, correct location
    if not os.environ.get("WORKER_QUEUE_URL"):
        return {
            "statusCode": 500,
            "body": "WORKER_QUEUE_URL is not set in Lambda environment variables."
        }

    project_id = event.get("project_id")
    s3_path = event.get("s3_path") # Format: s3://bucket/key
    bucket = None
    s3_key = None

    # Handle EventBridge trigger (S3 Object Created for tags.json)
    if 'detail' in event and 'bucket' in event['detail']:
        bucket = event['detail']['bucket']['name']
        s3_key = event['detail']['object']['key']
        # e.g., tags/FINAL CONSTRUCTION BUSINESS_tags.json
        if not project_id:
            project_id = s3_key.split('/')[-1].replace('_tags.json', '')
        s3_path = f"s3://{bucket}/{s3_key}"
    elif s3_path:
        bucket = s3_path.replace("s3://", "").split("/")[0]
        s3_key = "/".join(s3_path.replace("s3://", "").split("/")[1:])

    if not s3_path or not bucket or not s3_key:
        logger.error(f"Missing s3_path or EventBridge payload in event: {event}")
        return {"statusCode": 400, "body": "Missing s3_path or valid payload"}

    
    try:
        orchestrator = FanOutOrchestratorRemote(project_id, bucket, s3_key)
        orchestrator.run()
        return {"statusCode": 200, "body": f"Orchestrator execution finished for {project_id}"}
    except Exception as e:
        from src.database.error_logger import error_logger
        error_logger.log_error(project_id, "Orchestrator", e)
        return {"statusCode": 500, "body": str(e)}

if __name__ == "__main__":
    # Local Test
    # python fan_out_orchestrator_remote.py <project_id> <bucket> <s3_key>
    import sys
    if len(sys.argv) > 3:
        orchestrator = FanOutOrchestratorRemote(sys.argv[1], sys.argv[2], sys.argv[3])
        orchestrator.run()
    else:
        print("Usage: python fan_out_orchestrator_remote.py <project_id> <bucket> <s3_key>")
