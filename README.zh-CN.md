# GCP Genie

[English](README.md) | **简体中文**

一个基于 [ADK](https://google.github.io/adk-docs/) 构建的智能体，运行于 **Vertex AI
Agent Engine**，并注册到 **Gemini Enterprise** 应用。它从四个方面帮助用户使用 Google
Cloud——并且每一个会访问用户云资源的操作，都使用**该用户自己的 OAuth 权限**执行，绝不
使用服务账号。

## 功能简介

| # | 能力 | 如何保证可信 |
|---|------|--------------|
| 1 | **GCP 文档问答** | 通过 Google 搜索基于公开文档作答，并附上来源引用。 |
| 2 | **gcloud 脚本生成** | 总是先询问所需参数；缺省时给出清晰标注的占位符。每条命令在展示前都会对照 gcloud 自身的命令/参数树校验——不会出现臆造的参数。 |
| 3 | **实时资产清单查询** | 来自 Cloud Asset Inventory 的真实数据（并带有按服务的 REST 回退），范围受登录用户的 IAM 限制。 |
| 4 | **带确认的命令执行** | 使用用户的令牌执行一组精心挑选的**非破坏性**操作，且需经过**三层确认**。破坏性操作绝不执行。 |

## 演示

[![GCP Genie 演示](https://img.youtube.com/vi/qQNCawFOXzo/hqdefault.jpg)](https://youtu.be/qQNCawFOXzo)

▶️ **[在 YouTube 观看演示](https://youtu.be/qQNCawFOXzo)**

## 架构

- **ADK 智能体**（`gcp_genie_agent/`）：一个根智能体协调一个 Google 搜索子智能体，外加
  用于校验、资产查询和执行的函数工具。
- **运行时没有 `gcloud` 可执行文件**。Agent Engine 运行时是一个 Python 沙箱，因此命令
  **校验**使用 gcloud 自带的静态命令树，命令**执行**映射到 Google REST API——两者都无需
  安装 CLI。
- **仅使用用户 OAuth**。Gemini Enterprise 会转发终端用户的 OAuth 令牌；智能体从会话状态中
  读取并用于所有云调用。如果没有用户令牌，则返回 *unauthorized*（未授权）——绝不回退到
  服务账号。

---

## 前置条件

1. **一个已启用结算的 Google Cloud 项目**。
2. 已安装并完成认证的 **[Google Cloud SDK](https://cloud.google.com/sdk/docs/install)**
   （`gcloud`）：`gcloud auth login`。（部署脚本会读取 gcloud 自带的命令树用于离线校验，并
   使用你的 gcloud 身份来注册智能体。）
3. **Python 3.10+**。
4. 一个**已部署的 Gemini Enterprise 应用**（需提前创建）——记下它的**应用 ID（app id）**。
5. 一个 **OAuth 2.0 客户端 ID**（见下文），让智能体能够代表用户执行操作。

### 创建 OAuth 客户端（一次性）

在 GCP 控制台 → **API 和服务 → 凭据**：

1. 配置 **OAuth 同意屏幕**（单一组织选择 Internal 即可），并添加范围
   `https://www.googleapis.com/auth/cloud-platform`（以及 `.../auth/userinfo.email`）。
2. 创建一个类型为 **Web 应用**的 **OAuth 客户端 ID**。
3. 添加如下**已授权的重定向 URI**（需完全一致）：
   ```
   https://vertexaisearch.cloud.google.com/static/oauth/oauth.html
   ```
4. 复制 **客户端 ID** 和 **客户端密钥**——稍后粘贴到部署脚本中。

---

## 部署

一条交互式命令完成全部工作——启用 API、创建暂存桶、打包 CLI 命令树、部署到 Agent
Engine，并注册到 Gemini Enterprise：

```bash
./deploy.sh
```

它会提示输入以下内容（大多数都有合理的默认值）：

| 提示项 | 默认值 | 说明 |
|--------|--------|------|
| GCP 项目 ID | 当前 `gcloud` 项目 | |
| Agent Engine 区域 | `us-central1` | |
| 暂存桶 | `gs://<project>-agent-staging` | 不存在则自动创建 |
| Gemini 模型 | `gemini-3.5-flash` | |
| 模型区域 | `global` | gemini-3.x 由 `global` 端点提供服务 |
| Gemini Enterprise 应用 ID | — | 必填 |
| OAuth 授权 ID | `gcp-genie-oauth` | |
| OAuth 客户端 ID / 密钥 | — | 必填；密钥以隐藏方式读取 |
| OAuth 范围（scopes） | `cloud-platform email` | |
| 要更新的已有推理引擎 | 留空 | 留空 = 新建；填入资源名 = 原地更新 |

每个提示项都可以通过同名环境变量预先设置，以实现非交互式运行，例如：

```bash
GOOGLE_CLOUD_PROJECT=my-proj GE_APP_ID=my-app_123 \
OAUTH_CLIENT_ID=...apps.googleusercontent.com OAUTH_CLIENT_SECRET=... \
ASSUME_YES=1 ./deploy.sh
```

完成后，打开你的 Gemini Enterprise 应用，在提示时**授权该智能体**（这一步会签发用户的
委派令牌），然后即可开始对话。

---

## 使用智能体——示例提示词

**1 · 文档问答**
- “区域级（regional）和多区域级（multi-regional）Cloud Storage 存储桶有什么区别？”
- “Cloud Run 的自动扩缩容是如何工作的？有哪些限制？”

**2 · gcloud 生成**（智能体会询问所需参数）
- “给我一条创建 e2-medium 虚拟机的 gcloud 命令。”
- “生成一条创建带统一桶级访问控制的区域级 GCS 存储桶的脚本。”

**3 · 资产清单**（使用你的权限）
- “列出我的服务账号。”
- “us-central1 中有哪些正在运行的 Compute 实例？”
- “显示带有标签 env=prod 的存储桶。”

**4 · 执行**（需确认——见下文）
- “在 us-central1-a 创建一台 e2-medium 的虚拟机 `my-vm`，并运行它。”
- “帮我启用 Cloud Asset API。”
- “停止 us-central1-a 中的实例 `web-1`。”

你也可以问：**“你能执行哪些操作？”** 来查看允许列表。

---

## 三层执行确认

执行被刻意设计得非常谨慎。当你让智能体运行某条命令时：

1. **解释。** 智能体展示确切的命令，用通俗语言说明它的作用以及会改动什么，提出澄清问题，
   并警告它将修改你的实时 GCP 环境。（破坏性操作到此为止——绝不执行；你会拿到命令自行运行。）
2. **确认知悉。** 你确认命令正确**并且**知悉它会改动你的环境。
3. **确认执行。** 只有在你最终明确说“是的，现在执行”之后，智能体才会运行它——并使用你自己
   的 OAuth 权限。

即使你在一条消息里把所有确认都提前说了，智能体也不会合并这几个步骤。

## 添加更多允许的操作

执行被限制在一组精选的**非破坏性**操作允许列表内。开箱即用包含：

- `compute instances start | stop | reset`
- `compute instances create`
- `compute networks create`
- `storage buckets create`
- `services enable`
- `iam service-accounts create`

一些虽非破坏性但较敏感的操作默认是**受限的（gated）**，例如
`projects add-iam-policy-binding`。要在当前对话中启用某个受限操作，直接提出即可——智能体会
说明启用后允许什么，并在你确认后才开启（`allow_gcloud_operation`）。它仍然会经过完整的三层
确认，并且只使用你的权限。可以问 **“你能执行哪些操作，我可以启用哪些？”** 来查看当前状态。

要永久新增全新的操作，请在
[`gcp_genie_agent/tools/gcloud_exec.py`](gcp_genie_agent/tools/gcloud_exec.py) 中添加一个
处理函数 + 注册表条目（参见 `_OPERATIONS`）。

## 安全模型

- **仅使用用户 OAuth**——所有云调用都使用登录用户转发的令牌；智能体绝不使用其服务账号或
  ADC。没有用户令牌 ⇒ *unauthorized*（未授权）。
- **绝不执行破坏性操作**（delete/destroy/remove/…）；它们会作为命令返回供手动运行。
- 对每条生成的命令对照 gcloud 自身的命令/参数树进行**确定性校验**。
- **仓库中不含任何密钥**。OAuth 客户端密钥仅在部署时输入，并且只通过环境变量传递。

## 故障排查

- **“授权似乎已过期/失效”或 `ACCESS_TOKEN_TYPE_UNSUPPORTED`**——在较旧/长时间运行的对话中，
  转发的令牌可能会失效。请**新建一个对话**（或重新授权该智能体）后重试。（参见 ADK issue
  [#5556](https://github.com/google/adk-python/issues/5556)。）你可以问
  *“inspect my token”* 以获得通俗的健康检查。
- **Gemini Enterprise 显示 “no valid RunAgentResponse … stream data of size 0”**——依赖
  版本不匹配。请保持 [`requirements.txt`](requirements.txt) 中锁定的版本
  （`google-adk==1.34.3`、`google-cloud-aiplatform[agent_engines,adk]==1.154.0`）；较新的
  aiplatform 版本会在 Gemini Enterprise 调用路径上传入锁定版 ADK 的 runner 不接受的参数。
- **模型 404**——`gemini-3.x` 由 `global` 区域提供服务；请将模型区域设为 `global`。

## 仓库结构

```
gcp_genie_agent/
  agent.py                 # 根智能体 + 搜索子智能体 + 工具装配
  tools/
    gcloud_validator.py    # 确定性 gcloud 语法校验
    asset_query.py         # Cloud Asset Inventory + 按服务回退（用户 OAuth）
    gcloud_exec.py         # 带确认的执行、允许列表、令牌诊断
  data/                    # deploy.sh 会把 gcloud_completions.py 取到这里
scripts/
  deploy_agent_engine.py        # 创建/更新 Agent Engine 部署
  register_gemini_enterprise.py # 创建 OAuth 授权 + 注册智能体
deploy.sh                  # 一键交互式部署
requirements.txt
```

## 许可证

Apache License 2.0——见 [LICENSE](LICENSE)。GCP Genie 在部署时会从你本地安装的 Cloud SDK
打包其命令树数据（`gcloud_completions.py`）；该文件由 Google 以 Apache-2.0 许可，本仓库不对
其进行再分发。
