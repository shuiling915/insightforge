# InsightForge

> 企业级生产级数据分析 Agent —— 基于 LLM 的自主数据分析框架，具备沙箱执行、安全防护与可观测能力。

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![License](https://img.shields.io/badge/license-MIT-yellow)

## 功能特性

### 智能分析
- **自主 Agent 循环**：观察 → 规划 → 行动 → 修正，自动完成数据分析任务
- **结构化输出**：基于 JSON Schema + Pydantic 校验模型输出，确保机器可读
- **多模型回退**：通过 LiteLLM 支持 OpenAI / Anthropic / DeepSeek / Azure 等，支持 fallback 链
- **多 Agent 协作**：将复杂任务委派给子 Agent，隔离上下文与权限
- **上下文管理**：自动裁剪历史上下文，适配 token 限制

### 安全沙箱
- **Docker 隔离执行**：代码在临时容器中运行，只读工作区 + 非 root 用户 + 丢弃所有 capabilities
- **静态代码安全分析**：AST 级别拦截 os、subprocess、eval、__builtins__ 等危险模式，支持间接访问检测
- **SQL 写操作拦截**：阻止 DROP、DELETE、ALTER、INSERT 等危险 SQL
- **网络隔离**：默认容器无网络访问，防止数据外泄

### 认证与权限
- **RBAC 权限模型**：admin / analyst / viewer 三级角色
- **JWT 认证**：token 黑名单支持即时吊销，1 小时过期
- **密码策略**：强制复杂度校验（大小写字母 + 数字 + 最小长度）
- **暴力破解防护**：单 IP 5 分钟内最多 5 次登录失败
- **默认管理员强制改密**：首次登录必须修改默认密码

### 企业级安全
- **安全响应头**：CSP、X-Frame-Options、X-Content-Type-Options、Referrer-Policy
- **CORS 白名单**：默认仅允许 localhost，不再是 *
- **输入长度限制**：任务输入最大 4000 字符
- **速率限制**：每分钟请求数限制
- **敏感文件排除**：.env、数据库文件、工作区不入库

### 可观测与评估
- **事件流**：SSE 实时推送 Agent 运行事件（思考、规划、代码执行、结果）
- **评估框架**：内置测试用例、评分标准、通过率报告
- **Prometheus 指标**：/metrics 端点暴露运行指标
- **会话持久化**：基于 SQLite 的会话存储，支持断线恢复

### 用户界面
- **Web 前端**：深色主题，会话管理，实时事件流
- **代码高亮**：语法高亮 + 一键复制
- **Markdown 渲染**：最终答案支持 Markdown 排版
- **管理面板**：用户管理、角色分配、启用/禁用

## 快速开始

### 环境要求
- Python >= 3.11
- Docker（用于沙箱执行，开发环境可用 local 模式）
- LLM API Key（OpenAI / Anthropic / DeepSeek 等兼容 LiteLLM 的服务）

### 本地开发

```bash
git clone https://github.com/shuiling915/insightforge.git
cd insightforge
pip install -e .
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key 和模型配置
INSIGHTFORGE_EXECUTOR_BACKEND=local insightforge serve
# 打开 http://localhost:8787
```

**默认账号**：admin / admin123（首次登录强制修改密码）

### Docker 部署

```bash
docker-compose up -d
docker-compose logs -f insightforge
```

### CLI 使用

```bash
insightforge run "查询 ecommerce.db 中销量最高的商品"
insightforge run "统计各城市销售额" --model gpt-4o --executor local
insightforge evaluate --executor local
```

## 配置说明

### LLM 配置

| 变量 | 说明 | 示例 |
|------|------|------|
| INSIGHTFORGE_MODEL | LiteLLM 模型名 | gpt-4o |
| INSIGHTFORGE_API_KEY | API Key | sk-xxx |
| INSIGHTFORGE_API_BASE | API 地址（可选） | https://api.openai.com/v1 |
| INSIGHTFORGE_FALLBACK_MODELS | 回退模型（逗号分隔） | gpt-4o-mini |
| INSIGHTFORGE_TEMPERATURE | 采样温度 | 0.2 |

### 执行环境

| 变量 | 说明 | 默认值 |
|------|------|--------|
| INSIGHTFORGE_EXECUTOR_BACKEND | 执行器 docker 或 local | docker |
| INSIGHTFORGE_CODE_TIMEOUT | 代码执行超时（秒） | 300 |
| INSIGHTFORGE_DOCKER_IMAGE | 沙箱镜像 | insightforge/sandbox:latest |
| INSIGHTFORGE_DOCKER_NETWORK | 容器网络模式 | none |
| INSIGHTFORGE_DOCKER_MEMORY_LIMIT | 内存限制 | 2g |
| INSIGHTFORGE_DOCKER_CPU_LIMIT | CPU 限制 | 1.0 |

### 认证与安全

| 变量 | 说明 | 默认值 |
|------|------|--------|
| INSIGHTFORGE_REQUIRE_AUTH | 启用认证 | false |
| INSIGHTFORGE_JWT_SECRET | JWT 签名密钥 | - |
| INSIGHTFORGE_CORS_ORIGINS | CORS 白名单 | * |
| INSIGHTFORGE_RATE_LIMIT_PER_MINUTE | 每分钟请求限制 | 60 |
| INSIGHTFORGE_MAX_TASK_LENGTH | 任务输入最大字符数 | 4000 |

生产环境必须设置 INSIGHTFORGE_REQUIRE_AUTH=true 并配置强 JWT_SECRET。

## 项目结构

```
insightforge/
├── src/insightforge/
│   ├── agent/              # Agent 核心
│   ├── auth/               # 认证 JWT RBAC
│   ├── execution/          # 执行器 Docker 沙箱
│   ├── gateway/            # LLM 网关
│   ├── security/           # 安全 代码静态分析
│   ├── schema/             # 数据模型
│   ├── session/            # 会话持久化
│   ├── observability/      # 可观测
│   ├── evaluation/         # 评估框架
│   ├── server/             # FastAPI 服务
│   ├── config.py
│   └── cli.py
├── tests/
├── Dockerfile
├── Dockerfile.sandbox
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## API 概览

### 认证
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /auth/login | 登录返回 JWT |
| POST | /auth/logout | 登出吊销 token |
| POST | /auth/change-password | 修改密码 |
| GET | /auth/me | 获取当前用户 |

### 分析任务
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /runs | 创建分析任务 |
| GET | /runs/{run_id}/stream | SSE 流式事件 |
| POST | /runs/{run_id}/cancel | 取消任务 |

### 会话
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /sessions | 会话列表 |
| DELETE | /sessions/{session_id} | 删除会话 |

### 管理（需 admin）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/users | 用户列表 |
| POST | /auth/register | 创建用户 |
| PUT | /admin/users/{id}/role | 修改角色 |
| PUT | /admin/users/{id}/password | 重置密码 |
| PUT | /admin/users/{id}/active | 启用禁用 |
| DELETE | /admin/users/{id} | 删除用户 |

## 角色权限

| 角色 | 分析任务 | 查看会话 | 用户管理 | 修改密码 |
|------|:-------:|:-------:|:-------:|:-------:|
| admin | 是 | 是 | 是 | 是 |
| analyst | 是 | 是 | 否 | 是 |
| viewer | 否 | 是 | 否 | 是 |

## 开发

```bash
pytest tests/ -v
ruff check src/
INSIGHTFORGE_EXECUTOR_BACKEND=local insightforge serve
```

## 安全说明

1. 沙箱隔离：所有 AI 生成的代码在 Docker 容器中执行，工作区只读，禁止网络访问
2. 代码审查：执行前通过 AST 静态分析拦截危险操作
3. 最小权限：容器以非 root 用户运行，丢弃所有 Linux capabilities
4. 认证授权：生产环境强制 JWT 认证 + RBAC 权限控制
5. 数据保护：敏感配置通过环境变量注入，不入库

## License

MIT