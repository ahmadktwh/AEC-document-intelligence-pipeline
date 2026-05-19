import os
import logging
import json
import boto3
import time
import threading
from botocore.exceptions import ClientError
import fitz
import pdfplumber
from google import genai
from google.genai import types
from PIL import Image
import io

from src.utils.self_healing_json import SelfHealingJSONParser
from src.database.token_ledger import token_ledger_db
from src.database.ledger_db import ledger_db
from src.utils.gcp_helper import setup_gcp_credentials
from src.utils.rate_limiter import GlobalRateLimiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FallbackWorker")

class FallbackAgent:
    """
    Advanced Fallback Agent using Hybrid Engine:
    1. Page Classification (Text vs Scanned)
    2. PDFPlumber for Structured Table Extraction (Markdown)
    3. Gemini 2.5 Pro Vision for Visual Verification
    """
    def __init__(self):
        # Setup GCP Credentials for Vertex AI
        setup_gcp_credentials()
            
        self.client = genai.Client(
            vertexai=True,
            project=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "us-central1")
        )
        self.parser = SelfHealingJSONParser()
        self.model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro")
        self.gemini_limiter = GlobalRateLimiter("GLOBAL_GEMINI_LOCK", rpm_limit=5, token_limit=20000)

    def _get_page_as_image_dict(self, doc: fitz.Document, page_num: int) -> dict:
        """Returns raw dictionary for Gemini Vision (Most stable)."""
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        return {
            "inline_data": {
                "data": pix.tobytes("png"),
                "mime_type": "image/png"
            }
        }

    def _extract_tables_as_markdown(self, plumber_pdf, page_num: int) -> str:
        """Extracts tables using an already open pdfplumber instance."""
        try:
            page = plumber_pdf.pages[page_num - 1]
            tables = page.extract_tables()
            if not tables:
                return ""
            
            md_output = ""
            for i, table in enumerate(tables):
                md_output += f"\nTable {i+1}:\n"
                for row in table:
                    clean_row = [str(cell).replace("\n", " ").strip() if cell else "" for cell in row]
                    md_output += "| " + " | ".join(clean_row) + " |\n"
            return md_output
        except Exception as e:
            logger.error(f"PDFPlumber failed on p{page_num}: {e}")
            return ""

    def process(self, tag, project_id, s3_path, pages, source_document, router_metadata: dict = None):
        """
        Executes a deep hybrid scan (Vision + Markdown Tables + Raw OCR) for a specific tag.
        Returns parsed SpecDetailRow dict.
        """
        router_metadata = router_metadata or {}
        hints = []
        def _is_valid_meta(val):
            return val and val not in ("N/S", "", "none", "None", "null")

        if _is_valid_meta(router_metadata.get("manufacturer")):
            hints.append(f"Router pre-identified manufacturer: {router_metadata['manufacturer']}")
        if _is_valid_meta(router_metadata.get("product_model")):
            hints.append(f"Router pre-identified model/series: {router_metadata['product_model']}")
        if _is_valid_meta(router_metadata.get("color")):
            hints.append(f"Router pre-identified color/finish: {router_metadata['color']}")
        
        hint_clause = f"\nROUTING HINTS:\n" + "\n".join(hints) if hints else ""

        logger.info(f"[{tag}] HYBRID DEEP SCAN INITIATED. Pages: {pages}")
        
        s3 = boto3.client('s3')
        bucket = s3_path.replace("s3://", "").split("/")[0]
        key = "/".join(s3_path.replace("s3://", "").split("/")[1:])
        local_path = f"/tmp/fallback_{tag}.pdf"
        logger.info(f"[{tag}] S3: Downloading {s3_path} for Hybrid Scan...")
        s3.download_file(bucket, key, local_path)
        logger.info(f"[{tag}] S3: Download complete.")

        context_data = []
        image_parts = []
        
        fitz_doc = fitz.open(local_path)
        plumber_pdf = pdfplumber.open(local_path)

        best_result = None
        best_confidence = 0.0

        try:
            for p_num in pages:  # Scan ALL pages from buffer
                # 1. Vision Part (Raw Dict - Solid Proof)
                image_dict = self._get_page_as_image_dict(fitz_doc, p_num)

                # 2. Markdown Table Part
                md_table = self._extract_tables_as_markdown(plumber_pdf, p_num)
                context_data = []
                if md_table:
                    context_data.append(f"--- PAGE {p_num} TABLE DATA ---\n{md_table}")

                # 3. Raw Text Part
                raw_text = fitz_doc[p_num - 1].get_text()
                if raw_text:
                    context_data.append(f"--- PAGE {p_num} RAW TEXT ---\n{raw_text[:2000]}")

                combined_context = "\n\n".join(context_data)

                # 4. Prompt
                prompt = f"""
                [HYBRID DEEP SCAN PROTOCOL]
                Target Tag: {tag}
                Project: {project_id}
                Page: {p_num}
                
                Locate and extract EVERY technical detail for '{tag}'.
                {hint_clause}
                
                STRUCTURED DATA:
                {combined_context}
                
                VISUAL DATA: (See attached image)
                
                STRICT JSON SCHEMA:
                {{
                    "finish_tag": "{tag}",
                    "model_series": "...",
                    "manufacturer": "...",
                    "finish_color": "...",
                    "size": "...",
                    "product_criteria": "...",
                    "installation_criteria": "...",
                    "standards_codes": "...",
                    "area_scope": "...",
                    "page_reference": "{p_num}",
                    "confidence": 0.99
                }}
                """

                # Global Rate Limit
                self.gemini_limiter.acquire()
                
                try:
                    response = self.client.models.generate_content(
                        model=self.model_id,
                        contents=[prompt, image_dict],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            http_options=types.HttpOptions(timeout=120000)
                        )
                    )
                except Exception as vision_err:
                    logger.error(f"[{tag}] Hybrid Vision failed: {vision_err}. Falling back to Text-Only mode.")
                    response = self.client.models.generate_content(
                        model=self.model_id,
                        contents=[prompt + "\n\n(VISION FAILED - PLEASE ANALYZE TEXT AND TABLES ONLY)"],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            http_options=types.HttpOptions(timeout=120000)
                        )
                    )

                # Parse and Compare
                current_result = self.parser.parse(response.text)
                
                # Log tokens
                try:
                    usage = getattr(response, 'usage_metadata', None)
                    if usage:
                        token_ledger_db.log_usage(
                            project_id=project_id,
                            agent_name="FallbackAgent_Hybrid",
                            model_name=self.model_id,
                            input_tokens=getattr(usage, 'prompt_token_count', 0),
                            output_tokens=getattr(usage, 'candidates_token_count', 0)
                        )
                except: pass

                if current_result and current_result.get("status") != "not_found":
                    conf = current_result.get("confidence", 0.0)
                    if conf > best_confidence:
                        best_confidence = conf
                        best_result = current_result
                    
                    if best_confidence >= 0.7: # Good enough, stop early
                        break

            return best_result or {"status": "not_found"}

        except Exception as e:
            logger.error(f"Hybrid Error in Fallback: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            try:
                fitz_doc.close()
                plumber_pdf.close()
            except: pass
            if os.path.exists(local_path):
                os.remove(local_path)

class SQSHeartbeat(threading.Thread):
    def __init__(self, sqs_client, queue_url, receipt_handle, tag, interval=300):
        super().__init__()
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.tag = tag
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.receipt_handle,
                    VisibilityTimeout=900
                )
                logger.info(f"[{self.tag}] SQS Visibility extended (Heartbeat).")
            except Exception as e:
                logger.error(f"[{self.tag}] Heartbeat failed: {e}")
                break

    def stop(self):
        self.stop_event.set()


