import os
import logging
from supabase import create_client, Client

logger = logging.getLogger(__name__)

class LedgerDB:
    """
    Manages the Spec Detail Ledger via Supabase.
    Prevents duplicate rows via DB-level unique constraints.
    """
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if self.url and self.key:
            self.client: Client = create_client(self.url, self.key)
        else:
            self.client = None
            logger.warning("Supabase credentials missing for LedgerDB.")

    def upsert_row(self, project_id: str, row_data: dict):
        """
        Upserts a row into the cloud ledger. 
        Returns True on success, False on error.
        """
        if not self.client:
            logger.error("Cannot upsert row: Supabase client not initialized.")
            return False

        # Prepare payload from row_data (SpecDetailRow model)
        payload = {
            "project_id": project_id,
            "area_scope": row_data.get("area_scope", "N/S"),
            "csi_division": row_data.get("csi_division", "N/S"),
            "csi_section": row_data.get("csi_section", "N/S"),
            "finish_tag": row_data.get("finish_tag"),
            "manufacturer": row_data.get("manufacturer", "N/S"),
            "model_series": row_data.get("model_series", "N/S"),
            "finish_color": row_data.get("finish_color", "N/S"),
            "size": row_data.get("size", "N/S"),
            "product_criteria": row_data.get("product_criteria", "N/S"),
            "installation_criteria": row_data.get("installation_criteria", "N/S"),
            "standards_codes": row_data.get("standards_codes", "N/S"),
            "finish_type": row_data.get("finish_type", "N/S"),
            "special_remarks": row_data.get("special_remarks", "N/S"),
            "source_document": row_data.get("source_document"),
            "page_reference": row_data.get("page_reference"),
            "evidence_id": row_data.get("evidence_id"),
            "evidence_excerpt": row_data.get("evidence_excerpt"),
            "confidence": row_data.get("confidence", 0.0),
            "status": row_data.get("status", "confirmed"),
            "extraction_method": row_data.get("extraction_method", "text")
        }

        try:
            self.client.table("spec_detail_ledger").upsert(
                payload, on_conflict="project_id,finish_tag,area_scope"
            ).execute()
            logger.info(f"Row {payload['finish_tag']} upserted to Cloud Ledger.")
            return True
        except Exception as e:
            logger.error(f"Failed to upsert ledger row: {e}")
            return False

    def insert_row(self, project_id: str, row_data: dict):
        """
        Alias of upsert_row for compatibility across callers.
        """
        return self.upsert_row(project_id, row_data)

# Global Instance
ledger_db = LedgerDB()
