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
./start.sh                                 # dev:http://127.0.0.1:8321,无鉴权(STUDIO_PORT/STUDIO_HOST 可覆盖)
./start.sh --prod                          # prod:0.0.0.0:8321,登录验证开启(见下)
./stop.sh                                  # 停服;运行中的 job 不会被杀(先在 UI 取消)

# 手动单命令(需先构建一次前端)
cd skillopt_studio/frontend && npm install && npm run build && cd ../..
python3 -m skillopt_studio                 # http://127.0.0.1:8321

# 开发模式(双进程,前端热更新,/api 代理到 8321)
python3 -m skillopt_studio --reload        # 终端 1:后端
cd skillopt_studio/frontend && npm run dev # 终端 2:Vite dev server(:5173)
```

参数:`--host`(默认 127.0.0.1)、`--port`(默认 8321)、`--reload`。

### prod 模式与登录验证

`./start.sh --prod` 面向公网暴露场景(ALB / CloudFront 后面):绑定 `0.0.0.0`、检测到前端源码比 dist 新会自动重建,并强制**用户名/密码登录**:

- 用户名:`STUDIO_AUTH_USERNAME`(默认 `admin`);密码:`STUDIO_AUTH_PASSWORD`,未设置时首次启动自动生成并持久化到 `outputs/studio/auth_password`(chmod 600)。两者都可写进 `.env`。
- 鉴权是无状态签名会话 cookie(HttpOnly,12 小时有效):`POST /api/auth/login` 颁发,所有 `/api/*` 与 `/docs` 未登录一律 401;`/api/health`(负载均衡健康检查)与 SPA 静态壳保持开放。轮换密码即吊销全部会话。
- 前端未登录时整站切换为登录页;会话过期任意请求 401 会自动弹回登录页;侧栏底部有退出登录。
- 不设置 `STUDIO_AUTH_PASSWORD` 时(默认 dev 启动)行为与从前完全一致,零鉴权。

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
| 任务集 | `/tasksets` | single(单 tasks.json)/ split(train/val/test)两种模式;新建支持文件上传 / 手动逐条输入 / AI 自动生成三种方式(内嵌 JSON 格式说明);已有任务集可编辑;保存前 fail-fast 校验(缺 rubric 等直接 400 并指出条目) |
| 发起评估 | `/evaluate` | 选技能 × 任务集 × 参数(执行后端 claude_code_exec / codex_exec、目标模型/判分模型/workers/timeout)→ 真实 eval job |
| 发起训练 | `/train` | 额外支持 trainable_files 多选(与 SKILL.md 打包成 bundle 训练)、split 或 single+ratio、num_epochs/gate_metric/learning_rate/eval_test,同样可选执行后端 |
| 任务管理 | `/jobs` | 全部任务 + 状态/类型筛选 + 取消;详情页四 tab:概览 / 日志(增量轮询)/ 结果(eval 表格或训练时间线 + val 曲线 + skill diff)/ 产物浏览(md 渲染、代码高亮、逐文件下载,与技能库文件预览一致) |

执行后端按技能来源自动推荐(codex 源技能默认 Codex 执行);`GET /api/environment` 检测 `claude` / `codex` CLI 是否安装,向导里未检测到会红字提醒,提交时后端同样 fail-fast 拒绝。目标模型留空 = 用所选后端的默认模型。

## 任务集:编辑 / 手动录入 / AI 自动生成

**编辑修改。** 详情页“编辑任务集”进入编辑模式:每个分组一个逐条编辑器(split 集 train/val/test 各自独立,test 分组清空保存即删除),支持重命名(显示名变、id 与 URL 不变)。编辑器打开时按 `?full=1` 拉取全量任务(只读视图仅预览 20 条),行对象只覆写 id/question/rubric/task_type 四个字段——`files` 与任何未识别字段原样透传,不会被编辑丢掉。保存是**全量替换**:先整体校验、原子落盘,失败时原文件保持不变。注意:编辑影响后续使用该任务集的评估/训练运行(已排队/运行中的训练在提交时已拷贝文件,不受影响;排队中的评估会读到新内容)。

**手动逐条输入。** 新建表单的第二个 tab:行式编辑器(id 自动建议 task_001 风格、行级中文校验:必填缺失/重复 id/非法字符),产出 single 模式任务集;需要预分割 train/val/test 请用文件上传。两个 tab 均内嵌可折叠的 **JSON 格式说明**(字段表 + 完整示例 + 复制按钮),对应 `POST /api/tasksets/items`(JSON body)与 `PUT /api/tasksets/{id}`(全量更新)。

**AI 自动生成。** 第三个 tab:选一个待评估技能 + 执行后端(claude_code_exec / codex_exec,推荐规则与评估向导一致)+ 数量(1-30)+ 可选生成指引,提交为 `taskgen` 作业(底层是 `python3 scripts/generate_tasks.py`:agent 阅读技能后把任务写入 `generated_tasks.json`,经 `load_tasks` 严格校验,失败自动带错误反馈重试一次)。生成结果**不直接落库**——作业详情页审阅任务表后点“导入为新任务集”,条目预填进手动编辑器,确认/修改后再保存。

## 内置样例(Samples)

启动时 Studio 会把仓库里的预置素材物化成可直接使用的样例(`skillopt_studio/samples.py`):样例 skill 复制到 `<studio_root>/samples/skills/<slug>/`,以「内置样例」分组显示在技能库最前;样例任务集写入任务集目录,meta 带 `sample: true`。默认开启,`SKILLOPT_STUDIO_SAMPLES=0` 关闭(关闭后磁盘上残留的样例也不会出现在列表里)。物化每次启动重建,与仓库保持同步。

**样例 skill(9 个)。** SKILL.md 与仓库源文件**字节一致**(评测保真);中文名/描述来自隐藏边车文件 `.studio_sample.json`,它不进文件树、不可通过文件接口访问、也不会被复制进评测工作区。

| id | 来源 | 说明 |
|---|---|---|
| `sample--searchqa-gpt5.5` | `ckpt/searchqa/gpt5.5_skill.md` | 论文 checkpoint;可配 `sample-searchqa` 任务集 |
| `sample--alfworld-gpt5.5` | `ckpt/alfworld/gpt5.5_skill.md` | 论文 checkpoint;无内置任务集 |
| `sample--docvqa-gpt5.5` | `ckpt/docvqa/gpt5.5_skill.md` | 论文 checkpoint;无内置任务集 |
| `sample--livemath-gpt5.5` | `ckpt/livemath/gpt5.5_skill.md` | 论文 checkpoint;无内置任务集 |
| `sample--officeqa-gpt5.5` | `ckpt/officeqa/gpt5.5_skill.md` | 论文 checkpoint;无内置任务集 |
| `sample--spreadsheetbench-gpt5.5` | `ckpt/spreadsheetbench/gpt5.5_skill.md` | 论文 checkpoint;无内置任务集 |
| `sample--logtriage` | `data/skilleval_demo/logtriage_skill/` | 多文件弱基线,配 `sample-logtriage`,适合演示训练提升 |
| `sample--logtriage-v2` | `data/skilleval_demo/logtriage_skill_v2/` | 多文档训练演示(含故意错误的 report-template) |
| `sample--report` | `data/skilleval_demo/report_skill/initial.md` | 单文件弱基线,配 `sample-report` / `sample-xlsx` |

「无内置任务集」的 5 个 benchmark checkpoint 仍可评测:用第三个 tab 的 **AI 自动生成**为它生成任务集,或上传自带任务集。

**样例任务集(6 套,只读)。**

| id | 模式 | 规模 | 建议搭配 |
|---|---|---|---|
| `sample-logtriage` | split | 4/3/3 | `sample--logtriage` 或 `sample--logtriage-v2` |
| `sample-report` | split | 4/3/3 | `sample--report` |
| `sample-xlsx` | single | 3 | `sample--report` |
| `sample-searchqa` | split | 12/6/12 | `sample--searchqa-gpt5.5` |
| `sample-livemath` | split | 12/6/12 | `sample--livemath-gpt5.5` |
| `sample-officeqa` | split | 12/6/12 | `sample--officeqa-gpt5.5` |

后三套由本机 benchmark 数据转换而来(各 split 取前 12/6/12 条,rubric 由标准答案生成,宽松匹配 1.0/0.0):`sample-searchqa` 需 `data/searchqa_split`(`python3 scripts/materialize_searchqa.py`),question 内嵌检索上下文;`sample-livemath` 需 `data/livemathematicianbench_split`,单选题、答案为选项字母;`sample-officeqa` 需 `data/officeqa_split` + `data/officeqa_docs_official`(HF gated,需申请访问),question 内嵌 oracle 文档(超 16k 字符截断)。任一源数据不存在时自动跳过该套,其余样例不受影响。

样例任务集**只读**:编辑/删除会被 API 以 400 拒绝;详情页用「另存为我的任务集」创建可编辑副本。快速上手:发起评估 → 技能选 `sample--logtriage` → 任务集选 `sample-logtriage` → 提交,即可看到完整的评测链路与产物。

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
