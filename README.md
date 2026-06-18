# WebSearch 网页搜索插件

百度 + Bing 双引擎网页搜索，支持详情爬取。

## 功能

- `/search <关键词>` — 网页搜索，Bing 优先百度回退
- `/detail <序号>` — 查看搜索结果全文
- 自然语言触发：`搜索xxx`、`帮我搜xxx`、`详细看看第N条`
- 搜索结果缓存 5 分钟，支持继续追问查看详情

## 安装

```bash
# 手动安装
cd AstrBot/data/plugins
git clone https://github.com/hhjjyyOVO/astrbot-websearch websearch
pip install -r websearch/requirements.txt
```

## 使用示例

```
/search 华中农业大学
/detail 1
详细看看第2条
```
