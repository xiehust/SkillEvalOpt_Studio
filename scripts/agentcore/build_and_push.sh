#!/usr/bin/env bash
# Build the SkillOpt AgentCore worker image (linux/arm64) and push to ECR.
# Stages a minimal build context so the repo's data/ and outputs/ trees never
# reach the docker daemon. Native arm64 build (this box is aarch64).
#
# Usage: scripts/agentcore/build_and_push.sh <ecr-repo-uri> [tag]
set -euo pipefail

REPO_URI="${1:?usage: build_and_push.sh <ecr-repo-uri> [tag]}"
TAG="${2:-latest}"
REGION="${AWS_REGION:-us-west-2}"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CTX="$ROOT/outputs/agentcore_build_context"

CLAUDE_BIN="$(readlink -f "$(command -v claude)")"
CODEX_BIN="$(readlink -f "$(command -v codex)")"
echo "staging claude=$CLAUDE_BIN codex=$CODEX_BIN"

rm -rf "$CTX"
mkdir -p "$CTX/bin" "$CTX/codex-home/model-catalogs"
cp "$ROOT/deploy/agentcore/Dockerfile" "$CTX/Dockerfile"
cp "$ROOT/deploy/agentcore/codex-config.toml" "$CTX/codex-home/config.toml"
if [ -f "$HOME/.codex/model-catalogs/bedrock-models.json" ]; then
  cp "$HOME/.codex/model-catalogs/bedrock-models.json" "$CTX/codex-home/model-catalogs/"
else
  echo "{}" > "$CTX/codex-home/model-catalogs/bedrock-models.json"
fi
cp "$CLAUDE_BIN" "$CTX/bin/claude"
cp "$CODEX_BIN" "$CTX/bin/codex"
chmod 755 "$CTX/bin/claude" "$CTX/bin/codex"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/skillopt/" "$CTX/skillopt/"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${REPO_URI%%/*}"

docker build --platform linux/arm64 -t "$REPO_URI:$TAG" "$CTX"
docker push "$REPO_URI:$TAG"
echo "pushed $REPO_URI:$TAG"