def _is_standard_pipeline_active():
    """Checks if Standard-Worker-Queue has any pending or in-flight messages."""
    try:
        sqs = boto3.client('sqs')
        queue_url = os.environ.get("WORKER_QUEUE_URL")
        if not queue_url:
            return False
            
        attrs = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible']
        ).get('Attributes', {})
        
        pending = int(attrs.get('ApproximateNumberOfMessages', 0))
        inflight = int(attrs.get('ApproximateNumberOfMessagesNotVisible', 0))
        
        if (pending + inflight) > 0:
            logger.info(f"[GATEKEEPER] Standard Queue ACTIVE: {pending} pending, {inflight} in-flight. Fallback must wait.")
        else:
            logger.info(f"[GATEKEEPER] Standard Queue EMPTY. Safe to proceed with Fallback.")
            
        return (pending + inflight) > 0
    except Exception as e:
        logger.error(f"Error checking Standard Queue status: {e}")
        return False

def lambda_handler(event, context):
    """
    SQS-triggered Lambda handler for Fallback Worker.
    Uses batchItemFailures so failed messages go to DLQ, not silently dropped.
    """
    failed_message_ids = []

    sqs = boto3.client('sqs')
    fallback_queue_url = os.environ.get("FALLBACK_QUEUE_URL")

    for record in event.get("Records", []):
        # [SEQUENTIAL GATEKEEPER]
        if _is_standard_pipeline_active():
            logger.warning("Standard pipeline still active. Deferring Fallback processing.")
            failed_message_ids.append({"itemIdentifier": record.get("messageId")})
            continue

        message_id = record.get("messageId", "unknown")
        project_id = "UNKNOWN"
        tag = "UNKNOWN"
        heartbeat = None
        try:
            logger.info(f"=== FALLBACK WORKER: {message_id} ===")
            body_str = record.get("body", "{}")
            body = json.loads(body_str) if isinstance(body_str, str) else body_str

            tag = body.get("tag")
            s3_path = body.get("s3_path")
            pages = body.get("target_pages", [])
            project_id = body.get("project_id", "UNKNOWN")
            source_doc = body.get("source_doc", "UNKNOWN_DOC")
            total_tags = body.get("total_tags", 0)
            tags_key = body.get("tags_key", "")
            receipt_handle = record.get("receiptHandle")
            router_metadata = body.get("router_metadata", {})

            # Validate that this tag was actually approved by Router
            approved_tags = body.get("approved_tags", [])
            if approved_tags and tag not in approved_tags:
                logger.error(f"[{tag}] NOT in Router's approved tag list. Skipping fallback to prevent corrupt record.")
                continue

            # [HEARTBEAT START]
            if fallback_queue_url and receipt_handle:
                heartbeat = SQSHeartbeat(sqs, fallback_queue_url, receipt_handle, tag)
                heartbeat.start()
                logger.info(f"[{tag}] Heartbeat started.")

            if not tag or not s3_path:
                logger.error(f"[{tag}] ABORTED: Missing required fields in SQS message.")
                raise ValueError(f"Missing required fields. Tag: {tag}, S3Path: {s3_path}")

            logger.info(f"[{tag}] HYBRID SCAN START: Pages {pages}")
            agent = FallbackAgent()
            result = agent.process(tag, project_id, s3_path, pages, source_doc, router_metadata)
            logger.info(f"[{tag}] HYBRID SCAN COMPLETE. Status: {result.get('status') if result else 'UNKNOWN'}")

            if result and result.get("status") == "error":
                # True errors → DLQ for retry
                logger.error(f"[{tag}] Agent returned error: {result}")
                failed_message_ids.append({"itemIdentifier": message_id})
            elif result and result.get("status") == "not_found":
                # Tag not in PDF — log and discard, don't DLQ
                logger.warning(f"[{tag}] Not found in PDF after hybrid scan. Logged and discarded.")
                ledger_db.upsert_row(project_id, {"finish_tag": tag, "status": "not_found", "project_id": project_id, "source_document": source_doc})
            else:
                from supabase import create_client
                sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
                result["project_id"] = project_id
                result["source_document"] = source_doc
                if "material_name" in result:
                    result["model_series"] = result.pop("material_name")
                sb.table("spec_detail_ledger").upsert(
                    result, on_conflict="project_id,finish_tag,area_scope"
                ).execute()
                logger.info(f"[{tag}] Persisted to spec_detail_ledger.")

            # --- FINALIZATION CHECK ---
            from src.database.supabase_db import check_worker_finalization, init_pipeline_state, reconcile_tags
            if check_worker_finalization(project_id, total_tags):
                logger.info(f"[{tag}] THIS IS THE LAST WORKER. Triggering S3 Archive and State Reset.")
                
                # 0. Reconcile tags
                try:
                    reconciliation = reconcile_tags(project_id, approved_tags)
                    logger.info(f"[{tag}] Tag Reconciliation Result: {reconciliation}")
                except Exception as rec_e:
                    logger.error(f"[{tag}] Tag Reconciliation failed: {rec_e}")

                # 1. Archive S3 files
                bucket = s3_path.replace("s3://", "").split("/")[0] if s3_path else None
                s3_key = "/".join(s3_path.replace("s3://", "").split("/")[1:]) if s3_path else None
                
                if bucket and s3_key and tags_key:
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
                logger.info(f"[{tag}] Pipeline state reset complete.")

        except Exception as e:
            logger.error(f"[{tag}] Fatal error: {e}")
            from src.database.error_logger import error_logger
            error_logger.log_error(project_id, f"FallbackWorker_{tag}", e)
            failed_message_ids.append({"itemIdentifier": message_id})
        finally:
            if heartbeat:
                heartbeat.stop()
                heartbeat.join()
                logger.info(f"[{tag}] Heartbeat stopped.")

    return {"batchItemFailures": failed_message_ids}
