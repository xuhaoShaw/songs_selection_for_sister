# 每日清唱选题与飞书推送

`daily_topic_analysis.py` 为清唱账号生成 5 首歌曲选题、HTML 报告，并推送飞书。

## 一周内不重复

- 按北京时间计算日期，排除今天及前 6 天已经成功推送的歌名。9 月 30 日选曲时排除 9 月 24 日至 30 日的记录，9 月 23 日的歌曲可以再次推荐。
- 同一天手动重跑也会排除已经推送的歌曲。更换歌手、大小写、空格、书名号，以及括号里的 Live/现场版/清唱版等标记不能绕过去重。不同文字别名仍需维护统一歌名。
- 模型收到禁选歌单；代码再次过滤历史重复、当天重复和格式不合格的歌曲，最多调用 3 次补足 5 首。无法补足时停止并报错，不发送重复歌单。
- 只有飞书确认推送成功后才写入历史。保留 30 天，损坏历史会报错，不会悄悄重置。

GitHub Actions 使用 `codex/song-history` 独立分支保存 `song-history.json`，每次运行先读取、最后保存。首次运行自动初始化，需要 workflow 的 `contents: write` 权限；该分支不得被保护规则禁止机器人写入。工作流串行执行，避免定时与手动运行同时选中歌曲。源码分支不会写入选曲记录。具体接口见 [GitHub Contents API](https://docs.github.com/en/rest/repos/contents)。

首次启用没有过去的推送记录，因此从启用之日起累积去重历史。如果需要立即排除过去 6 天的歌曲，可先在 `codex/song-history` 分支的 JSON 中补录日期、歌名和歌手，例如：

```json
{
  "version": 1,
  "entries": [
    {"date": "2026-09-29", "songs": [{"song": "阿楚姑娘", "artist": "原推荐歌手"}]}
  ]
}
```

## 配置与运行

Actions 配置仓库 Secrets：`DEEPSEEK_API_KEY`、`FEISHU_WEBHOOK`。默认定时为北京时间每天 01:27，也支持手动触发。

本地安装 `requirements.txt` 后运行 `python daily_topic_analysis.py`。通过环境变量配置密钥；`HISTORY_FILE` 默认 `song-history.json`，`REPORT_DIR` 默认 `daily-analysis`。本地/云函数运行时应将历史文件放在持久存储，并保证只有一个实例同时运行。

推送成功但历史写入或远端同步失败时，工作流会报错，并尽可能上传历史文件和错误日志。应先恢复该次历史，再重跑，防止重新推荐已经发送的歌曲。

## 离线验证

`python -m unittest discover -s tests -v` 验证去重、七天边界、补选和推送后的历史更新；不会调用模型或发送飞书消息。
`node --test tests/test-song-history.cjs` 验证 Actions 历史同步。
