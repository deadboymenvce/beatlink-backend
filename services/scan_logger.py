import os
import logging
import requests

logger = logging.getLogger(__name__)


class ScanLogger:
    """Records one row per scan-pipeline stage into the Supabase `scan_logs` table so the
    frontend admin can see EXACTLY what happened during a scan.

    Rows are buffered in memory during the scan and flushed in a single batch at the end
    (call flush() from a finally block), so logging adds no per-stage latency and can never
    slow down or break the scan. No-ops silently when scan_id or the SUPABASE_* env vars are
    absent, so scans keep working even before logging is configured on Render.

    Requires two env vars on the backend (Render):
      SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    """

    def __init__(self, scan_id, youtube_url):
        self.scan_id = scan_id
        self.youtube_url = youtube_url
        self.seq = 0
        self.rows = []
        self.base = os.getenv("SUPABASE_URL")
        self.key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        self.enabled = bool(scan_id and self.base and self.key)

    def log(self, stage, message, level="info", data=None):
        self.seq += 1
        # Keep it in Render's own logs too (visible even if Supabase logging is off).
        logger.info(f"[scan {self.scan_id}] {stage} · {message}")
        if not self.enabled:
            return
        self.rows.append({
            "scan_id": self.scan_id,
            "youtube_url": self.youtube_url,
            "seq": self.seq,
            "stage": stage,
            "level": level,
            "message": message,
            "data": data,
        })

    def warn(self, stage, message, data=None):
        self.log(stage, message, level="warn", data=data)

    def error(self, stage, message, data=None):
        self.log(stage, message, level="error", data=data)

    def flush(self):
        if not self.enabled or not self.rows:
            return
        try:
            requests.post(
                f"{self.base}/rest/v1/scan_logs",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=self.rows,
                timeout=10,
            )
        except Exception as e:  # never let logging break a completed scan
            logger.warning(f"[scan {self.scan_id}] scan_logs flush failed: {e}")
        finally:
            self.rows = []
