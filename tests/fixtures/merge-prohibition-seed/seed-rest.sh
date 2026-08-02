#!/usr/bin/env bash
# mechanism 2: repository merge REST endpoints
curl -X PUT "https://api.github.com/repos/o/r/pulls/42/merge"
curl -X POST "https://api.github.com/repos/o/r/merges"
