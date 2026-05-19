import json
import boto3
import os
import logging
from src.database.supabase_db import supabase, init_pipeline_state

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Ingestion Controller.
    Triggered by SQS (Inbound PDFs from S3).
    """
    try:
        if 'Records' not in event:
            records = [{'body': json.dumps(event)}]
        else:
            records = event['Records']

        for record in records:
            try:
                body = json.loads(record['body']) if isinstance(record['body'], str) else record['body']

                # Short-circuit for loader chunks
                if body.get("is_chunk"):
                    logger.info("[CHUNK] Routing to Loader...")
                    loader_function_name = os.environ.get(
                        "LOADER_FUNCTION_NAME", "Construction-Loader"
                    )
                    boto3.client('lambda').invoke(
                        FunctionName=loader_function_name,
                        InvocationType='Event',
                        Payload=json.dumps(body).encode()
                    )
                    continue

                # Parse S3 Key
                s3_data = (
                    body.get('detail', {}).get('object')
                    or body.get('s3', {}).get('object')
                )
                if not s3_data:
                    s3_data = body.get('Records', [{}])[0].get('s3', {}).get('object')

                key = s3_data.get('key') if s3_data else "UNKNOWN"
                bucket = (
                    body.get('detail', {}).get('bucket', {}).get('name')
                    or body.get('s3', {}).get('bucket', {}).get('name')
                )
                if not bucket:
                    bucket = body.get('Records', [{}])[0].get('s3', {}).get('bucket', {}).get('name')

                if not key.lower().endswith(".pdf") or "tags/" in key:
                    logger.info(f"Skipping: {key}")
                    continue

                project_id = key.split("/")[-1].split(".")[0].upper()
                logger.info(f"=== PROCESSING PROJECT: {project_id} ===")

                # Step 0: Initialize pipeline state
                try:
                    init_pipeline_state(project_id)
                    logger.info(f"[{project_id}] State initialized.")
                except Exception as state_e:
                    logger.warning(f"[{project_id}] State init failed: {state_e}")

                # Step 1: Trigger Loader (async)
                try:
                    loader_function_name = os.environ.get(
                        "LOADER_FUNCTION_NAME", "Construction-Loader"
                    )
                    boto3.client('lambda').invoke(
                        FunctionName=loader_function_name,
                        InvocationType='Event',
                        Payload=json.dumps(body).encode()
                    )
                    logger.info(f"[{project_id}] Loader invoked async.")
                except Exception as loader_e:
                    logger.error(f"[{project_id}] Loader invoke failed: {loader_e}")

                # Step 2: Trigger Router (async)
                try:
                    router_function_name = os.environ.get("ROUTER_FUNCTION_NAME")
                    if router_function_name:
                        boto3.client('lambda').invoke(
                            FunctionName=router_function_name,
                            InvocationType='Event',
                            Payload=json.dumps(body).encode()
                        )
                        logger.info(f"[{project_id}] Router invoked async.")
                    else:
                        logger.error(
                            f"[{project_id}] ROUTER_FUNCTION_NAME not set. "
                            f"Router NOT triggered."
                        )
                except Exception as router_e:
                    logger.error(f"[{project_id}] Router invoke failed: {router_e}")

            except Exception as inner_e:
                logger.error(f"Record processing error: {inner_e}")
                continue

        return {"statusCode": 200, "body": "Ingestion complete."}

    except Exception as e:
        logger.error(f"CRITICAL FAILURE: {e}")
        return {"statusCode": 500, "body": str(e)}
