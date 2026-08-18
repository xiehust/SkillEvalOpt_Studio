#!/usr/bin/env python3
"""Idempotent setup for the SkillOpt AgentCore exec runner infrastructure.

Creates/updates: workspace S3 bucket, ECR repo, execution role, and the
AgentCore runtime (PUBLIC network + managed session storage at /mnt/workspace).
The container image is built/pushed by scripts/agentcore/build_and_push.sh
(run automatically unless --no-build).

Usage:
  python3 scripts/agentcore/setup_infra.py            # full setup (incl. image build)
  python3 scripts/agentcore/setup_infra.py --no-build # infra only, reuse pushed image
  python3 scripts/agentcore/setup_infra.py --smoke    # ping the deployed runtime
  python3 scripts/agentcore/setup_infra.py --teardown # delete the runtime (keeps bucket/ECR/role)

Prints the .env snippet on success and writes outputs/agentcore/runtime_info.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SKILLOPT_AGENTCORE_REGION", "us-west-2")
RUNTIME_NAME = "skillopt_exec_worker"
ECR_REPO = "skillopt-agentcore-worker"
ROLE_NAME = "SkillOptAgentCoreExecRole"
MOUNT_PATH = "/mnt/workspace"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INFO_PATH = os.path.join(ROOT, "outputs", "agentcore", "runtime_info.json")

# Defaults injected into the worker container; explicit --model flags override
# the claude default per invocation.
RUNTIME_ENV = {
    "AWS_REGION": REGION,
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-5",
    "ANTHROPIC_SMALL_FAST_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "CLAUDE_CODE_EXEC_USE_SDK": "cli",
    "CODEX_EXEC_USE_SDK": "cli",
}


def account_id() -> str:
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def ensure_bucket(s3, acct: str) -> str:
    name = f"skillopt-agentcore-{acct}-{REGION}"
    try:
        s3.head_bucket(Bucket=name)
        print(f"bucket exists: {name}")
    except ClientError:
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        print(f"bucket created: {name}")
    return name


def ensure_ecr(ecr) -> str:
    try:
        repo = ecr.describe_repositories(repositoryNames=[ECR_REPO])["repositories"][0]
        print(f"ecr repo exists: {repo['repositoryUri']}")
    except ClientError:
        repo = ecr.create_repository(repositoryName=ECR_REPO)["repository"]
        print(f"ecr repo created: {repo['repositoryUri']}")
    return repo["repositoryUri"]


def ensure_role(iam, acct: str, bucket: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": acct}},
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrAuth",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": f"arn:aws:ecr:{REGION}:{acct}:repository/{ECR_REPO}",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": "*",
            },
            {
                "Sid": "Telemetry",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData",
                ],
                "Resource": "*",
            },
            {
                "Sid": "BedrockModels",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
            },
            {
                "Sid": "Workspace",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        print(f"role exists: {role['Arn']}")
    except ClientError:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for the SkillOpt AgentCore exec worker",
        )["Role"]
        print(f"role created: {role['Arn']}")
        time.sleep(10)  # IAM propagation before first runtime creation
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="skillopt-exec-worker",
        PolicyDocument=json.dumps(policy),
    )
    return role["Arn"]


def build_image(repo_uri: str) -> str:
    script = os.path.join(ROOT, "scripts", "agentcore", "build_and_push.sh")
    subprocess.run(["bash", script, repo_uri, "latest"], check=True)
    return f"{repo_uri}:latest"


def _runtime_request(image_uri: str, role_arn: str) -> dict:
    return {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": RUNTIME_ENV,
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 300,
            "maxLifetime": 28800,
        },
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": MOUNT_PATH}}],
    }


def find_runtime(control) -> dict | None:
    paginator = control.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == RUNTIME_NAME:
                return rt
    return None


def wait_ready(control, runtime_id: str) -> None:
    for _ in range(60):
        rt = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            print("runtime READY")
            return
        if status.endswith("FAILED"):
            raise SystemExit(f"runtime {status}: {rt.get('failureReason', rt)}")
        print(f"runtime status: {status} ...")
        time.sleep(10)
    raise SystemExit("timed out waiting for runtime READY")


def ensure_runtime(control, image_uri: str, role_arn: str) -> str:
    existing = find_runtime(control)
    request = _runtime_request(image_uri, role_arn)
    if existing is None:
        resp = control.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, **request)
        runtime_id, arn = resp["agentRuntimeId"], resp["agentRuntimeArn"]
        print(f"runtime created: {arn}")
    else:
        runtime_id, arn = existing["agentRuntimeId"], existing["agentRuntimeArn"]
        control.update_agent_runtime(agentRuntimeId=runtime_id, **request)
        print(f"runtime updated: {arn}")
    wait_ready(control, runtime_id)
    return arn


def smoke(runtime_arn: str) -> None:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier="DEFAULT",
        runtimeSessionId=f"skillopt-smoke-{uuid.uuid4().hex}",
        payload=json.dumps({"action": "ping", "versions": True}).encode("utf-8"),
    )
    body = resp["response"].read().decode("utf-8")
    print("smoke response:", body)
    parsed = json.loads(body.split("data:")[-1].strip() if body.startswith("data:") else body)
    if not parsed.get("ok"):
        raise SystemExit("smoke ping failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-build", action="store_true", help="skip image build/push")
    ap.add_argument("--smoke", action="store_true", help="only ping the deployed runtime")
    ap.add_argument("--teardown", action="store_true", help="delete the runtime")
    args = ap.parse_args()

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)

    if args.smoke or args.teardown:
        existing = find_runtime(control)
        if existing is None:
            raise SystemExit(f"runtime {RUNTIME_NAME} not found")
        if args.teardown:
            control.delete_agent_runtime(agentRuntimeId=existing["agentRuntimeId"])
            print(f"runtime deleted: {existing['agentRuntimeArn']}")
            return
        smoke(existing["agentRuntimeArn"])
        return

    acct = account_id()
    bucket = ensure_bucket(boto3.client("s3", region_name=REGION), acct)
    repo_uri = ensure_ecr(boto3.client("ecr", region_name=REGION))
    role_arn = ensure_role(boto3.client("iam", region_name=REGION), acct, bucket)
    image_uri = f"{repo_uri}:latest" if args.no_build else build_image(repo_uri)
    runtime_arn = ensure_runtime(control, image_uri, role_arn)

    info = {
        "runtime_arn": runtime_arn,
        "bucket": bucket,
        "region": REGION,
        "image_uri": image_uri,
        "role_arn": role_arn,
    }
    os.makedirs(os.path.dirname(INFO_PATH), exist_ok=True)
    with open(INFO_PATH, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)

    print("\nAdd to .env / shell to enable the remote exec runner:")
    print("export SKILLOPT_EXEC_RUNNER=agentcore")
    print(f"export SKILLOPT_AGENTCORE_RUNTIME_ARN={runtime_arn}")
    print(f"export SKILLOPT_AGENTCORE_S3_BUCKET={bucket}")


if __name__ == "__main__":
    sys.exit(main())
