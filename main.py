"""
AstrBot Web Search 插件 — 百度 + Bing 双引擎网页搜索 + 详情爬取
用法:
  /search <关键词>      开始搜索
  /detail <序号>        查看第N条结果的网页全文
  详细看看第2条          自然语言触发详情
"""
import re
import time
import urllib.parse
import ssl
from html import unescape

import requests
from bs4 import BeautifulSoup
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 搜索结果缓存有效期（秒）
CACHE_TTL = 300


@register("websearch", "hhjjyy", "百度+Bing网页搜索+详情爬取", "1.1.0")
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
        # 用户搜索结果缓存: {user_id: {"results": [...], "time": ts, "query": str}}
        self._cache: dict = {}
        logger.info("WebSearch 插件已加载 (搜索 + 详情爬取)")

    # ── 搜索指令 ──────────────────────────

    @filter.command("search")
    async def search(self, event: AstrMessageEvent, query: str = ""):
        """搜索互联网 — /search <关键词>"""
        if not query:
            yield event.plain_result(
                "用法: /search <关键词>\n"
                "搜索后可 /detail <序号> 查看网页全文"
            )
            return
        result = await self._do_search(event, query)
        yield event.plain_result(result)

    @filter.command("网页搜索")
    async def search_cn(self, event: AstrMessageEvent, query: str = ""):
        """中文别名"""
        if not query:
            yield event.plain_result("用法: /网页搜索 <关键词>")
            return
        result = await self._do_search(event, query)
        yield event.plain_result(result)

    # ── 详情指令 ──────────────────────────

    @filter.command("detail")
    async def detail(self, event: AstrMessageEvent, index: str = ""):
        """查看搜索结果详情 — /detail <序号>"""
        await self._handle_detail(event, index)

    @filter.command("详情")
    async def detail_cn(self, event: AstrMessageEvent, index: str = ""):
        """中文别名 — /详情 <序号>"""
        await self._handle_detail(event, index)

    # ── 自然语言触发 ──────────────────────

    @filter.regex(r"^(?:搜索|帮我搜|查一下|百度一下)\s*(.+)")
    async def on_search(self, event: AstrMessageEvent):
        """自然语言搜索触发"""
        msg = event.get_message_str()
        m = re.match(r"^(?:搜索|帮我搜|查一下|百度一下)\s*(.+)", msg)
        if m and m.group(1).strip():
            result = await self._do_search(event, m.group(1).strip())
            yield event.plain_result(result)

    @filter.regex(r"(?:详细)?看(?:看|下|一下)?第?\s*(\d+)\s*(?:条|个|篇|项)")
    async def on_detail(self, event: AstrMessageEvent):
        """自然语言详情触发 — "详细看第2条" / "看看第3个" """
        msg = event.get_message_str()
        m = re.search(r"第?\s*(\d+)\s*(?:条|个|篇|项)", msg)
        if m:
            await self._handle_detail(event, m.group(1))

    # ── 核心逻辑 ──────────────────────────

    async def _do_search(self, event: AstrMessageEvent, query: str) -> str:
        """执行搜索并缓存结果"""
        uid = event.unified_msg_origin
        bing = self._search_bing(query, 3)
        baidu = self._search_baidu(query, 3)
        source = "Bing" if bing else "百度"
        results = bing + baidu[:(5 - len(bing))] if bing else baidu

        if not results:
            return f"未找到与「{query}」相关的搜索结果。"

        # 缓存
        self._cache[uid] = {
            "results": results,
            "time": time.time(),
            "query": query,
        }

        lines = [f"🔍 {query}  ({source})"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")[:80]
            href = r.get("href", "")
            body = r.get("body", "")[:150]
            lines.append(f"{i}. {title}\n   {href}")
        lines.append("\n💡 回复 /detail <序号> 查看网页全文")
        return "\n\n".join(lines)

    async def _handle_detail(self, event: AstrMessageEvent, index: str):
        """处理详情请求"""
        uid = event.unified_msg_origin

        # 检查缓存
        cache = self._cache.get(uid)
        if not cache or time.time() - cache["time"] > CACHE_TTL:
            yield event.plain_result("没有最近的搜索结果。请先用 /search <关键词> 搜索。")
            return

        try:
            idx = int(index) - 1
        except ValueError:
            yield event.plain_result(f"请输入有效序号（1-{len(cache['results'])}）")
            return

        results = cache["results"]
        if idx < 0 or idx >= len(results):
            yield event.plain_result(f"序号超出范围（1-{len(results)}）")
            return

        target = results[idx]
        url = target["href"]
        title = target["title"]

        yield event.plain_result(f"⏳ 正在获取: {title[:60]}...")

        # 爬取网页内容
        content = self._fetch_page(url)
        if not content:
            yield event.plain_result(f"❌ 无法获取网页内容: {url}")
            return

        # 分段发送（AstrBot 消息有长度限制）
        header = f"📄 {title}\n🔗 {url}\n\n"
        full_text = header + content
        max_len = 2000

        for i in range(0, len(full_text), max_len):
            chunk = full_text[i:i + max_len]
            if i == 0:
                yield event.plain_result(chunk)
            else:
                yield event.plain_result(f"(续) {chunk}")

    def _fetch_page(self, url: str) -> str:
        """爬取网页正文"""
        try:
            resp = self.session.get(url, timeout=12)
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code != 200:
                return ""

            soup = BeautifulSoup(resp.text, "html.parser")

            # 移除无用标签
            for tag in soup.select(
                "script, style, nav, footer, header, aside, "
                "iframe, noscript, .sidebar, .ad, .advertisement, "
                ".nav, .footer, .header, .comment, .menu"
            ):
                tag.decompose()

            # 优先从正文容器提取
            body = (
                soup.find("article") or
                soup.find("main") or
                soup.find(class_=re.compile(r"content|article|post|entry|body|text",
                                            re.I)) or
                soup.find(id=re.compile(r"content|article|post|main|body", re.I)) or
                soup.body
            )

            if not body:
                return ""

            # 提取文本
            text = body.get_text(separator="\n", strip=True)
            # 解码 HTML 实体
            text = unescape(text)
            # 压缩空行
            text = re.sub(r'\n{3,}', '\n\n', text)
            # 限制总长度
            if len(text) > 6000:
                text = text[:6000] + "\n\n...(内容过长已截断)"

            return text

        except requests.Timeout:
            return "请求超时，请稍后重试。"
        except Exception as e:
            logger.error(f"爬取失败 {url}: {e}")
            return ""

    # ── 搜索引擎 ──────────────────────────

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

    async def terminate(self):
        self._cache.clear()
        self.session.close()
