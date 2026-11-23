# HSC Sheets Sync (Google Sheets → 腾讯文档单向回传)

基于 **GitHub Actions (Public 仓库)** 与 **腾讯文档官方 HTTP MCP (`sheet-mcp`)** 实现的纯云端零成本单向同步工具。

## 核心特点

- **不关闭 Google 高级保护计划**：通过读取 Google Sheets 只读公开导出 CSV（无需主号个人 OAuth 授权），彻底避开 `Error 400: policy_enforced`。
- **零资金成本**：Public 仓库使用 GitHub Actions 标准 Ubuntu Runner 免费不限分钟数；腾讯文档官方个人 MCP 免费用户每日支持 2,000 次调用。
- **保留展示表排版**：推荐在腾讯在线表格拆分为 `_sync_data`（同步纯数据分页，脚本仅清空内容并覆写该分页）+ `展示页`（通过 `='_sync_data'!A1` 引用，完整保护合并单元格与配色）。
- **变化检测节流**：每次同步先比对内容 SHA-256，数据无变化时跳过写入，避免产生冗余编辑记录。

## 使用配置

### 1. 准备 Google 数据源与腾讯目标表
1. 将要同步的 Google 表格设为 **「知道链接的任何人 → 查看者」**，记录链接中的 `SHEET_ID` 与分页 `gid`（默认首表为 `0`）。
2. 在腾讯文档创建一个目标在线表格（或留空 `TENCENT_FILE_ID`，首次运行由脚本自动新建空白测试表），记下 URL 中的文档 ID。

### 2. 获取腾讯个人 MCP Token
1. 登录腾讯文档后访问：[https://docs.qq.com/scenario/open-claw.html?nlc=1](https://docs.qq.com/scenario/open-claw.html?nlc=1)
2. 复制页面第 3 项中的个人 `token`。

### 3. 配置 GitHub Actions
1. 在仓库 **Settings → Secrets and variables → Actions → Secrets** 新增：
   - `TENCENT_DOCS_TOKEN`：填入手中复制的腾讯个人 token（切勿写入仓库文件）。
2. 在 **Variables** 新增（或按 `config.example.json` 格式管理多任务）：
   - `GOOGLE_SHEET_ID`：Google 源表 ID
   - `GOOGLE_GID`：Google 源表分页 GID（默认 `0`）
   - `TENCENT_FILE_ID`：腾讯目标表 ID（首次测试可留空自动创建后再填入固定下来）
   - `TENCENT_SHEET_NAME`（可选）：目标腾讯子表名称（如 `_sync_data`；留空则写首个子表）
3. 前往 **Actions → Sync Google Sheets to Tencent Docs → Run workflow** 即可立即手动触发一次，此后按 `.github/workflows/sync.yml` 中的定时策略自动运行。

## 本地调试与测试

```bash
python -m unittest discover -s tests -v
```
