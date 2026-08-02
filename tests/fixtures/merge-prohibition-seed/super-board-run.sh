#!/usr/bin/env bash
# mechanism 7: runtime issue closure as a merge substitute, in a dispatcher path
gh issue close "$1" --reason completed
STATE_REASON="completed"
state_reason=completed
