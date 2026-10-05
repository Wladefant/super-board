"""Select the issues a wave may start."""
from __future__ import annotations

from issue_gateway import IssueGateway


def ready_issues(client):
    gateway = IssueGateway(client)
    return gateway.list_issues(state="open", label="status:ready")
