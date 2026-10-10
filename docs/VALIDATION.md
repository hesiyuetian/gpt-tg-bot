# 验证记录

验证日期：2026-10-10 UTC。简介采用原文模式。

## 已通过

- Python 3.12 语法/编译检查
- `python3 -m unittest discover -s tests -v`：38 项通过
- 合成 fixture dry-run：3 项，1 个分段，禁止将 fixture 发送到 Telegram
- 已安装 PyYAML 检查 workflow 语法、UTC 12:00 schedule、两个手动选项默认 false、只读权限、原文默认和固定去重步骤名；未为验证额外安装依赖
- 2026-10-10 01:58:31 UTC，通过直接 HTTPS 实时抓取 `https://github.com/trending?since=daily`，解析出 11 项，按 stars today 降序输出；没有造数据补到 20 项
- 当次真实预览 1 个分段，2,605 UTF-16 单位，低于 4,096；仅表示该历史时刻快照，之后说明文案已经调整为原文模式
- 官方 checkout 固定提交已经通过官方发布/提交页核对
- 安全检查：不调用 Telegram 更新或 webhook API；发送单次请求且错误脱敏；同日重复/未知送达保守阻止；Actions Summary 使用 HTML 转义展示公开预览

测试覆盖解析、日新增排序、实体、缺字段、空页、重复仓库、外链、top20、北京时间日期、可选中文覆写哈希、emoji 分段、过期预览、收件人限制、发送默认关闭、原文默认、错误脱敏、历史不完整/重复/取消/重跑、只读 GET 重试、Telegram 单次请求和部分发送后停止。

## 未验证

- 尚未运行真实 GitHub Actions 或配置/启用日常发送
- 尚未添加、读取或使用任何真实 Telegram token/chat ID
- 尚未调用 Telegram，不知道真实收件结果或 bot 现有用途
- Actions token 的历史查询权限和历史可见性尚未端到端测试
- 未核对账户 Actions 额度、未来 HTML 结构或调度准点性
- 没有改动已有邮件自动化，两渠道同稿不属于本版功能

只有配置好 secrets、核对私人接收人并完成一次实际收件测试后，才开启定时变量。
