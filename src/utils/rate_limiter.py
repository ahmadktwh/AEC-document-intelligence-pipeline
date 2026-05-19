import os
import time
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

# Setup logging
logger = logging.getLogger("GlobalLimiter")

class GlobalRateLimiter:
    """
    Supabase-backed distributed rate limiter for cross-Lambda synchronization.
    Enforces RPM (via time interval) and Token Budget (via ledger check).
    """
    def __init__(self, lock_id: str, rpm_limit: int = 5, token_limit: int = 20000, project_id: str = "GLOBAL"):
        self.lock_id = lock_id
        self.interval = 60 / rpm_limit
        self.token_limit = token_limit
        self.project_id = project_id
        
        from src.database.supabase_db import supabase
        self.supabase = supabase

    def _get_last_call_at(self) -> datetime:
        if not self.supabase:
            return datetime.fromtimestamp(0, tz=timezone.utc)
            
        try:
            res = self.supabase.table("pipeline_state").select("updated_at").eq("project_id", self.lock_id).execute()
            if res.data and len(res.data) > 0 and res.data[0].get("updated_at"):
                ts = res.data[0]["updated_at"].replace("Z", "+00:00")
                return datetime.fromisoformat(ts)
            else:
                # Initialize the lock row if it doesn't exist
                try:
                    self.supabase.table("pipeline_state").insert({
                        "project_id": self.lock_id,
                        "updated_at": datetime.fromtimestamp(0, tz=timezone.utc).isoformat(),
                        "loader_done": True,
                        "router_done": True,
                        "orchestrator_triggered": True,
                        "chunks_done": 0,
                        "total_chunks": 0
                    }).execute()
                except Exception:
                    pass # Already exists or concurrency issue
        except Exception as e:
            logger.error(f"[{self.lock_id}] Failed to get last call: {e}")
        return datetime.fromtimestamp(0, tz=timezone.utc)

    def _check_token_budget(self) -> bool:
        """Checks if the total token usage in the last minute is under the limit. (With Retry)"""
        if not self.supabase:
            return True # Fail open locally
            
        for attempt in range(3):
            try:
                one_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                res = self.supabase.table("token_usage_ledger").select("input_tokens, output_tokens").gte("created_at", one_min_ago).execute()
                total = sum((r.get("input_tokens", 0) + r.get("output_tokens", 0)) for r in res.data)
                
                is_ok = total < self.token_limit
                if not is_ok:
                    logger.warning(f"[{self.lock_id}] Token budget EXCEEDED: {total} / {self.token_limit}")
                return is_ok
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"[{self.lock_id}] Token check attempt {attempt+1} failed: {e}. Retrying...")
                    time.sleep(1)
                    continue
                logger.error(f"[{self.lock_id}] Token check failed after 3 attempts: {e}")
                return True # Fail open

    def acquire(self):
        """Waits for a slot and token budget availability."""
        while True:
            last_call = self._get_last_call_at()
            now = datetime.now(timezone.utc)
            seconds_since = (now - last_call).total_seconds()

            if seconds_since >= self.interval:
                if not self._check_token_budget():
                    time.sleep(5)
                    continue

                # Atomic Update Lock
                if not self.supabase:
                    return # Local bypass
                    
                try:
                    res = self.supabase.table("pipeline_state")\
                        .update({"updated_at": now.isoformat()})\
                        .eq("project_id", self.lock_id)\
                        .eq("updated_at", last_call.isoformat())\
                        .execute()
                    
                    if res.data and len(res.data) > 0:
                        logger.info(f"[{self.lock_id}] Lock ACQUIRED ({60/self.interval} RPM enforced).")
                        return 
                except Exception:
                    pass # Someone else grabbed it
            
            # Wait a bit
            wait_time = max(1, min(self.interval - seconds_since, 3)) + random.uniform(0.2, 1.0)
            logger.info(f"[{self.lock_id}] Waiting {wait_time:.1f}s for slot...")
            time.sleep(wait_time)
