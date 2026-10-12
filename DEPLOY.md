# 两项更新：由你自行提交

本包包含A股TG早盘量价观察，以及GitHub日报跨午夜去重修复。没有推送到GitHub；本包不含密钥，不需要重填现有secrets。

## 推荐：在现有仓库应用补丁

解压压缩包，将 `changes.patch` 放在仓库目录旁。在已有 `gpt-tg-bot` 仓库运行（以下路径按实际解压位置替换）：

```sh
git switch main
git pull --ff-only
git status --short
```

确认工作区无未保存改动。补丁基于main提交 `d113b54c2e36fb2132360a14ddfbfcca2d745194`；如果仓库已经有新修改，不要强行覆盖，先核对差异。

```sh
git apply --check ../gpt-tg-bot-delivery/changes.patch
git apply ../gpt-tg-bot-delivery/changes.patch
python3 -m unittest discover -s tests -v
python3 -m py_compile digest.py ashare.py
git diff --check
git diff --stat
git add README.md digest.py tests .github/workflows/ashare.yml ashare.py config docs/ASHARE_BRIEF.md docs/UPDATE_VALIDATION.md
git commit -m "Add A-share morning brief and fix scheduled digest cycles"
git push origin main
```

Windows通常可将 `python3` 改为 `python`。无需新增Python依赖。

补丁无法应用时，不要使用强制覆盖。压缩包 `source/` 是完整更新后源码，供逐文件比较；注意 `.github/` 是隐藏目录，必须保留。不要上传压缩包本身来代替源代码。

## 发布效果与核验

推送默认分支后，新增 `A-share morning Telegram brief` 工作流配置北京时间交易日10:00发送，沿用现有Telegram secrets与私人聊天。运行可能延迟；晚于10:20跳过。只列固定20只观察池中最多5只达标股，不足不凑数。日历仅覆盖2026，2027未更新会安全停止。

仓库 Actions 中查看是否出现该工作流。手动 `Run workflow` 只预览、绝不发送；交易时段外预览会显示skip。实际投递仍需观察下次正式定时结果，不要用重新运行按钮盲目重发。若需暂缓A股任务，可在提交前移除新工作流，或发布后暂停该工作流；仓库变量 `ASHARE_DISABLED=true` 也会停止发送。

原20:00 GitHub日报工作流文件未改；新去重按计划周期处理凌晨延迟。邮件任务不变。

## 本包已完成检查

- 78项单测、Python编译、两份工作流YAML解析通过。
- 补丁在干净基线应用检查通过；应用后代码与source逐文件一致。
- 当天09:32只读实时行情3指数+20观察股全部解析通过；这不是10:00行情或TG真实投递测试。
- 未推送、未触发真实发送、未更改secrets或权限。

详情：`source/docs/ASHARE_BRIEF.md` 与 `source/docs/UPDATE_VALIDATION.md`。
