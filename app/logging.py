import json
import logging


def event(action: str, **ids):
    # Only explicit metadata; never raw provider payloads, tokens or customer text.
    allowed = {
        k: str(v)
        for k, v in ids.items()
        if k in {"tenant_id", "conversation_id", "message_id", "job_id", "error_type", "count"}
    }
    logging.getLogger("barrio").info(json.dumps({"event": action, **allowed}))
