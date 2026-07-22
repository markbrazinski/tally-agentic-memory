.PHONY: tick test db-verify ui-dev ui-build ui-test

# Operator's first command of every session: last 3 days' manifest status
# per registered source (CLAUDE.md demo requirement #1). Defaults AWS_PROFILE
# to "tally" inside capture/tick.py itself, but honors it if already set.
tick:
	.venv/bin/python -m capture.tick

test:
	.venv/bin/pytest

# Applies any pending migrations, then reads back live schema state and
# asserts every expected table/column actually exists on the cluster —
# the "deploy isn't done until read-back confirms it" standing lock,
# applied to schema migrations.
db-verify:
	.venv/bin/python -m src.external.db_verify

ui-dev:
	npm --prefix ui run dev

ui-build:
	npm --prefix ui run build

ui-test:
	npm --prefix ui test
