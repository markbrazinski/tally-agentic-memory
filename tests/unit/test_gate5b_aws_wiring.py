from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_deployment_uses_oauth_provider_without_static_mcp_credential_mapping():
    deploy = (ROOT / "deploy_app.sh").read_text(encoding="utf-8")
    source_config = deploy[deploy.index("SOURCE_CONFIG=") :]
    assert "TALLY_OAUTH_TOKEN_PARAMETER" in source_config
    assert "TALLY_OAUTH_REFRESH_LEASE_TABLE" in source_config
    assert "parameter\"+$prefix+\"/mcp-api-key" not in source_config
    assert "service-account-api-key-write-denied" not in source_config
    assert "gate5b_deployment_preflight" in deploy


def test_runtime_iam_can_rotate_only_one_bundle_and_one_lease_table():
    provision = (ROOT / "scripts/gate5_provision_aws.sh").read_text(encoding="utf-8")
    assert 'Sid:"RotateOneOAuthBundle"' in provision
    assert 'Action:["ssm:GetParameter","ssm:PutParameter"]' in provision
    assert 'Sid:"CoordinateOneOAuthRefresh"' in provision
    assert 'Action:["dynamodb:PutItem","dynamodb:DeleteItem"]' in provision
    assert '"dynamodb:LeadingKeys":[$oauth]' in provision
    assert 'Action:["ssm:PutParameter"],Resource:("arn:aws:ssm:"' not in provision
    assert 'parameter"+$prefix+"/*"' not in provision


def test_app_runner_is_one_worker_and_one_instance():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy_app.sh").read_text(encoding="utf-8")
    assert '"--workers", "1"' in dockerfile
    assert "--min-size 1 --max-size 1" in deploy


def test_budget_is_scoped_to_gate5_services_not_the_whole_account():
    guardrails = (ROOT / "scripts/gate5_guardrails.sh").read_text(encoding="utf-8")
    assert '"CostFilters":{"Service":[' in guardrails
    for service in (
        "AWS App Runner",
        "Amazon Elastic Container Registry (ECR)",
        "Amazon DynamoDB",
        "AWS Systems Manager",
        "Amazon Simple Storage Service",
        "Amazon Bedrock",
        "Amazon EventBridge",
    ):
        assert service in guardrails


def test_budget_notifications_and_teardown_match_the_current_owner_authorization():
    guardrails = (ROOT / "scripts/gate5_guardrails.sh").read_text(encoding="utf-8")
    assert 'BUDGET_LIMIT="50"' in guardrails
    assert '"ThresholdType":"ABSOLUTE_VALUE"' in guardrails
    for threshold in (15, 25, 40, 50):
        assert f"ensure_notification {threshold}" in guardrails
    assert "create-budget-action" not in guardrails
    assert "execute-budget-action" not in guardrails
    assert 'INITIAL_TEARDOWN_DATE="2026-09-30"' in guardrails
    assert "delta % 7 == 0" in guardrails
    assert 'TEARDOWN_EXPRESSION="at(${TEARDOWN_DATE}T23:59:00)"' in guardrails
