"""
One place that picks "the right cloud client for this row's provider", used by every call site that
destroys/acts on a sensor without already knowing which cloud created it (jobs.py, ttl_manager.py,
api.py). Avoids duplicating the same if/else across all of them.
"""

from typing import Optional, Union

from app.do_client import DOClient
from app.gcp_client import GCPClient
from app.config import get_do_token, get_gcp_project_id


def get_client(provider: str, token_override: Optional[str] = None) -> Union[DOClient, GCPClient]:
    if provider == "gcp":
        return GCPClient(project_id=get_gcp_project_id())
    return DOClient(token=token_override or get_do_token())
