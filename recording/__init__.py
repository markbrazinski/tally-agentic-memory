"""Tally's recording package: the DB commit path (Bundle R Session 3).

Turns S3 raw captures (capture/handler.py, Sessions 1-2) into committed
CockroachDB rows. See recording/commit.py for the library; callable from
two entry points per docs/bundle-r.md Session 3: the Lambda (daily, going
forward) and restore_live.py (replay, built separately, consumes this
module's public functions).
"""
