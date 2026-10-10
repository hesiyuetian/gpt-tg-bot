# GitHub Trending → Telegram 日报

每天北京时间 **20:00**，读取 [GitHub Trending daily 全语言榜](https://github.com/trending?since=daily)，按页面 `stars today` 从高到低取最多 20 个项目，推送到自己的 Telegram 私聊。**简介保留项目原文**；已有邮件日报不受影响。

**初始状态：定时发送关闭。先预览、核对收件人并完成一次手动测试，再开启定时。** 不需要服务器、付费模型 API 或额外 API key。

## 只需设置这几步

1. 在 Telegram 中打开自己的 bot，核对准确用户名并发送 `/start`。安全取得这条私人会话的 `chat.id`，确认是自己的会话，不能使用群组/频道 ID。已有 bot 正在运行时，从原程序的受信任处理器或管理界面取得，不启动第二个更新消费者，也不删除 webhook。不能确认 ID 时先停在预览。
2. 打开本仓库 **Settings → Secrets and variables → Actions → Secrets**，由你亲自添加：
   - `TELEGRAM_BOT_TOKEN`：现有 bot token
   - `TELEGRAM_CHAT_ID`：已经核对的私人聊天正整数 ID
3. 打开 **Actions → GitHub Trending Telegram digest → Run workflow**，默认两个选项不勾选，只查看 Summary 中的完整预览。
4. 预览正确后，再 Run workflow，勾选 `send` 和 `recipient_verified`。这会向所配置的私人聊天发送一次当天完整日报。核对 Telegram 实际收到的消息；失败时不要直接 Re-run，先看下方说明。
5. 一次测试收件正确后，在 **Settings → Secrets and variables → Actions → Variables** 新建 `DIGEST_ENABLED`，值为 `true`。此后每天定时发送。暂停时改为 `false` 或删除这个变量。

手动测试不需要提前启用定时。手动已发送当天日报后，当天的后续定时/手动发送会被保守去重；下一天正常继续。

**不要把 token 放进 Git、聊天、截图、命令行参数、浏览器 URL 或第三方“获取 ID”网站。** Chat ID 也按隐私信息存放。本仓库公开；不要将任何私人内容写进源文件。只有两个 Telegram secrets 需要手动添加；`GITHUB_TOKEN` 由 Actions 自动提供且只读。

[GitHub secrets 官方说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) · [Telegram 更新方式](https://core.telegram.org/bots/faq#how-do-i-get-updates)

## 内容与时间说明

- 只在 Trending 返回的候选项目内排序，不是 GitHub 全站新增 stars 排名，也不是累计 stars 排名。少于 20 个就如实发送，不造数据补齐。
- `today` 采用 GitHub 页面口径，抓取时间和日报日期用北京时间标注。
- 简介保留原文；较长简介带省略号，仓库链接始终保留。正文按保守 UTF-16 长度分段，每条不超过 Telegram 的 4096 字符限制，关闭链接预览。
- `0 12 * * *` 为 UTC 12:00 / 北京时间 20:00。GitHub 定时任务可能延迟，繁忙时可能丢弃，不能保证整点。workflow 必须在默认分支；公开仓库连续 60 天无活动可能被停用定时任务。[官方调度限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- 邮件与 Telegram 独立抓取/生成，因此不保证项目和文字完全一致；本项目没有复制或改动邮件自动化。
- 不使用付费翻译或 Telegram 付费广播。Actions 额度和潜在费用取决于账户/仓库设置，上线前查看自己的 Billing/Actions 限额。

## 安全与失败处理

- Python 3.11+ 标准库，无 pip/npm 依赖。Ubuntu 24.04 runner 自带 Python。
- 只在最终发送步骤注入 Telegram secrets；测试和抓取不接触这些凭据。workflow 仅有 `contents: read` 和 `actions: read` 权限。
- 仅调用 Telegram `sendMessage`，完全不调用 `getUpdates`、`setWebhook` 或 `deleteWebhook`。现有 bot 可继续使用原有服务；只是需要安全核对私聊 ID。
- Telegram POST 每段仅尝试一次。超时、429、连接中断或异常响应立即停止，不自动重发；前面的分段或出错这一段可能已经到达，**先检查 Telegram**。不同分段间隔 1.2 秒。[Telegram API](https://core.telegram.org/bots/api#sendmessage)
- 同一 workflow 串行运行，以只读 job 历史保守防重复。当天 `Delivery attempt (do not rename)` 步骤一旦启动，即使失败/取消，也视作“可能已发送”。已有 run 的 Re-run 不能发送。凭据错误也可能占用当天；修好后通常等下一天，确需补发时先核对实际收件再做有范围的恢复。
- 这是尽力防重复，不是严格 exactly-once。删除运行历史、重建/复制 workflow、更名发送步骤或跨仓库运行会破坏判断，不要用这些操作绕过提示。历史不完整/尚未同步/超过检查上限时停止。
- GitHub **只读 GET** 临时失败可最多请求三次。空榜、HTML 结构变化、缺少 `stars today`、过期预览或 fixture 都停止；不拿总 stars 替代。
- 失败会显示在 Actions；当前没有额外故障通知服务。不要开启包含原始 HTTP 请求或环境变量的调试日志。
- 源码和 workflow 的修改者可能接触运行时 secrets，只给可信的人仓库写权限。唯一外部 Action 为官方 checkout，固定完整 SHA，禁用凭据持久化：[已核验提交 / v7.0.1](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)。升级时核对上游发布和 SHA。[GitHub 安全建议](https://docs.github.com/en/actions/reference/security/secure-use)

## 本地开发 / 无凭据验证

```sh
python3 -m unittest discover -s tests -v
python3 digest.py build --input-html tests/fixtures/trending.html --out out/fixture
python3 digest.py build --out out/live
```

第二项仅合成演示数据，结果禁止发送；第三项只读取 GitHub，打开 `out/live/preview.txt` 看正文。`digest.json` 保留项目、原文、哈希、排序和时间。

主要文件：`digest.py`、`.github/workflows/digest.yml`、`tests/`。`chinese_overrides.json` 默认为空，原文模式不需要修改；可选的人工译文只在原文哈希一致时应用，不调用自动翻译。

本地测试和真实 GitHub 抓取已验证；真实 Actions、Telegram 配对和收件需要按上方步骤完成，不能仅凭本地测试认为已上线成功。
