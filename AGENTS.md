# video-curator 开发约定

## 项目与边界

- Python 包为 `video_curator`，安装包和 CLI 为 `video-curator`。Labubu 是测试主题，不是框架唯一对象；关键词、提示词与任务语义放在主题 YAML。
- 主线为 `search → metadata → filter → screen → download`。阶段可独立运行和恢复；下载不补详情、不调用模型。
- 公共阶段在 `src/video_curator/`，平台实现放 `platforms/`，外部服务适配放 `providers/`。保持职责直接，不恢复顶层大 pipeline 或多层 service/base 包装。
- Cosmos 后处理、`clips.jsonl`、主体跟踪和动作恢复尚未接入生产流程；内部实验不代表已实现。
- YouTube、B站曾通过匿名搜索及下载验证；抖音搜索、小红书仍有验证码或出口风控限制，不宣称四站全流程已验证。

## 高频命令

需要 Python 3.12+、uv、FFmpeg/ffprobe。在仓库根目录执行：

```bash
uv sync                        # 浏览器后端另用 uv sync --extra browser
uv run video-curator search --topic configs/topics/labubu.yaml --platform bilibili --mode native --all-pages --page-size 50 --output datasets/labubu-bilibili
uv run video-curator metadata --input datasets/labubu-bilibili
uv run video-curator filter --input datasets/labubu-bilibili --topic configs/topics/labubu.yaml
uv run video-curator screen --input datasets/labubu-bilibili --topic configs/topics/labubu.yaml
uv run video-curator download --input datasets/labubu-bilibili --limit 40 --resolution 720
uv run video-curator status --input datasets/labubu-bilibili
uv run video-curator export --input datasets/labubu-bilibili
uv run python scripts/dataset_viewer.py --input datasets/labubu-bilibili
```

- 外部 ROS 等 `PYTHONPATH` 干扰时，用 `env -u PYTHONPATH .venv/bin/python -m video_curator.cli ...`，测试与脚本同样清理 PYTHONPATH。
- 搜索默认读取主题的全部关键词；分页用 `--page-size`。阶段 `--limit` 必须为正数，0 不表示全量。下载上限是本轮处理数，720 是格式选择偏好，不是强制转码。
- `filter`、`screen` 使用 `ARK_API_KEY`；模型和提示词跟随主题配置。凭据只来自环境或明确指定的会话文件，不自动读取个人浏览器 Cookie。
- 查看器默认只读 SQLite、每 10 秒刷新，不调用模型；`--output tmp/viewer.html` 导出静态快照。

## 数据与恢复

- 同一来源视频只有一条 `video_curator.video.v3` 记录，包含 search/metadata/filter/screening/download；每阶段统一 `{status,error,data}`。从未执行为 not_run/null/null，失败和过期结果不能当作有效判断。
- 文本分类 keep/reject/uncertain；执行失败为 failed/unclassified。视觉评级 priority/partial/difficult/low，分页取最高档；未完成所有必要页面不能当成视觉成功。
- SQLite `.state/state.sqlite` 是最新执行状态；`videos.jsonl` 与 `task.json` 是成对快照，靠 videos_sha256 校验。任务 schema 为 `video_curator.task.v2`，不同对象的版本号不要求相同。
- 连接记录使用 record_id；同站身份按 platform/content_id/part_id 去重，B站每个分P单独记录。不按标题、行号连接，不冒充跨站语义去重。
- 写任务必须持有 jobs 的任务锁。移动任务优先保留 SQLite 最新结果并转换路径，不用旧快照覆盖数据库；数据库缺失才从快照重建。保留原库时可用 export 修复中断的快照导出。
- 重试次数按 metadata/download 分开。迁移前备份 SQLite 与两个快照，使用 SQLite backup API 包含 WAL；代码、导出、恢复和查看器必须同步更新。复用媒体还需携带 raw/evidence/references。
- 已有筛选结果、调用数和用量必须保留。不要修改 JSONL 绕过数据库；不要把 legacy_whole_video 或历史调用协议伪装成新结果。
- `--retry-failed`、`--new-version`、`--resample-legacy` 可能再次计费；仅在任务要求时使用。请求结果未知应保留 outcome_unknown，不自动重复付费。429 也可能是额度耗尽，不能一概归因于并发。
- 升级入口为 `scripts/upgrade_dataset.py --input <任务目录> --backup <新备份目录>`；旧 v2 单页数据先用 `scripts/migrate_screen_pages.py`。运行时不保留旧包名别名。

## 修改与验证

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q
env -u PYTHONPATH .venv/bin/ruff check src tests scripts/*.py
uv lock --check --offline
git diff --check
```

- 修改状态、缓存、迁移、身份或恢复逻辑须补对应回归测试；不为减少测试数量删除不同失败路径的覆盖。测试应使用 mock/本地媒体，不发真实模型请求。
- 优先复用现成依赖。复制上游源码时保留许可证和 UPSTREAM 说明，并维护 `vendor/manifest.json` 的来源、固定 commit、路径与改动记录；该清单不参与运行时加载。
- 数据集、模型结果、凭据、`tmp/` 和 `docs/internal/` 不提交；只跟踪 `datasets/README.md`。临时实验与备份放本仓库 tmp，不修改相邻项目。
- 内部详细交接在本机 `docs/internal/HANDOFF.md`（被 Git 忽略，远端克隆可能没有），本文是可提交的操作入口。历史统计以实时查询为准；旧交接中的命令以当前 CLI 为准。
- 本地修改、提交与推送遵循本轮用户授权；不要强制覆盖远端历史。
