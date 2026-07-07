# SkillOpt Studio

SkillOpt Studio 是一个 **localhost 可视化操作台**(FastAPI 后端 + React 前端),把技能评估(skilleval)与技能训练(train.py)的完整闭环搬进浏览器:扫描/上传技能 → 组织任务集 → 发起真实评估或训练 → 实时看日志、结果表、训练时间线与 skill diff。

它面向单用户本机使用:默认只绑定 `127.0.0.1`,无鉴权、无多用户、无 HTTPS(见文末"边界")。

## 架构

```text
skillopt_studio/
├── app.py            # FastAPI 工厂:/api 路由 + 托管 frontend/dist(SPA 回退)
├── config.py         # StudioConfig:studio_root(默认 outputs/studio/)、四技能源
├── skill_sources.py  # 四源扫描(claude/codex/kiro/agents)+ zip 上传(zip-slip 守卫)
├── tasksets.py       # 任务集存储,校验复用 skillopt.envs.skilleval.dataloader
├── jobs.py           # JobManager:FIFO 队列 + subprocess(start_new_session/killpg)
├── runners.py        # eval argv / train config.yaml 生成(load_config 解析后覆写)
├── artifacts.py      # 只读产物解析:results/timeline/diff/文件浏览(路径守卫)
├── api/              # skills / tasksets / jobs / dashboard 路由
└── frontend/         # Vite + React + TS + Tailwind(深色主题)
```

运行数据全部落在 `outputs/studio/`(已 gitignore):`skills/`(上传的技能)、`tasksets/<slug>/`、`jobs/<id>/`(`job.json` + `log.txt` + `config.yaml` + `out/` 产物)。

后端不直接 import 训练循环:eval/train 都以子进程运行 `scripts/evaluate_skill.py` / `scripts/train.py`,与命令行跑法产物完全一致。

## 启动

```bash
# 最简:仓库根目录的启停脚本(自动补前端构建、健康检查、pidfile)
./start.sh                                 # http://127.0.0.1:8321(STUDIO_PORT/STUDIO_HOST 可覆盖)
./stop.sh                                  # 停服;运行中的 job 不会被杀(先在 UI 取消)

# 生产模式(手动单命令;需先构建一次前端)
cd skillopt_studio/frontend && npm install && npm run build && cd ../..
python3 -m skillopt_studio                 # http://127.0.0.1:8321

# 开发模式(双进程,前端热更新,/api 代理到 8321)
python3 -m skillopt_studio --reload        # 终端 1:后端
cd skillopt_studio/frontend && npm run dev # 终端 2:Vite dev server(:5173)
```

参数:`--host`(默认 127.0.0.1)、`--port`(默认 8321)、`--reload`。

模型网关环境变量要在**启动 shell**里就绪(与 train.py/eval_only.py 同一套约定),例如 mantle 网关:

```bash
export OPENAI_ENDPOINT=https://<gateway>/openai/v1
export OPENAI_API_KEY=<gateway key>
export OPENAI_AUTH_MODE=openai_compatible
# Claude 目标模型走 claude CLI(claude_code_exec),CLI 已连好即可
```

## 六个页面

| 页面 | 路径 | 作用 |
|---|---|---|
| 总览 | `/` | 状态统计、运行中任务卡(进度短语)、近 10 任务表、快捷发起按钮 |
| 技能库 | `/skills` | 四源扫描分组(claude/codex/kiro/agents 色标)+ 上传 zip(uploaded 组);详情页渲染 SKILL.md 与文件树 |
| 任务集 | `/tasksets` | single(单 tasks.json)/ split(train/val/test)两种模式;保存前 fail-fast 校验(缺 rubric 等直接 400 并指出条目) |
| 发起评估 | `/evaluate` | 选技能 × 任务集 × 参数(执行后端 claude_code_exec / codex_exec、目标模型/判分模型/workers/timeout)→ 真实 eval job |
| 发起训练 | `/train` | 额外支持 trainable_files 多选(与 SKILL.md 打包成 bundle 训练)、split 或 single+ratio、num_epochs/gate_metric/learning_rate/eval_test,同样可选执行后端 |
| 任务管理 | `/jobs` | 全部任务 + 状态/类型筛选 + 取消;详情页四 tab:概览 / 日志(增量轮询)/ 结果(eval 表格或训练时间线 + val 曲线 + skill diff)/ 产物浏览 |

执行后端按技能来源自动推荐(codex 源技能默认 Codex 执行);`GET /api/environment` 检测 `claude` / `codex` CLI 是否安装,向导里未检测到会红字提醒,提交时后端同样 fail-fast 拒绝。目标模型留空 = 用所选后端的默认模型。

## 任务产物在哪

每个 job 一个目录:`outputs/studio/jobs/<job-id>/`

- `job.json` — 状态机记录(queued → running → succeeded/failed/cancelled,exit_code、error)
- `log.txt` — 子进程 stdout+stderr 合并
- `config.yaml` — train job 的完整生成配置(无 `_base_`,可直接拿去命令行复跑)
- `out/` — 与 CLI 跑法相同的产物:eval 是 `results.json` + `report.md`;train 是 `best_skill.md`、`history.json`、`summary.json`、`skills/`、`steps/`

## 常见问题(FAQ)

**评估/训练一直 failed,日志里是网关/密钥错误?**
环境变量只在启动 Studio 的 shell 里生效。停掉服务,`export` 好 `OPENAI_ENDPOINT` / `OPENAI_API_KEY` / `OPENAI_AUTH_MODE` 后重启(或写进 `.env`,`start.sh` 会自动加载);Claude 侧确认 `claude` CLI 本身能跑。

**端口被占用(Address already in use)?**
`ss -ltn | grep 8321` 找到占用进程,或换端口 `python3 -m skillopt_studio --port 8322`。

**任务看起来卡住了?**
详情页"日志"tab 是增量实时流;训练的 rollout 阶段单步可达几分钟无输出属正常。真的挂了就点"取消"——取消按进程组 SIGTERM→3s→SIGKILL,子进程(claude CLI 等)会一并清理。

**上传 zip 被拒?**
上限 50MB;zip 根级(或唯一顶层目录内)必须有 `SKILL.md`;含 `../` 或绝对路径成员的 zip 会被 400 拒绝(zip-slip 防护)。

**任务集校验失败?**
每个任务必须有非空 `id` / `question` / `rubric`,id 需文件系统安全且不重复。错误信息会指出第几条、缺哪个字段。

## 边界(刻意不做)

- 无鉴权/多用户/HTTPS——仅限本机使用;如需远程访问请自行套反向代理并加访问控制。
- 不做部署脚本;不托管模型密钥(密钥只存在于启动 shell 的环境变量)。
- 并发上限默认 1 个运行中 job(SkillOpt 运行很重),其余排队。
