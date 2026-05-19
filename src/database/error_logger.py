import os
import logging
import traceback
from typing import Optional
from supabase import create_client, Client

logger = logging.getLogger(__name__)

class ErrorLogger:
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if self.url and self.key:
            self.client: Client = create_client(self.url, self.key)
        else:
            self.client = None

    def log_error(self, project_id: Optional[str], component: str, error: Exception, severity: str = "ERROR"):
        pid = project_id or "GLOBAL"
        error_msg = str(error)
        stack_trace = traceback.format_exc()
        
        logger.error(f"[{component}] CRITICAL ERROR: {error_msg}")
        
        if self.client:
            try:
                self.client.table("error_logs").insert({
                    "project_id": pid,
                    "component": component,
                    "error_message": error_msg,
                    "stack_trace": stack_trace,
                    "severity": severity
                }).execute()
            except Exception as e:
                logger.error(f"Failed to write to ErrorLogs table: {e}")

# Global Instance
error_logger = ErrorLogger()
