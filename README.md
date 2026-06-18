# WebSearch 网页搜索插件

百度 + Bing 双引擎搜索，爬取全文，嵌入整理，LLM 生成自然回复。

## 前置依赖

需安装 AstrBot 嵌入适配器插件：`astrbot_plugin_embedding_adapter`

## 功能

- LLM 自动调用 `search_web` 工具搜索互联网
- 搜索后自动爬取网页全文，用嵌入模型提取相关内容
- System Prompt 引导 LLM 生成对话式自然回复
- `/search <关键词>` — 手动触发
- `/detail <序号>` — 查看搜索结果全文

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/hhjjyyOVO/astrbot-websearch websearch
pip install -r websearch/requirements.txt
```
