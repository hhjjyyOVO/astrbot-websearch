"""
AstrBot Web Search 插件 — Agent Reach 驱动搜索 + 嵌入整理 + LLM 生成回复

后端链: Exa 语义搜索 → Bing → 百度（自动 fallback）
网页阅读: Jina Reader → BeautifulSoup（自动 fallback）
新增平台: V2EX 社区 / GitHub 代码 / B站视频

流程: 搜索 → 爬取网页全文 → 嵌入模型/关键词 提取相关内容 → LLM 生成自然回复
"""
import json
import re
import subprocess
import time
import urllib.parse
from html import unescape

import requests
from bs4 import BeautifulSoup
import urllib3
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Agent Reach 集成（Python API 优先，失败不阻塞插件加载）───
try:
    from agent_reach.channels.web import WebChannel
    _jina = WebChannel()
    _JINA_AVAILABLE = True
except Exception:
    _jina = None
    _JINA_AVAILABLE = False

try:
    from agent_reach.channels.v2ex import V2EXChannel
    _v2ex = V2EXChannel()
    _V2EX_AVAILABLE = True
except Exception:
    _v2ex = None
    _V2EX_AVAILABLE = False

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


def _fetch_page_jina(url: str, timeout: int = 15) -> str:
    """通过 Jina Reader 读取网页，返回干净 Markdown。失败返回空字符串。"""
    if not _JINA_AVAILABLE:
        return ""
    try:
        text = _jina.read(url)
        if text and len(text) > 50:
            return text
        return ""
    except Exception as e:
        logger.debug(f"Jina Reader 失败 {url}: {e}")
        return ""


# ═══════════════════════════════════════════

