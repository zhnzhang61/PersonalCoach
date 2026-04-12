## Garmin Error 429

由于 Garmin 目前全面启用了严格的 Cloudflare 防爬虫机制，使用 Playwright 等自动化浏览器登录极易触发 `HTTP 429 Too Many Requests` 和无限验证码死循环。

本方案通过**浏览器手动获取一次性票据 (Service Ticket)**，再用项目脚本**立刻**完成兑换，最终提取出原生 `garminconnect`（Garth）可用的长效 `OAuth2` 通行证。

**安全提示**：不要把 Service Ticket、密码或 token 写进仓库、截图或提交到 git。若曾在 `.venv` 里硬编码过 `ST-...`，请删掉或重装 `pirate-garmin`（见文末「恢复虚拟环境里的包」）。

---

### 步骤 1：手动获取一次性服务票据 (Service Ticket)

> **注意**：Service Ticket (`ST-xxxx`) 是一次性的，且有效期极短（不到 1 分钟），获取后必须**立刻**在终端运行下一步脚本。

1. 打开一个**全新/无痕模式**的浏览器窗口。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 面板。
3. **关键操作**：在 Network 面板顶部勾选 **Preserve log（保留日志）**。
4. 在地址栏输入并访问以下专属移动端登录链接：
  ```text
   https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android
  ```
5. 正常输入账号密码登录（如有真人验证码则手动通过）。
6. 登录成功后，页面会跳转并显示找不到网页 (`This site can't be reached`)，**这是正常现象**。
7. 立刻查看浏览器地址栏，复制**整段 URL**，或只复制 `ticket=` 后面的服务票据：
  ```text
   ST-xxxxxxx-xxxxxxxxxxxxxx-sso
  ```

---

### 步骤 2：一键兑换并写入 Garth（推荐）

无需再修改 `.venv` 里的 `pirate_garmin` 源码。在项目根目录执行：

```bash
# 方式 A：直接把重定向 URL 或 ST 字符串作为参数（最快）
uv run python garmin_ticket_login.py --url "https://...ticket=ST-...."

# 或
uv run python garmin_ticket_login.py --ticket "ST-....-sso"

# 方式 B：无参数运行，按提示粘贴「重定向后的完整 URL」（或只贴 ST-…-sso）
uv run python garmin_ticket_login.py

# 方式 C：先自动打开登录页，再在终端按提示粘贴地址栏 URL
uv run python garmin_ticket_login.py --open-browser
```

脚本会：

1. 用 `pirate_garmin` 的兑换逻辑把 ST 换成长效会话，写入 `~/.local/share/pirate-garmin/native-oauth2.json`（可用 `--app-dir` 覆盖）。
2. 把其中的 DI token 写入 `~/.garth/oauth2_token.json`。

可选参数：

- `--compat`：同时生成 `oauth1_token.json` 与 `domain_profile.json` 占位文件（见步骤 3），兼容仍检查 OAuth1 的旧版 `garminconnect`。
- `--run-sync`：成功后自动执行 `uv run python garmin_sync.py`。

示例（兑换 + 兼容占位 + 拉数据）：

```bash
uv run python garmin_ticket_login.py --url "$PASTED_URL" --compat --run-sync
```

---

### 步骤 3：仅迁移已有 `native-oauth2.json`（可选）

若你已用其他方式生成了 `~/.local/share/pirate-garmin/native-oauth2.json`，只需写入 Garth：

```bash
uv run python migrate.py
```

（`migrate.py` 与 `garmin_ticket_login.py` 共用同一套迁移逻辑。）

---

### 步骤 4：旧版 `garminconnect` 的 OAuth1 检查（可选）

若运行时仍提示缺少 `oauth1_token.json`，使用上面 `**--compat**` 即可；或手动生成占位文件：

```python
import json
import os

garth_dir = os.path.expanduser("~/.garth")

with open(os.path.join(garth_dir, "oauth1_token.json"), "w") as f:
    json.dump({"oauth_token": "dummy", "oauth_token_secret": "dummy"}, f)

with open(os.path.join(garth_dir, "domain_profile.json"), "w") as f:
    json.dump({}, f)

print("✅ 兼容性假文件已生成！")
```

---

### 恢复虚拟环境里的 `pirate_garmin`（若曾修改过 `site-packages`）

不建议长期改 `.venv` 内文件；升级或同步依赖时也会被覆盖。若你曾改 `auth.py` 硬编码票据，可重装该包以恢复上游行为：

```bash
uv sync --reinstall-package pirate-garmin
```

---

### 旧流程（不推荐）：魔改 `pirate-garmin` 再 `login`

若仍需手动改 `create_native_session` 并运行 `pirate-garmin login`，可参考历史提交或备份；**新流程应优先使用 `garmin_ticket_login.py`**，避免修改 `site-packages`。

## 📜 授权与商用协议 (License & Commercial Use)

本项目采用 **双轨授权模式 (Dual Licensing)**，旨在平衡开源共享与知识产权保护：

### 1. 个人与非商业用途 (Personal & Non-Commercial)

本项目代码在 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) 协议下发布。

- **允许**：个人用户免费使用、学习、修改及在非营利环境下运行。
- **禁止**：严禁将本项目核心逻辑、AI 架构或爬虫接口用于任何盈利性产品、付费服务、企业内部商业系统或作为商业 App 的一部分。

### 2. 商业授权 (Commercial Licensing)

如果你希望将本项目的代码或架构（如：Garmin 鉴权绕过逻辑、多层 AI 记忆模型、LangGraph 运动分析流）集成到商业产品、收费 SaaS 或企业级应用中，**必须获得作者的正式书面授权**。

- 如需商用，请联系作者：[zhnzhang61@gmail.com](mailto:zhnzhang61@gmail.com)
- 商业授权将提供更稳定的技术支持建议及免除开源协议中的非商用限制。

---

## ⚖️ 免责声明 (Disclaimer)

1. **非官方关联**：本项目是一个独立开发的个人研究项目，与 **Garmin (佳明)** 官方公司无任何关联、赞助或认可关系。
2. **风险自负**：本项目涉及对 Garmin Connect 非公开 API 的调用。用户在使用过程中应严格遵守 Garmin 的服务条款。因频繁调用、逆向工程等行为导致的账号封禁、数据丢失或任何法律纠纷，**作者概不负责**。
3. **数据安全**：本项目本地运行，不上传任何隐私数据。请勿将包含个人账号、ST 票据或 API Key 的 `.env` 文件及 `data/` 目录上传至任何公共仓库。

---

## 🙏 鸣谢 (Acknowledgements)

本项目站在巨人的肩膀上，感谢以下优秀开源项目提供的支持与灵感：

- [Streamlit](https://github.com/streamlit/streamlit) (Apache 2.0) - 提供了极其优雅的仪表盘前端框架。
- [LangGraph](https://github.com/langchain-ai/langgraph) (MIT) - 赋予了 AI 教练复杂的多智能体推理与记忆能力。
- [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) (MIT) - 提供了基础的 Garmin API 封装。
- [pirate-garmin](https://github.com/petergardfjall/pirate-garmin) - 在攻克移动端鉴权逻辑上提供了关键的逆向思路。
- [Pandas](https://github.com/pandas-dev/pandas) & [Altair](https://github.com/altair-viz/altair) - 支撑了底层强大的数据处理与可视化。

*注：以上引用的第三方库其所有权归原作者所有，并分别遵循其各自的开源协议。*