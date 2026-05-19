import os
import logging
from supabase import create_client, Client
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

class TokenLedger:
    """
    Tracks and persists Token Cost / Usage per Agent.
    Logs the data to the 'token_ledger' table in Supabase.
    """
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if self.url and self.key:
            self.client: Client = create_client(self.url, self.key)
        else:
            self.client = None
            logger.warning("Supabase credentials missing. TokenTracker will run in memory (DRY RUN).")

    def log_usage(self, project_id: str, agent_name: str, model_name: str, input_tokens: Optional[int], output_tokens: Optional[int]):
        """
        Logs token usage to Supabase. Handles None values to prevent crashes.
        """
        i_tokens = input_tokens or 0
        o_tokens = output_tokens or 0
        total_tokens = i_tokens + o_tokens

        data = {
            "project_id": project_id,
            "agent_name": agent_name,
            "model_name": model_name,
            "input_tokens": i_tokens,
            "output_tokens": o_tokens
        }
        
        if self.client:
            try:
                self.client.table("token_usage_ledger").insert(data).execute()
                logger.info(f"TokenUsageLedger updated: [{agent_name}] used {total_tokens} tokens.")
            except Exception as e:
                logger.error(f"Failed to write to TokenUsageLedger: {e}")
        else:
            logger.info(f"[DRY RUN - TokenLedger] {project_id} | {agent_name} ({model_name}): {total_tokens} total tokens.")

# Singleton instance
token_ledger_db = TokenLedger()
