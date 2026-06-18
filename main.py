"""
AstrBot Web Search 插件 — 搜索 + 嵌入整理 + LLM 生成回复

流程: 搜索 → 爬取网页全文 → 嵌入模型/关键词 提取相关内容 → LLM 按 system prompt 生成自然回复
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

CACHE_TTL = 300



# ── 嵌入适配器（优先用 AstrBot 配置的模型）───
_embed_adapter = None


def _get_embed_adapter(context: Context = None):
    """获取 AstrBot 配置的 embedding 模型，不可用时回退关键词匹配"""
    global _embed_adapter
    if _embed_adapter is not None:
        return _embed_adapter
    if context is None:
        _embed_adapter = False
        return _embed_adapter
    try:
        star = context.get_registered_star("astrbot_plugin_embedding_adapter")
        if star and hasattr(star.star_cls, "get_embeddings"):
            _embed_adapter = star.star_cls
            logger.info(f"使用 AstrBot 嵌入模型: {_embed_adapter.get_model_name()}")
            return _embed_adapter
    except Exception as e:
        logger.warning(f"嵌入适配器未就绪: {e}")
    _embed_adapter = False
    logger.info("嵌入模型不可用，使用关键词匹配")
    return _embed_adapter


# ── 中文分词 ──────────────────────────────

def _tokenize(text: str) -> set:
    """中文 bigram + 英文单词 分词"""
    tokens = set()
    cleaned = re.sub(r'[^一-鿿\w]', ' ', text.lower())
    for i in range(len(cleaned) - 1):
        bigram = cleaned[i:i+2]
        if len(bigram) == 2 and '一' <= bigram[0] <= '鿿' and '一' <= bigram[1] <= '鿿':
            tokens.add(bigram)
    for w in re.findall(r'[a-zA-Z]{2,}', cleaned):
        tokens.add(w)
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── 内容整理 ──────────────────────────────

def _extract_relevant_passages(query: str, pages: list, max_chars: int = 3500,
                               adapter=None) -> str:
    """从多个网页中提取与查询最相关的段落"""
    query_tokens = _tokenize(query)

    # 每页分段
    all_paragraphs = []
    for page in pages:
        paras = [p.strip() for p in page["text"].split("\n") if len(p.strip()) > 20]
        for p in paras:
            all_paragraphs.append({
                "text": p,
                "source": page["title"],
                "url": page["url"],
            })

    if not all_paragraphs:
        return ""

    # 优先用 AstrBot 嵌入模型，不可用时用关键词匹配
    para_texts = [p["text"] for p in all_paragraphs]
    if adapter:
        try:
            all_texts = [query] + para_texts
            embeddings = adapter.get_embeddings(all_texts)
            query_emb = embeddings[0]
            from numpy import dot
            from numpy.linalg import norm
            scores = [dot(query_emb, e) / (norm(query_emb) * norm(e))
                      for e in embeddings[1:]]
        except Exception:
            adapter = None

    if not adapter:
        para_tokens = [_tokenize(p["text"]) for p in all_paragraphs]
        scores = [_jaccard(query_tokens, pt) for pt in para_tokens]

    # 按分数排序
    for i, p in enumerate(all_paragraphs):
        p["score"] = scores[i]
    all_paragraphs.sort(key=lambda x: x["score"], reverse=True)

    # 取高分段落，直到达到字数上限
    seen = set()
    parts = []
    total = 0
    for p in all_paragraphs:
        if p["score"] < 0.02:
            break
        key = p["text"][:50]
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"[来源: {p['source']}]\n{p['text']}")
        total += len(p['text'])
        if total >= max_chars:
            break

    logger.info(f"内容整理: {len(all_paragraphs)}段 → {len(parts)}段 ({total}字)")
    return "\n\n".join(parts)


def _fetch_page(url: str, session: requests.Session) -> str:
    try:
        resp = session.get(url, timeout=12)
        resp.encoding = resp.apparent_encoding or "utf-8"
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select(
            "script, style, nav, footer, header, aside, "
            "iframe, noscript, .sidebar, .ad, .nav, .footer, .header, .menu"
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
        return re.sub(r'\n{3,}', '\n\n', text)
    except Exception as e:
        logger.warning(f"爬取失败 {url}: {e}")
        return ""


# ═══════════════════════════════════════════

@register("websearch", "hhjjyy", "LLM驱动的网页搜索插件(嵌入整理)", "2.1.0")
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
        self._adapter = _get_embed_adapter(context)
        logger.info("WebSearch 插件已加载 (AstrBot嵌入 + LLM生成)")

    # ── LLM 工具 ───────────────────────────

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, query: str):
        """搜索互联网获取实时信息，自动爬取网页全文并提取相关内容。
        当需要了解新闻、实时数据、或知识库外的公开信息时调用。

        Args:
            query(string): 搜索关键词
        """
        uid = event.unified_msg_origin

        # 1. 搜索
        bing = self._search_bing(query, 3)
        baidu = self._search_baidu(query, 3)
        results = bing + baidu[:(5 - len(bing))] if bing else baidu

        if not results:
            yield event.plain_result(f"未找到与「{query}」相关的搜索结果。")
            return

        self._cache[uid] = {
            "results": results, "time": time.time(), "query": query,
        }

        # 2. 爬取网页全文
        pages = []
        for r in results[:3]:
            text = _fetch_page(r["href"], self.session)
            if text:
                pages.append({
                    "title": r["title"],
                    "url": r["href"],
                    "text": text[:5000],
                })
        logger.info(f"爬取完成: {len(pages)}/{min(3, len(results))} 页")

        # 3. 嵌入/关键词整理相关内容
        if pages:
            context = _extract_relevant_passages(query, pages, adapter=self._adapter)
        else:
            # 无全文时用搜索摘要
            context = "\n\n".join(
                f"[{i+1}] {r['title']}\n{r['body']}"
                for i, r in enumerate(results[:5])
            )

        # 4. 返回搜索结果（LLM 会自动根据此信息生成回复）
        yield event.plain_result(
            f"关于「{query}」的搜索结果：\n\n{context}"
        )

    # ── 手动指令 ──────────────────────────

    @filter.command("search")
    async def cmd_search(self, event: AstrMessageEvent, query: str = ""):
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

    # ── 详情 ──────────────────────────────

    @filter.command("detail")
    async def cmd_detail(self, event: AstrMessageEvent, index: str = ""):
        await self._do_detail(event, index)

    @filter.command("详情")
    async def cmd_detail_cn(self, event: AstrMessageEvent, index: str = ""):
        await self._do_detail(event, index)

    @filter.regex(r"(?:详细)?看(?:看|下|一下)?第?\s*(\d+)\s*(?:条|个|篇|项)")
    async def on_detail(self, event: AstrMessageEvent):
        m = re.search(r"第?\s*(\d+)\s*(?:条|个|篇|项)", event.get_message_str())
        if m:
            await self._do_detail(event, m.group(1))

    async def _do_detail(self, event: AstrMessageEvent, index: str):
        uid = event.unified_msg_origin
        cache = self._cache.get(uid)
        if not cache or time.time() - cache["time"] > CACHE_TTL:
            yield event.plain_result("没有最近的搜索结果，请先提问让我搜索。")
            return
        try:
            idx = int(index) - 1
        except ValueError:
            yield event.plain_result(f"序号范围 1-{len(cache['results'])}")
            return
        if idx < 0 or idx >= len(cache["results"]):
            yield event.plain_result(f"序号范围 1-{len(cache['results'])}")
            return

        t = cache["results"][idx]
        text = _fetch_page(t["href"], self.session)
        if not text:
            yield event.plain_result(f"无法获取网页内容。原文: {t['href']}")
            return

        context = _extract_relevant_passages(cache["query"], [{
            "title": t["title"], "url": t["href"], "text": text,
        }], adapter=self._adapter)
        yield event.plain_result(
            f"用户想查看第{idx+1}条结果的详细内容：\n\n{context}"
        )

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
                t = item.select_one("h2 a")
                if not t:
                    continue
                s = item.select_one("div.b_caption p, p.b_lineclamp2")
                results.append({
                    "title": t.get_text(strip=True),
                    "href": t.get("href", ""),
                    "body": s.get_text(strip=True) if s else "",
                })
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
            for c in soup.select("div.result, div.c-container"):
                if len(results) >= n:
                    break
                t = c.select_one("h3 a")
                if not t:
                    continue
                a = c.select_one("span.content-right_8Zs40, span.content, div.c-abstract")
                results.append({
                    "title": t.get_text(strip=True),
                    "href": t.get("href", ""),
                    "body": a.get_text(strip=True) if a else "",
                })
        except Exception as e:
            logger.warning(f"百度失败: {e}")
        return results

    async def terminate(self):
        self._cache.clear()
        self.session.close()
