# video-curator

按主题发现、筛选和整理视频素材，为动作恢复、角色动画及其他视频数据集任务准备数据。Labubu 是当前测试主题；Python 包名为 `video_curator`，命令入口为 `video-curator`。

## 当前流程

`search → metadata → filter → screen → download`

- 搜索与详情：统一平台字段、同站资源去重、分页恢复。
- 文本筛选：按主题提示词批量判断相关性。
- 视觉筛选：默认每 5 分钟抽 8 帧，加封面组成拼图，逐页评级。
- 下载：默认只下载视觉高优先级原片，核验后归档，已完成记录自动跳过。

YouTube、B站曾通过匿名搜索和下载验证；抖音搜索、小红书采集仍有验证码或网络风控限制。镜头切分、主体跟踪、分割等后处理尚未接入正式流程。

## 使用

需要 Python 3.12+、FFmpeg/ffprobe。使用 uv 安装依赖：

```bash
uv sync
```

在仓库根目录执行，各阶段可独立运行：

```bash
# 搜索：读取主题中全部关键词
uv run python -m video_curator.cli search \
  --topic configs/topics/labubu.yaml --platform bilibili --mode native \
  --all-pages --page-size 50 --output datasets/labubu-bilibili

uv run python -m video_curator.cli metadata --input datasets/labubu-bilibili

uv run python -m video_curator.cli filter \
  --input datasets/labubu-bilibili --topic configs/topics/labubu.yaml

uv run python -m video_curator.cli screen \
  --input datasets/labubu-bilibili --topic configs/topics/labubu.yaml --workers 2

uv run python -m video_curator.cli download \
  --input datasets/labubu-bilibili --limit 40 --resolution 720
```

`filter` 与 `screen` 需要 `ARK_API_KEY`，模型和提示词在主题 YAML 中配置。失败项通过 `--retry-failed` 显式重试，可能再次计费。下载 `--limit 40` 表示本轮最多处理 40 条；720 是格式选择偏好，不保证每个文件尺寸相同。

本机若有外部 `PYTHONPATH` 干扰，可将 `uv run python` 替换为 `env -u PYTHONPATH .venv/bin/python`。

## 查看数据

```bash
uv run python scripts/dataset_viewer.py --input datasets/labubu-bilibili
```

加 `--output tmp/dataset-viewer.html` 可导出静态快照。默认打开 `http://127.0.0.1:8765/`（端口占用时以终端地址为准）。网页只读 SQLite，每 10 秒刷新，不调用模型；按 `Ctrl+C` 关闭。

每个任务主要包含 `videos.jsonl`、`task.json`、`.state/`、`evidence/` 和 `raw/`。当前视频格式为 v3，阶段结果持续补充；不要手工修改 JSONL 绕过数据库。后处理拟使用独立的 `clips.jsonl`。

## 开发与交接

- 开发入口与注意事项见 [AGENTS.md](AGENTS.md)。
- 公共阶段：`src/video_curator/`；平台实现：`platforms/`；外部后端：`providers/`。
- 现有交接、研究和历史说明存放在本地 `docs/internal/`，不纳入 Git；目前暂无独立的对外文档。

```bash
uv run pytest -q
uv run ruff check src tests scripts/*.py
```
