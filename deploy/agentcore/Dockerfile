# SkillOpt AgentCore exec worker (linux/arm64).
# Build via scripts/agentcore/build_and_push.sh, which stages this context
# (skillopt/ package, claude/codex standalone binaries, codex config).
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git ripgrep procps unzip jq \
    && rm -rf /var/lib/apt/lists/*

# openai/pyyaml: imported by skillopt.model / skillopt.config at import time
RUN pip install --no-cache-dir boto3 bedrock-agentcore openai pyyaml

COPY bin/claude /usr/local/bin/claude
COPY bin/codex /usr/local/bin/codex

RUN useradd -m -u 1000 agent
COPY --chown=agent:agent codex-home/ /home/agent/.codex/
COPY skillopt/ /opt/skillopt/skillopt/

ENV PYTHONPATH=/opt/skillopt \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    AWS_REGION=us-west-2 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_EXEC_USE_SDK=cli \
    CODEX_EXEC_USE_SDK=cli \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER agent
WORKDIR /home/agent
EXPOSE 8080
CMD ["python", "-m", "skillopt.model.agentcore_worker"]
