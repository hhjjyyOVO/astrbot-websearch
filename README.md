# WebSearch 网页搜索插件

Agent Reach 驱动的多引擎搜索插件，支持 Exa 语义搜索、Bing/百度抓取、Jina Reader 网页阅读，以及 V2EX / GitHub / B站 平台搜索。爬取全文后用嵌入模型提取相关内容，由 LLM 生成自然对话回复。

## 前置依赖

- （可选）AstrBot 嵌入适配器插件：`astrbot_plugin_embedding_adapter` — 提升内容匹配精度，未安装时自动回退关键词匹配
- （可选）[Agent Reach](https://github.com/Panniantong/Agent-Reach) — 解锁 Exa 语义搜索、Jina Reader 网页阅读、V2EX 社区搜索

## 功能

### LLM 工具

| 工具名 | 功能 | 依赖 |
|--------|------|------|
| `search_web` | 全网搜索（Exa → Bing → 百度自动 fallback） | 零依赖即可用 |
| `search_v2ex` | V2EX 社区热门/节点/关键词搜索 | 需 Agent Reach |
| `search_github` | GitHub 仓库搜索（按星数排序） | 需 gh CLI |
| `search_bilibili` | B站视频搜索 | 零依赖 |

### 手动指令

- `/search <关键词>` / `/网页搜索 <关键词>` — 手动触发搜索
- `/detail <序号>` / `/详情 <序号>` — 查看某条结果的详细内容
- `看第3条` / `看看第二条` — 自然语言触发详情

### 搜索后端链

```
Exa 语义搜索 → Bing 抓取 → 百度抓取 （自动 fallback）
     ↓
Jina Reader → BeautifulSoup 抓取    （自动 fallback）
     ↓
嵌入/关键词提取 → LLM 生成回复
```

## 配置项

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search_backend` | 枚举 | `auto` | `auto`=Exa优先→Bing→百度; `exa_first`=仅Exa; `bing_baidu`=仅传统 |
| `use_jina_reader` | 布尔 | `true` | Jina Reader 优先（失败自动回退） |
| `enable_v2ex` | 布尔 | `true` | 启用 V2EX 社区搜索工具 |
| `enable_github` | 布尔 | `true` | 启用 GitHub 仓库搜索工具 |
| `reply_max_chars` | 整数 | `200` | LLM 回复最大字数 |
| `show_source` | 布尔 | `false` | 回复中是否附带来源链接 |

## 安装

### 基础安装（零依赖即可用）

```bash
cd AstrBot/data/plugins
git clone https://github.com/hhjjyyOVO/astrbot-websearch websearch
pip install -r websearch/requirements.txt
```

此时 `search_web` 和 `search_bilibili` 即可正常使用（Bing + 百度后端）。

### 完整安装（解锁全部渠道）

```bash
# 1. 安装 Agent Reach
pip install https://github.com/Panniantong/agent-reach/archive/main.zip
agent-reach install --env=auto

# 2. 安装 Exa 搜索引擎（免费）
npm install -g mcporter
mcporter config add exa https://mcp.exa.ai/mcp

# 3. 安装 GitHub CLI
# 下载: https://github.com/cli/cli/releases/latest
# 或 winget: winget install --id GitHub.cli
```

完成后重启 AstrBot，插件会自动检测可用渠道并加载。
