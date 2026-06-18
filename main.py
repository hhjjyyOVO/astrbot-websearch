"""
AstrBot Web Search 插件 — LLM 驱动的网页搜索
搜索结果注入对话上下文，由大模型生成自然回复，不在回复中显示网址。
用法:
  直接提问（LLM 自动调用搜索工具）
  /search <关键词>        手动触发搜索
  /detail <序号>          查看网页全文（LLM 总结）
"""
import re
import time
import urllib.parse
from html import unescape

import requests
from bs4 import BeautifulSoup
import urllib3
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CACHE_TTL = 300  # 搜索结果缓存秒数


@register("websearch", "hhjjyy", "LLM驱动的网页搜索插件", "2.0.0")
class WebSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._cache: dict = {}
        logger.info("WebSearch 插件已加载 (LLM驱动)")

    # ── LLM 工具：自动被大模型调用 ──────────

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, query: str):
        """搜索互联网获取实时信息。当需要了解最新新闻、实时数据、或知识库外的公开信息时调用。

        Args:
            query(string): 搜索关键词，用简洁的词组描述要查找的内容
        """
        uid = event.unified_msg_origin
        bing = self._search_bing(query, 3)
        baidu = self._search_baidu(query, 3)
        results = bing + baidu[:(5 - len(bing))] if bing else baidu

        if not results:
            yield event.plain_result(f"未找到与「{query}」相关的搜索结果。")
            return

        # 缓存（供后续 /detail 使用）
        self._cache[uid] = {
            "results": results, "time": time.time(), "query": query,
        }

        # 返回搜索结果（会被注入 LLM 上下文，由 LLM 生成自然回复）
        lines = [f"以下是与「{query}」相关的搜索结果："]

        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")
            body = r.get("body", "")
            lines.append(
                f"[{i}] {title}\n"
                f"    摘要: {body}\n"
                f"    来源: {r.get('href', '')}"
            )

        yield event.plain_result("\n\n".join(lines))

    # ── 手动搜索指令 ──────────────────────

    @filter.command("search")
    async def cmd_search(self, event: AstrMessageEvent, query: str = ""):
        """手动搜索 — /search <关键词>"""
        if not query:
            yield event.plain_result("用法: /search <关键词>")
            return
        async for r in self.search_web(event, query):
            yield r

    @filter.command("网页搜索")
    async def cmd_search_cn(self, event: AstrMessageEvent, query: str = ""):
        if not query:
            yield event.plain_result("用法: /网页搜索 <关键词>")
            return
        async for r in self.search_web(event, query):
            yield r

    # ── 详情查看（也走 LLM）───────────────

    @filter.command("detail")
    async def cmd_detail(self, event: AstrMessageEvent, index: str = ""):
        """查看搜索结果全文，由 LLM 总结 — /detail <序号>"""
        await self._handle_detail(event, index)

    @filter.command("详情")
    async def cmd_detail_cn(self, event: AstrMessageEvent, index: str = ""):
        await self._handle_detail(event, index)

    @filter.regex(r"(?:详细)?看(?:看|下|一下)?第?\s*(\d+)\s*(?:条|个|篇|项)")
    async def on_detail_natural(self, event: AstrMessageEvent):
        msg = event.get_message_str()
        m = re.search(r"第?\s*(\d+)\s*(?:条|个|篇|项)", msg)
        if m:
            await self._handle_detail(event, m.group(1))

    async def _handle_detail(self, event: AstrMessageEvent, index: str):
        uid = event.unified_msg_origin
        cache = self._cache.get(uid)

        if not cache or time.time() - cache["time"] > CACHE_TTL:
            yield event.plain_result("没有最近的搜索结果，请先提问让我搜索。")
            return

        try:
            idx = int(index) - 1
        except ValueError:
            yield event.plain_result(f"请输入有效序号（1-{len(cache['results'])}）")
            return

        if idx < 0 or idx >= len(cache["results"]):
            yield event.plain_result(f"序号超出范围（1-{len(cache['results'])}）")
            return

        target = cache["results"][idx]
        url = target["href"]
        title = target["title"]

        content = self._fetch_page(url)
        if not content:
            yield event.plain_result(f"无法获取网页内容。如需查看原文，请访问: {url}")
            return

        # 注入 LLM 上下文让其总结
        article = (
            f"用户要求查看以下网页的详细内容。请用中文简洁总结要点（200字以内），"
            f"不要列出网址，用自然的对话语气回复：\n\n"
            f"标题: {title}\n"
            f"正文:\n{content[:4000]}"
        )
        yield event.plain_result(article)

    # ── 核心搜索 ──────────────────────────

    def _search_bing(self, query: str, n: int = 3) -> list:
        results = []
        try:
            url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return results
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select("li.b_algo"):
                if len(results) >= n:
                    break
                title_tag = item.select_one("h2 a")
                if not title_tag:
                    continue
                snippet_tag = item.select_one("div.b_caption p, p.b_lineclamp2")
                results.append({
                    "title": title_tag.get_text(strip=True),
                    "href": title_tag.get("href", ""),
                    "body": snippet_tag.get_text(strip=True) if snippet_tag else "",
                })
            logger.info(f"Bing: {len(results)} 条 → {query}")
        except Exception as e:
            logger.warning(f"Bing 失败: {e}")
        return results

    def _search_baidu(self, query: str, n: int = 3) -> list:
        results = []
        try:
            url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"
            resp = self.session.get(url, timeout=10)
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                return results
            soup = BeautifulSoup(resp.text, "html.parser")
            for container in soup.select("div.result, div.c-container"):
                if len(results) >= n:
                    break
                title_tag = container.select_one("h3 a")
                if not title_tag:
                    continue
                abstract_tag = container.select_one(
                    "span.content-right_8Zs40, span.content, div.c-abstract"
                )
                results.append({
                    "title": title_tag.get_text(strip=True),
                    "href": title_tag.get("href", ""),
                    "body": abstract_tag.get_text(strip=True) if abstract_tag else "",
                })
            logger.info(f"百度: {len(results)} 条 → {query}")
        except Exception as e:
            logger.warning(f"百度失败: {e}")
        return results

    def _fetch_page(self, url: str) -> str:
        try:
            resp = self.session.get(url, timeout=12)
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code != 200:
                return ""
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.select(
                "script, style, nav, footer, header, aside, "
                "iframe, noscript, .sidebar, .ad, .nav, .footer, .header"
            ):
                tag.decompose()
            body = (
                soup.find("article") or soup.find("main") or
                soup.find(class_=re.compile(r"content|article|post|entry|body", re.I)) or
                soup.body
            )
            if not body:
                return ""
            text = body.get_text(separator="\n", strip=True)
            text = unescape(text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text[:6000] if len(text) > 6000 else text
        except Exception as e:
            logger.error(f"爬取失败 {url}: {e}")
            return ""

    async def terminate(self):
        self._cache.clear()
        self.session.close()
