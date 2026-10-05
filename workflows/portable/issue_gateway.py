"""Issue gateway: the one place the wave planner reads GitHub issues."""
from __future__ import annotations


class IssueGateway:
    """Wraps the GitHub client for the wave planner."""

    def __init__(self, client):
        self.client = client

    def get_issue(self, number):
        return self.client.get_issue(number)

    def list_issues(self, state="open", label=None):
        return self.client.list_issues(state=state, label=label)

    def get_comments(self, number):
        return self.client.get_comments(number)

    def close_issue(self, number):
        return self.client.close_issue(number)
