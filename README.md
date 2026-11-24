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
3. 前往 **Actions → Sync Google Sheets to Tencent Docs → Run workflow** 按需手动触发即可；当前已关闭自动定时检查（如需自动周期检查，可在 [sync.yml](.github/workflows/sync.yml) 中取消 `schedule` 段注释）。
4. 多表/多分页同步可参考 [config.example.json](config.example.json) 与 [examples/hscm_practice.json](examples/hscm_practice.json)，触发时在 `config_path` 填入路径即可覆盖单表环境变量。

## 使用边界与注意事项

- **数据源必须公开只读**：通过 Google Sheets 公开导出 CSV 读取数据，**未公开图池、裁判后台备注或隐私数据不要使用本方式**，也勿将私密表 ID 写入 Public 仓库文件。
- **URL 与公式导出特点**：公开 CSV 仅导出显示文本；`HYPERLINK("url", "文字")` 公式会丢失超链接地址，谱面链接请直接在单元格存放完整 URL 文本。带千分位逗号或 `%` 文本按原样字符串写入展示，若需参加公式汇总建议在源表保留纯数值。
- **子表约束与重试**：新建子表名需控制在 **31 字符**内且不含 `: \ / ? * [ ]`；脚本已内置 `429/5xx` 与网络瞬断退避重试、超大表分块读/清以及多任务故障隔离（单表异常不中断其余分页）。
- **个人 Token 有效期**：如后续遭遇鉴权错误，重新登录腾讯文档获取个人 Token 并更新 GitHub Secret `TENCENT_DOCS_TOKEN` 即可。

## 本地调试与测试

```bash
python -m unittest discover -s tests -v
```