@register("websearch", "hhjjyy", "Agent Reach驱动搜索+Exa/Bing/百度+Jina+V2EX+GitHub+B站", "2.2.0")
class WebSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context, config=config)
        self._cfg = config or {}
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

        # ── Agent Reach 可用性检测 ──
        self._exa_ok = self._probe_exa()
        self._gh_ok = self._probe_gh()
        self._v2ex_ok = _V2EX_AVAILABLE
        self._jina_ok = _JINA_AVAILABLE

        parts = []
        if self._exa_ok: parts.append("Exa")
        if self._jina_ok: parts.append("Jina")
        if self._v2ex_ok: parts.append("V2EX")
        if self._gh_ok: parts.append("GitHub")
        ar_info = f"Agent Reach: {', '.join(parts)}" if parts else "Agent Reach: 无可用渠道"
        logger.info(f"WebSearch v2.2.0 已加载 ({ar_info})")

    # ── LLM 工具 ───────────────────────────

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, query: str):
        """搜索互联网获取最新信息。当用户询问新闻、天气、实时事件或任何需要联网查询的问题时，必须调用此工具。

        Args:
            query(string): 搜索关键词
        """
        uid = event.unified_msg_origin
        backend = self._cfg.get("search_backend", "auto")

        # 1. 搜索（后端链：Exa → Bing → 百度）
        results = []
        if backend != "bing_baidu":
            results = self._search_exa(query, 5)
            if results:
                logger.info(f"搜索: Exa 返回 {len(results)} 条结果")
        if not results and backend != "exa_first":
            bing = self._search_bing(query, 3)
            baidu = self._search_baidu(query, 3)
            results = bing + baidu[:(5 - len(bing))] if bing else baidu
            if results:
                logger.info(f"搜索: Bing/Baidu 返回 {len(results)} 条结果")

        if not results:
            return "未找到相关搜索结果，请如实告知用户未找到，建议更具体的关键词。"

        self._cache[uid] = {
            "results": results, "time": time.time(), "query": query,
        }

        # 2. 爬取网页全文（Jina Reader 优先 → BS4 fallback）
        pages = []
        use_jina = self._cfg.get("use_jina_reader", True)
        for r in results[:3]:
            text = ""
            if use_jina:
                text = _fetch_page_jina(r["href"])
            if not text:
                text = _fetch_page(r["href"], self.session)
            if text:
                pages.append({
                    "title": r["title"],
                    "url": r["href"],
                    "text": text[:3000],
                })
        logger.info(f"爬取完成: {len(pages)}/{min(3, len(results))} 页")

        # 3. 嵌入/关键词整理相关内容
        if pages:
            context = _extract_relevant_passages(query, pages, adapter=self._adapter, max_chars=2000)
        else:
            context = "\n\n".join(
                f"[{i+1}] {r['title']}\n{r['body']}"
                for i, r in enumerate(results[:5])
            )

        # 4. 返回搜索结果给LLM，按配置控制回复
        max_chars = self._cfg.get("reply_max_chars", 200)
        show_src = self._cfg.get("show_source", False)
        src_rule = "可以附带来源链接" if show_src else "不要列出网址或来源"
        return (
            f"以下是与「{query}」相关的搜索结果。"
            f"请用不超过{max_chars}字的自然对话语气回答用户，{src_rule}：\n\n{context}"
        )

    # ── 平台专用 LLM 工具 ──────────────

    @filter.llm_tool(name="search_v2ex")
    async def search_v2ex(self, event: AstrMessageEvent, query: str = "",
                           node: str = "", hot: bool = False):
        """搜索 V2EX 社区获取技术讨论和问答。当用户想了解开发者社区对某话题的看法、寻找技术方案讨论、或查看 V2EX 热门帖子时使用。

        Args:
            query(string): 搜索关键词，在 V2EX 站内搜索（通过 Exa site:v2ex.com），可为空
            node(string): 节点名称，如 python/tech/jobs/qna/programmers，为空则不限
            hot(bool): True=只看热门帖子，False=按关键词或节点搜索
        """
        if not self._cfg.get("enable_v2ex", True):
            return "V2EX 搜索未启用（请在插件配置中开启 enable_v2ex）。"
        if not self._v2ex_ok:
            return "V2EX 渠道未就绪（需安装 agent-reach: pip install agent-reach）。"

        try:
            if hot:
                topics = _v2ex.get_hot_topics(limit=15)
                label = "V2EX 热门帖子"
            elif node:
                topics = _v2ex.get_node_topics(node, limit=15)
                label = f"V2EX 节点「{node}」最新帖子"
            elif query:
                # 用 Exa 做 site:v2ex.com 搜索
                if self._exa_ok:
                    results = self._search_exa(f"{query} site:v2ex.com", 10)
                    if results:
                        lines = [f"V2EX 搜索「{query}」结果："]
                        for i, r in enumerate(results, 1):
                            lines.append(
                                f"[{i}] {r['title']}\n"
                                f"    {r['href']}\n    {r['body'][:200]}"
                            )
                        return "\n".join(lines)
                return (
                    f"V2EX 站内搜索暂不可用（需 Exa 搜索引擎）。"
                    f"可尝试：https://www.v2ex.com/?q={urllib.parse.quote(query)}"
                )
            else:
                topics = _v2ex.get_hot_topics(limit=10)
                label = "V2EX 热门帖子（无搜索关键词，显示热门）"

            if not topics:
                return f"{label}：暂无结果。"

            lines = [f"{label}："]
            for i, t in enumerate(topics, 1):
                lines.append(
                    f"[{i}] {t['title']} — "
                    f"节点:{t['node_title']}({t['node_name']}) "
                    f"回复:{t['replies']}\n    {t['content'][:120]}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"V2EX 搜索失败: {e}")
            return f"V2EX 搜索失败: {e}"

    @filter.llm_tool(name="search_github")
    async def search_github(self, event: AstrMessageEvent, query: str):
        """搜索 GitHub 代码仓库。当用户想找某个项目、库、框架或开源工具时使用。

        Args:
            query(string): GitHub 搜索关键词
        """
        if not self._cfg.get("enable_github", True):
            return "GitHub 搜索未启用（请在插件配置中开启 enable_github）。"
        if not self._gh_ok:
            return "GitHub 渠道未就绪（需安装 gh CLI: https://cli.github.com）。"

        try:
            r = subprocess.run(
                ["gh", "search", "repos", query,
                 "--sort", "stars", "--limit", "10",
                 "--json", "nameWithOwner,description,stargazersCount,url"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=20,
            )
            if r.returncode != 0:
                return f"GitHub 搜索失败: {r.stderr[:200]}"

            repos = json.loads(r.stdout)
            if not repos:
                return f"未找到与「{query}」相关的 GitHub 仓库。"

            lines = [f"GitHub 搜索「{query}」结果（按星数排序）："]
            for i, repo in enumerate(repos, 1):
                desc = (repo.get("description") or "")[:150]
                stars = repo.get("stargazersCount", 0)
                lines.append(
                    f"[{i}] {repo['nameWithOwner']} ⭐{stars}\n"
                    f"    {desc}\n    {repo['url']}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"GitHub 搜索失败: {e}")
            return f"GitHub 搜索失败: {e}"

    @filter.llm_tool(name="search_bilibili")
    async def search_bilibili(self, event: AstrMessageEvent, query: str):
        """搜索 B站视频。当用户想找 B站上的教程、评测、Vlog 等视频内容时使用。

        Args:
            query(string): 搜索关键词
        """
        try:
            url = (
                "https://api.bilibili.com/x/web-interface/search/all/v2"
                f"?keyword={urllib.parse.quote(query)}&page=1"
            )
            resp = self.session.get(url, timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                return f"B站搜索失败: {data.get('message', '未知错误')}"

            result = data.get("data", {}).get("result", [])
            if not result:
                return "未找到相关 B站视频。"

            lines = [f"B站搜索「{query}」结果："]
            count = 0
            for cat in result:
                if cat.get("data"):
                    for item in cat["data"][:5]:
                        if count >= 10:
                            break
                        title = re.sub(r'<[^>]+>', '', item.get("title", ""))
                        author = item.get("author", "")
                        play = item.get("play", 0)
                        bvid = item.get("bvid", "")
                        desc = (item.get("description", "") or "")[:100]
                        lines.append(
                            f"[{count+1}] {title}\n"
                            f"    UP主: {author} | 播放: {play}\n"
                            f"    https://www.bilibili.com/video/{bvid}\n"
                            f"    {desc}"
                        )
                        count += 1
                if count >= 10:
                    break

            if count == 0:
                return "未找到相关 B站视频。"
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"B站搜索失败: {e}")
            return f"B站搜索失败: {e}"

    # ── 手动指令 ──────────────────────────

    @filter.command("search")
    async def cmd_search(self, event: AstrMessageEvent, query: str = ""):
        if not query:
            yield event.plain_result("用法: /search <关键词>")
            return
        result = await self.search_web(event, query)
        if isinstance(result, str):
            yield event.plain_result(result)

    @filter.command("网页搜索")
    async def cmd_search_cn(self, event: AstrMessageEvent, query: str = ""):
        if not query:
            yield event.plain_result("用法: /网页搜索 <关键词>")
            return
        result = await self.search_web(event, query)
        if isinstance(result, str):
            yield event.plain_result(result)

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
        use_jina = self._cfg.get("use_jina_reader", True)
        text = _fetch_page_jina(t["href"]) if use_jina else ""
        if not text:
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

    # ── Agent Reach 探测 ──────────────────

    @staticmethod
    def _probe_exa() -> bool:
        """检测 mcporter + Exa MCP 是否可用。"""
        try:
            r = subprocess.run(
                ["mcporter", "config", "list"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=8,
            )
            return r.returncode == 0 and "exa" in (r.stdout + r.stderr).lower()
        except Exception:
            return False

    @staticmethod
    def _probe_gh() -> bool:
        """检测 gh CLI 是否可用。"""
        try:
            r = subprocess.run(
                ["gh", "--version"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=8,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ── 搜索引擎 ──────────────────────────

    def _search_exa(self, query: str, n: int = 5) -> list:
        """通过 Exa AI 搜索引擎获取结果。失败返回空列表。"""
        if not self._exa_ok:
            return []
        # 转义查询中的双引号，防止命令注入
        safe_query = query.replace('"', '\\"')
        try:
            r = subprocess.run(
                ["mcporter", "call",
                 f'exa.web_search_exa(query: "{safe_query}", numResults: {n})'],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=20,
            )
            if r.returncode != 0:
                logger.debug(f"Exa 搜索失败: {r.stderr[:200]}")
                return []
            return self._parse_exa_output(r.stdout)
        except Exception as e:
            logger.debug(f"Exa 异常: {e}")
            return []

    @staticmethod
    def _parse_exa_output(raw: str) -> list:
        """解析 mcporter Exa 输出为统一格式 [{title, href, body}]。

        实际格式:
            Title: xxx
            URL: xxx
            Published: xxx
            Author: xxx
            Highlights:
            snippet text...
            ---
        """
        results = []
        blocks = raw.split("\n---")
        for block in blocks:
            lines = block.strip().split("\n")
            entry = {}
            in_highlights = False
            highlights = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("Title:"):
                    entry["title"] = line[6:].strip()
                elif line.startswith("URL:"):
                    entry["href"] = line[4:].strip()
                elif line.startswith("Highlights:"):
                    in_highlights = True
                elif in_highlights and not line.startswith(("Title:", "URL:", "Published:", "Author:")):
                    highlights.append(line)

            if entry.get("title") and entry.get("href"):
                entry["body"] = " ".join(highlights)[:500]
                results.append(entry)
        return results

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
