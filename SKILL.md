---
name: github-trend-radar
description: "GitHub 开源趋势雷达：抓取并分析 GitHub 热门项目，输出总 Star 榜、24 小时/7 天增长榜、分类趋势、可复用项目评分与观察清单维护状态，并生成 Markdown/HTML 日报。当用户要求查看 GitHub 趋势、开源技术趋势、热门仓库排行、项目复用评估、历史退榜原因、替代项目判断，或查看观察清单的版本升级与维护状态时使用。Use when users want GitHub trending repos, star rankings, daily/weekly growth, or open-source trend analysis."
---

# GitHub 开源趋势雷达

## 启动

从项目根目录执行。先运行 `radar.py --help`，再按帮助选择命令；`--workspace` 是全局参数，必须放在子命令前。

**Python 环境**：需要 Python 3.10+。先安装依赖：`pip install -r requirements.txt`（含 openpyxl、PyYAML）。之后直接用 `python3 radar.py ...` 即可；若当前 `python3` 未安装依赖请先安装，或把你的解释器路径通过 `run_daily_report.sh` 的 `PYTHON` 环境变量传入——命令本身不写死解释器。

```bash
PY=python3   # 如本机解释器路径特殊，改成绝对路径，或用 run_daily_report.sh 的 PYTHON 环境变量覆盖
$PY .workbuddy/skills/github-trend-radar/scripts/radar.py --workspace /path/to/workspace report
$PY .workbuddy/skills/github-trend-radar/scripts/radar.py repo openai/codex
$PY .workbuddy/skills/github-trend-radar/scripts/radar.py compare old-owner/old-repo new-owner/new-repo
$PY .workbuddy/skills/github-trend-radar/scripts/radar.py watchlist
```

网络请求建议设置 CA 证书（与 `scripts/run_daily_report.sh` 一致）：`export SSL_CERT_FILE=/path/to/certifi/cacert.pem`（替换为你的 certifi 路径，或依赖系统证书；脚本通过 `CERTIFI_PEM` 环境变量传入）。

## 选择模式

- 运行无 `--category` 的 `report` 生成完整报告并默认保存；日期统一使用 `Asia/Shanghai`。本项目的默认根目录为 `GitHub开源趋势雷达/`；指定 `--workspace` 时写入该目录之下。
- 若需要把日报资产放在项目外的专用目录，使用 `--output-root /path/to/GitHub开源趋势雷达`；该目录直接包含 `配置/`、`最新报告/`、`历史归档/` 与 `运行状态/`。日常运行脚本 `scripts/run_daily_report.sh` 通过环境变量 `OUTPUT_ROOT` / `PYTHON` / `CERTIFI_PEM` 配置路径，默认在技能目录所在项目的 `GitHub开源趋势雷达/` 下生成资产，可覆盖这些变量指向任意目录。
- 运行 `report --category AI` 默认仅预览；增加 `--save` 后写入独立的分类 Markdown/JSON，不得覆盖完整日报同日期文件或归档，也不得写全局 history 或更新 Excel。
- 运行 `repo` 或 `compare` 仅输出 JSON，不持久化。
- 首次运行任一 `report` 会初始化分类规则；首次运行完整 `report` 或 `watchlist` 会初始化固定 16 列 Excel 模板。已有 Excel 严格只更新 M–P 自动列，不得覆盖、移动或重排 A–L 人工列。

## 执行约束

- 生成完整报告前读取 [指标口径](references/指标口径.md) 和 [报告结构](references/报告结构.md)；运行 `repo` 或 `compare` 前至少读取指标口径。
- 严格区分总 Star、`created_at`、24h 新增和 7d 新增；禁止用创建时间排序近似增长。
- 报告中的指标必须带中文名称和单位：总榜是当前累计 Star，24h/7d 榜是统计窗口内新增 Star，加速度是 24h 增速相对 7 日日均的倍数；不要输出无法解释的“数值”列。
- 每个核心榜单项目都要显示一级分类、仓库类型、用途与定位、主要解决问题和数据口径；用途优先来自 GitHub 官方简介，信息不足时明确标记，不得猜测。
- 把 GitHub Trending 视为 selected set。按来源和周期隔离状态，明确标注 `external` 与 `local`，不得跨周期补值。
- 先冻结客观榜、分类趋势和复用榜，再读取观察清单；watchlist-only 项目只进入观察、历史与后置更新。
- 把退榜事实、原因推断、证据、可信度、替代关系和动作分开。退榜不等于过时；没有 canonical 直接证据不得声称“直接替代”。普通仓库说明页不构成直接替代证据。
- 仅对可预期的来源失败降级。意外异常必须失败且不得覆盖已成功生成的同日期报告；报告成功后 history 或 Excel 失败时，明确报告“后置更新部分失败”。
- 业务锁统一位于 `运行状态/锁/`，不要在报告、归档或配置文件旁创建 `.lock`；锁文件不能在解锁后直接删除，以免并发任务出现锁 inode 分裂。

## 优化记录（2026-08-17）

### 重点项目解读
- **改动前**：按总 Star 排序取前 5，多为 Awesome 列表和教育资源
- **改动后**：优先选择 AI/开发工具分类中周增长≥500 或日增长≥2 的项目，更聚焦技术趋势
- **代码位置**：`radar.py` `_model()` 方法中的 `_featured_candidates` 逻辑

### 市场走向
- **改动前**：固定为空列表 `[]`
- **改动后**：新增 `_generate_market_trends()` 方法，基于分类数据和增长模式生成趋势判断
- **输出示例**：「AI 类项目本周增长活跃，前 3 名周增合计贡献约 40% 的总增量」
- **代码位置**：`radar.py` `_generate_market_trends()` 方法

### 今日速览 - 推断
- **改动前**：固定为「榜单热度不等同于项目适配度」
- **改动后**：新增 `_generate_overview_inferences()` 方法，补充 AI 增长集中度和整体活跃度判断
- **代码位置**：`radar.py` `_generate_overview_inferences()` 方法

### 可复用项目评分
- **改动前**：仅计算 license、maintenance、releases、community 四个信号，满分实际只有 65 分
- **改动后**：补充 docs（15 分）、integration（15 分）、ci（5 分）三个信号，满分恢复到 100 分
- **docs 信号**：基于描述长度和 topics 数量启发式计算
- **integration 信号**：基于仓库类型（SDK/Library/Framework）和主语言启发式计算
- **ci 信号**：基于 topics 中的 CI/CD 标识启发式计算
- **代码位置**：`radar.py` `_reusability()` 方法

### 可复用项目榜排序
- **改动前**：按评分降序，同分按仓库名排序（未显式实现）
- **改动后**：在 `_model()` 中显式调用 `reusable.sort(key=lambda x: (-x.get("score", 0), x.get("repo", "")))`
- **代码位置**：`radar.py` `_model()` 方法，`reusable` 赋值后

