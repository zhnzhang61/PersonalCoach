# PersonalCoach

> 单用户、iPhone 优先的 AI 跑步教练。一个常驻 agent 推理你的 Garmin
> 传感器数据、恢复指标、计划日历、主观 check-in，以及一份累积的长期记忆
> （topics / episodes / 统计模型）。
>
> A single-user, iPhone-first AI running coach: one always-on agent
> reasoning over your Garmin sensor data, recovery metrics, planned
> calendar, subjective check-ins, and an accumulated long-term memory.

## 📖 文档 / Documentation

完整的架构、子系统、路线图、工程债、以及 **Garmin / Google / LangSmith 的
接入步骤**，全在这一份指南里（含语言切换）：

→ **[English](docs/PROJECT_GUIDE.md)** · **[中文](docs/PROJECT_GUIDE.zh.md)**

Everything — architecture, subsystems, roadmap, engineering debt, and
the **Garmin / Google / LangSmith setup runbooks** — lives in that one
guide.

| 想找什么 / Looking for | 去哪 / Where |
|---|---|
| 大图 + 5 个 tab | [§1 Overview](docs/PROJECT_GUIDE.md#1-overview) |
| **Garmin 登录（429 绕过）** | [§3.2 Authentication](docs/PROJECT_GUIDE.md#32-authentication) |
| MCP 工具清单 | [§3.3 MCP tools](docs/PROJECT_GUIDE.md#33-mcp-tools) |
| 记忆 / 模型 / 输入流 | [§3.4.1 Coach brain](docs/PROJECT_GUIDE.md#341-coach-brain--memory-models-input-streams) |
| LangSmith tracing 接入 | [§3.4.4 Observability](docs/PROJECT_GUIDE.md#344-observability--traces--langsmith) |
| 还剩什么没做 | [§4 Engineering debt](docs/PROJECT_GUIDE.md#4-engineering-debt) |

## 🚀 快速开始 / Quick start

```bash
# 后端 / backend
uv run uvicorn backend.api_server:app --port 8765

# 前端 / frontend
cd web && npm run dev          # 或 npm run build && npm run start (prod)
```

Garmin 首次登录（Cloudflare 429 绕过）见
[§3.2](docs/PROJECT_GUIDE.md#32-authentication)；测试 `uv run pytest -q`。

---

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

- [Next.js](https://github.com/vercel/next.js) (MIT) - iPhone 前端框架，承担了 Coach / Health / Activity / Training / Setup 五个 tab。
- [FastAPI](https://github.com/fastapi/fastapi) (MIT) - 后端 HTTP 层，所有数据访问的单一入口。
- [LangGraph](https://github.com/langchain-ai/langgraph) (MIT) - 赋予了 AI 教练复杂的多智能体推理与记忆能力。
- [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) (MIT) - 提供了基础的 Garmin API 封装。
- [pirate-garmin](https://github.com/petergardfjall/pirate-garmin) - 在攻克移动端鉴权逻辑上提供了关键的逆向思路。
- [Pandas](https://github.com/pandas-dev/pandas) - 支撑了底层强大的数据处理。
