# 本地数据集

当前任务为 `labubu-bilibili/`，视频记录使用 `video_collect.video.v3`，任务快照使用 `video_collect.task.v2`；它们是不同数据对象的版本号。旧结构备份保存在仓库 `tmp/dataset-backups/`。

新任务从统一 CLI 创建：

```bash
env -u PYTHONPATH .venv/bin/python -m video_collect.cli search \
  --topic configs/topics/labubu.yaml --platform bilibili \
  --mode native --all-pages --page-size 50 \
  --output datasets/labubu-bilibili
```

该命令只搜索并按同站资源身份去重，不补详情、不下载、不运行 VLM。之后使用 `metadata --input datasets/labubu-bilibili`、`filter --input datasets/labubu-bilibili`、`screen --input datasets/labubu-bilibili`、`download --input datasets/labubu-bilibili` 执行后续阶段。数据内容忽略 Git。

每个任务只固定输出 `videos.jsonl` 和 `task.json`。详情、下载及审核持续更新同一清单；`.state/` 保存内部数据库与暂存。任务停止后可只交接两个文件继续搜索／补详情；复用已下载文件还需带上 `raw/`。

`videos.jsonl` 每行固定包含 `schema / record_id / identity / search / metadata / filter / download / screening`。五个阶段均为 `{status, error, data}`，未执行时 data 为 null。文本分类读取 `filter.data.decision`，取值为 keep、reject、uncertain 或 unclassified。执行错误码读取 filter.error。

`task.json.resume_records[record_id]` 保存任务恢复所需的调度信息、初始候选、合并 ID、筛选指纹和审核历史。两个文件以 videos_sha256 配对；只交接两个文件仍可重建 SQLite，复用已下载媒体则需一同携带 raw/evidence。若导出中断造成配对不一致，保留原 SQLite 并执行 `export --input <目录>` 修复。

## 离线升级

较旧的 v3 任务可用以下命令统一阶段重试计数和等价文本筛选指纹：

```bash
uv run python scripts/upgrade_dataset.py \
  --input datasets/labubu-bilibili \
  --backup tmp/dataset-upgrade-backups/labubu-bilibili-before-upgrade
```

备份目录必须是任务目录以外的新目录。脚本持有任务锁，先备份 SQLite 与两个快照；失败时恢复备份，不调用模型或下载媒体。原片和证据文件保持不动。已完成同版本升级时再次运行直接跳过。

每条恢复记录的 `processing.stage_attempts` 按 metadata/download 记录重试次数；`processing.attempts` 保留历史总计。等价筛选指纹转换后保留 `original_config_hash` / `original_input_hash`，原批次审计不变。

`legacy_whole_video` 表示历史真实采样方式，不是旧文件格式；不能仅修改标签冒充重新抽帧。旧模型调用协议和已确认的配置等价关系同样保留溯源。只有明确需要重新采样时才使用 `screen --resample-legacy`，这可能产生模型费用。
