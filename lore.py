# -*- coding: utf-8 -*-
"""泡泡的「肚子里的资料」：作品的自动查资料 + 自动总结 + 按需检索。

为什么有这一层
--------------
titles.json 里那条「作品小抄」只够她说一句「哦，这是绝区零」，再往下就露馅：
玩法、术语、界面、角色一个都不知道；而把大段百科常驻塞进提示词，她又会逮着
刚学到的名词乱抖（之前那个「角色对照」就是这么翻车的）。所以改成两层：

  1. **平时不注入**：只有画面/问题里出现了相关线索，才从手册里检索一两段递给她，
     并且明确写「用不上就一个字都别提」。
  2. **手册是她自己攒的**：看到不认识的游戏/番剧，后台去萌娘百科 + B站攻略视频抓原料，
     用模型总结成固定结构的手册，存 `lore/games/<slug>.md`，隔一阵自动更新。

目录
----
lore/games/<slug>.md      手册（第一行是 `<!-- paopao-lore {...} -->` 元数据）
lore/progress/<slug>.md   老大在这款游戏里的进度/偏好（从识屏账本提炼）
lore/raw/<slug>/*.txt     抓到的原料（留着重总结）

给网关用的接口
--------------
    lore = Lore(cfg, gateway)
    lore.block(app="...", title="...", query="...")   # 识屏/语音时取一小段资料
    lore.ensure(name, kind, aliases=[...])            # 排队去查（后台，不挡回复）
    lore.learn_now(name, kind)                        # 同步查 + 总结（CLI / 接口用）
    lore.name_for(app, title)                         # 这个前台作品叫什么、什么类型

命令行（薄客户端，走 9888 的 /lore 接口；list/show 直接读本地文件）：
    python lore.py list
    python lore.py show 绝区零
    python lore.py learn 绝区零 --kind game
    python lore.py search "这个界面是干嘛的" --game 绝区零
    python lore.py status
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

log = logging.getLogger("petgw")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TITLES_PATH = os.path.join(APP_DIR, "titles.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MOEGIRL_API = "https://zh.moegirl.org.cn/api.php"
DEFAULT_BILI = r"C:\QQBot\Python312\Scripts\bili.exe"

META_RE = re.compile(r"<!--\s*paopao-lore\s*(\{.*?\})\s*-->", re.S)
PROGRESS_META_RE = re.compile(r"<!--\s*paopao-progress\s*(\{.*?\})\s*-->", re.S)
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?")
DATE_TOKEN_RE = re.compile(r"20\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?"
                           r"|20\d{2}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?")
# 「时效」里不该出现的东西：公司新闻/官司/版权这类，跟陪玩没关系
BAD_TIMELY = ("起诉", "诉讼", "版权", "侵权", "判决", "案件", "纠纷", "官司", "索赔",
              "赔偿", "律师函", "商标", "专利")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")

# 识屏提示词里那些套话：它们字多、还都出现，留着会把检索带偏
BOILERPLATE = (
    "身份提示", "上传前的屏幕截图", "参考元数据", "窗口标题只是参考",
    "这是老大当前的屏幕截图", "这是你开口说话那一刻的屏幕",
    "【以下是上传的屏幕截图", "本次元数据",
)

# 桌宠的识屏提示词里整句整句的套话：留着只会把检索带偏
BOILER_SENTENCES = (
    "用你的人设口吻", "画面里正在发生", "窗口标题只是参考", "逐字念出", "逐条罗列",
    "陌生软件", "与己无关", "桌面上的化身", "请以第一人称", "我在你桌面上呢",
    "关心、吐槽、好奇", "不要只围绕标题", "参考信息", "回应一两句就好",
)

HEADINGS_GAME = ("一句话", "怎么玩", "操作与战斗", "界面长什么样", "术语黑话", "角色",
                 "版本沿革", "时效")
HEADINGS_ANIME = ("一句话", "讲的是什么", "主要人物", "角色简称", "梗和术语", "时效")

# 问句里的万能词：它们几乎每段都有，拿来算分只会把真正相关的挤下去
STOP_BIGRAMS = {    "什么", "怎么", "这个", "那个", "是什", "么意", "意思", "么样", "一个", "可以",
    "现在", "今天", "这样", "那样", "为什", "么是", "怎样", "如何", "哪些", "哪个",
    "告诉", "知道", "觉得", "感觉", "应该", "是不是", "有没有", "的话", "一下",
}

# 这些词一出现，基本就是在问某个小节（角色/界面/术语…）：命中就把那一节提到最前面
HEADING_WORDS = {
    "角色", "人物", "界面", "术语", "黑话", "玩法", "操作", "战斗", "版本", "时效",
    "卡池", "剧情", "故事", "配置", "键位",
}

# 一句话里的「俗称」→ 手册里的小节名：补进查询里，才检索得到对的那一节
QUERY_EXPAND = (
    ("梗", "梗和术语"), ("弹幕", "梗和术语"), ("名场面", "梗和术语"),
    ("番", "讲的是什么"), ("动漫", "讲的是什么"), ("动画", "讲的是什么"),
    ("剧情", "讲的是什么"), ("演到哪", "讲的是什么"),
    ("人物", "主要人物 角色"), ("角色", "角色 主要人物"),
    ("界面", "界面长什么样"), ("按钮", "界面长什么样"), ("入口", "界面长什么样"),
    ("按键", "操作与战斗"), ("操作", "操作与战斗"), ("连招", "操作与战斗"),
    ("版本", "时效"), ("卡池", "时效"), ("活动", "时效"), ("更新", "时效"),
)


# --------------------------------------------------------------------- 小工具
def _has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s or ""))


def _norm(name: str) -> str:
    """把名字压成「只剩字」的样子，用来判断两个叫法是不是同一部作品。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(name or "").lower())


def _same_name(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return min(len(na), len(nb)) >= 2
    return False


def _slug(name: str) -> str:
    """文件名：留中文/字母/数字，其余换 -，太长就用短哈希。"""
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(name or "").strip()).strip("-").lower()
    if not base:
        base = "x"
    if len(base) > 48:
        base = base[:40] + "-" + hashlib.md5(str(name).encode("utf-8")).hexdigest()[:6]
    return base


def _now() -> float:
    return time.time()


def _http_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _http_get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _wiki_plain(text: str) -> str:
    """萌娘百科的纯文本里还有 `== 小节 ==` 这类标记，转成 markdown 给总结模型看。"""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^(=+)\s*(.+?)\s*=+\s*$", line.strip())
        if m:
            level = min(4, max(2, len(m.group(1))))
            out.append("#" * level + " " + m.group(2).strip())
        else:
            out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_+-]*\s*\n?", "", text)
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _parse_date(s: str) -> _dt.date | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None


def sanitize_dates(markdown: str, sources: list[tuple[str, str]]) -> str:
    """只保留原料里真出现过的日期。

    模型特别爱在「时效」里拿今天的日期充数（提示词里只要出现一个示例日期它就会抄），
    这里按原料里实际出现过的日期核对一遍：对不上的日期直接抹掉，宁可这条没有日期。
    """
    real: set[str] = set()
    for _, text in sources:
        for m in DATE_TOKEN_RE.finditer(text or ""):
            d = _parse_date(m.group(0))
            if d:
                real.add(d.isoformat())

    def fix_line(line: str) -> str:
        def repl(m: re.Match) -> str:
            d = _parse_date(m.group(0))
            return m.group(0) if (d and d.isoformat() in real) else ""

        new = DATE_TOKEN_RE.sub(repl, line)
        if new == line:
            return line
        new = re.sub(r"[（(]\s*[)）]", "", new)
        new = re.sub(r"[（(]\s*$", "", new)
        new = re.sub(r"[（(]\s*(日期|原料|具体日期)?\s*(没|未)(写|提)[^)）]*[)）]", "", new)
        return new.rstrip(" -—·、,，")

    out: list[str] = []
    section = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            out.append(line)
            continue
        if section in ("时效", "版本", "当前版本", "卡池", "活动", "更新"):
            if any(w in line for w in BAD_TIMELY):
                continue
            if line.lstrip().startswith(("-", "*")):
                line = fix_line(line)
                d = _parse_date(line)
                # 「时效」只留真带日期的条目：没日期的放这儿永远不过期，等于假时效
                if not d or d.isoformat() not in real:
                    continue
                if not line.strip(" -—·*、"):
                    continue
        out.append(line)
    text = "\n".join(out)
    # 一整节都被清空了就把它删掉，别在手册里留个空标题
    text = re.sub(r"\n## (时效|版本|当前版本|卡池|活动|更新)\s*\n+(?=## |\Z)", "\n", text)
    return text


def clean_query(text: str) -> str:
    """把识屏提示词里的套话去掉，只留「标题 + 真正的问话」。

    注意：问话经常跟套话挤在同一行（「这是老大当前的屏幕截图。这个界面是干嘛的？」），
    所以是**按短语抠掉**，不是整行丢掉——整行丢会把问话一起扔掉。
    """
    keep: list[str] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "|" in s and ("参考元数据" in s or "本次元数据" in s):
            keep.append(s.split("|", 1)[1].strip(" ）)"))
            continue
        for b in BOILERPLATE:
            s = s.replace(b, " ")
        s = re.sub(r"^[（(].*?[)）]\s*$", " ", s)     # 整行只剩一个括号说明
        parts = re.split(r"(?<=[。！？!?；;])", s)
        s = " ".join(p for p in parts
                     if p.strip() and not any(b in p for b in BOILER_SENTENCES))
        s = re.sub(r"\s+", " ", s).strip(" 　，,。；;")
        if len(s) >= 2:
            keep.append(s)
    return " ".join(keep)[:800]


# --------------------------------------------------------------------- 分词
def tokenize(text: str) -> list[str]:
    """中文按 bigram、英文数字按整词。够用、不用装分词库。"""
    out: list[str] = []
    for m in TOKEN_RE.finditer((text or "").lower()):
        tok = m.group(0)
        if tok[0].isascii():
            out.append(tok)
        elif len(tok) == 1:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return out


# --------------------------------------------------------------------- 手册仓库
class Store:
    """lore/ 目录的读取、分块、检索。按 mtime 自动重载，网关不用重启。"""

    def __init__(self, base_dir: str, stale_days: float = 60.0, chunk_chars: int = 380):
        self.base = base_dir
        self.games_dir = os.path.join(base_dir, "games")
        self.anime_dir = os.path.join(base_dir, "anime")
        self.progress_dir = os.path.join(base_dir, "progress")
        self.raw_dir = os.path.join(base_dir, "raw")
        for d in (self.games_dir, self.anime_dir, self.progress_dir, self.raw_dir):
            os.makedirs(d, exist_ok=True)
        self.stale_days = max(1.0, float(stale_days))
        self.chunk_chars = max(120, int(chunk_chars))
        self.lock = threading.RLock()
        self.entries: dict[str, dict] = {}
        self.chunks: list[dict] = []
        self.searcher: Searcher | None = None
        self._sig = ""
        self._checked_at = 0.0

    # ---- 读
    def _files_sig(self) -> str:
        parts = []
        for d in (self.games_dir, self.anime_dir):
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".md"):
                        p = os.path.join(d, fn)
                        parts.append("%s:%s:%d" % (os.path.basename(d), fn,
                                                    int(os.path.getmtime(p))))
            except OSError:
                pass
        return "|".join(parts)

    def ensure_fresh(self, max_age: float = 2.0) -> None:
        now = _now()
        if now - self._checked_at < max_age:
            return
        self._checked_at = now
        sig = self._files_sig()
        if sig != self._sig:
            self.reload()

    def reload(self) -> None:
        with self.lock:
            sig = self._files_sig()
            entries: dict[str, dict] = {}
            chunks: list[dict] = []
            for d in (self.games_dir, self.anime_dir):
                for fn in sorted(os.listdir(d)):
                    if not fn.endswith(".md"):
                        continue
                    path = os.path.join(d, fn)
                    try:
                        with open(path, "r", encoding="utf-8-sig") as f:
                            raw = f.read()
                    except OSError as exc:
                        log.warning("手册读不了（%s）：%s", fn, exc)
                        continue
                    slug = fn[:-3]
                    meta, body = self._parse(raw, slug)
                    body = self._filter_stale(body)
                    entries[slug] = meta
                    chunks.extend(self._chunk(slug, meta, body))
            self.entries = entries
            self.chunks = chunks
            self.searcher = Searcher(chunks)
            self._sig = sig
            log.info("资料库：%d 本手册 / %d 段", len(entries), len(chunks))

    def _parse(self, raw: str, slug: str) -> tuple[dict, str]:
        meta: dict = {}
        m = META_RE.search(raw)
        if m:
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    meta = obj
            except ValueError:
                pass
            raw = META_RE.sub("", raw)
        lines = raw.splitlines()
        # 第一个 # 标题当名字
        for line in lines:
            if line.startswith("# "):
                meta.setdefault("name", line[2:].strip())
                break
        meta.setdefault("name", slug)
        meta.setdefault("kind", "")
        meta.setdefault("aliases", [])
        meta["slug"] = slug
        meta["name"] = str(meta.get("name") or slug).strip()
        aliases = {str(a).strip().lower() for a in (meta.get("aliases") or []) if str(a).strip()}
        name = meta["name"]
        aliases.add(name.lower())
        aliases.add(slug)
        aliases.add(re.sub(r"[（(].*?[)）]", "", name).strip().lower())
        aliases.add(name.replace("：", ":").lower())
        aliases.add(name.replace(":", "：").lower())
        meta["aliases"] = [a for a in aliases if len(a) >= 2 or _has_cjk(a)]
        meta["kind"] = str(meta.get("kind") or "").strip().lower()
        try:
            meta["updated"] = float(meta.get("updated") or 0)
        except (TypeError, ValueError):
            meta["updated"] = 0.0
        return meta, raw

    def _filter_stale(self, body: str) -> str:
        """「时效」这类会过期的段落：带日期又太老的条目直接丢掉。"""
        today = _dt.date.today()
        out: list[str] = []
        section = ""
        for line in body.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
                out.append(line)
                continue
            if section in ("时效", "版本", "当前版本", "卡池", "活动", "更新"):
                d = _parse_date(line)
                if d and (today - d).days > self.stale_days:
                    continue
            out.append(line)
        text = "\n".join(out)
        # 整个「## 时效」被清空就一起删掉，别留个空标题
        text = re.sub(r"\n## (时效|版本|当前版本|卡池|活动|更新)\s*\n+(?=## |\Z)", "\n", text)
        return text

    def _chunk(self, slug: str, meta: dict, body: str) -> list[dict]:
        name = meta.get("name") or slug
        kind = meta.get("kind") or ""
        out: list[dict] = []
        heading = ""
        buf: list[str] = []

        def flush() -> None:
            text = "\n".join(buf).strip()
            buf.clear()
            if not text:
                return
            for piece in self._split(text):
                out.append({"slug": slug, "name": name, "kind": kind,
                            "heading": heading, "text": piece})

        for line in body.splitlines():
            if line.startswith("## "):
                flush()
                heading = line[3:].strip()
                continue
            if line.startswith("# "):
                continue
            buf.append(line)
        flush()
        return out

    def _split(self, text: str) -> list[str]:
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) <= self.chunk_chars:
            return [text]
        pieces: list[str] = []
        cur = ""
        # 先按行/句切，攒到差不多一段就收
        for seg in re.split(r"(?<=[。！？!?；;\n])", text):
            seg = seg.strip()
            if not seg:
                continue
            if cur and len(cur) + len(seg) > self.chunk_chars:
                pieces.append(cur.strip())
                cur = ""
            cur += seg
        if cur.strip():
            pieces.append(cur.strip())
        return pieces or [text]

    # ---- 查
    def entry(self, slug: str) -> dict:
        return self.entries.get(slug) or {}

    def resolve(self, *, name: str = "", app: str = "", title: str = "") -> str:
        """这句/这个窗口说的是哪本手册（返回 slug，没命中返回空串）。"""
        self.ensure_fresh()
        direct = _slug(name) if name else ""
        if direct and direct in self.entries:
            return direct
        hay = ("%s %s %s" % (name or "", app or "", title or "")).lower()
        if not hay.strip():
            return ""
        best, best_len = "", 0
        for slug, meta in self.entries.items():
            for al in meta.get("aliases") or []:
                if not al or al not in hay:
                    continue
                # 英文短别名（zzz 之类）容易误命中，要求 3 个字符以上
                if al.isascii() and len(al) < 3:
                    continue
                if len(al) > best_len:
                    best, best_len = slug, len(al)
        return best

    def search(self, query: str, slugs: set[str] | None = None, k: int = 3) -> list[dict]:
        self.ensure_fresh()
        if not self.searcher or not query.strip():
            return []
        extra = [canon for key, canon in QUERY_EXPAND if key in query]
        if extra:
            query = query + " " + " ".join(extra)
        hits = self.searcher.search(query, slugs=slugs, k=k)
        return hits

    # ---- 写
    def save(self, name: str, kind: str, markdown: str, sources: list[str],
             aliases: list[str] | None = None) -> str:
        slug = _slug(name)
        markdown = _strip_fence(markdown)
        if not markdown.lstrip().startswith("# "):
            markdown = "# %s\n\n%s" % (name, markdown)
        meta = {
            "name": name,
            "kind": kind or "",
            "aliases": sorted({str(a).strip() for a in (aliases or []) if str(a).strip()}
                              | {name}),
            "sources": [str(s) for s in (sources or [])][:8],
            "updated": int(_now()),
            "auto": True,
        }
        text = "<!-- paopao-lore %s -->\n%s\n" % (
            json.dumps(meta, ensure_ascii=False), markdown.strip())
        target_dir = self.anime_dir if (kind or "").lower() == "anime" else self.games_dir
        path = os.path.join(target_dir, slug + ".md")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
        self._checked_at = 0.0
        self.reload()
        return path

    def save_progress(self, slug: str, lines: list[str], rows: int) -> str:
        path = os.path.join(self.progress_dir, slug + ".md")
        meta = {"updated": int(_now()), "rows": int(rows)}
        text = "<!-- paopao-progress %s -->\n%s\n" % (
            json.dumps(meta, ensure_ascii=False), "\n".join(lines).strip())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
        return path

    def read_progress(self, slug: str, max_age_days: float) -> str:
        path = os.path.join(self.progress_dir, slug + ".md")
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except OSError:
            return ""
        m = PROGRESS_META_RE.search(raw)
        updated = 0.0
        if m:
            try:
                updated = float(json.loads(m.group(1)).get("updated") or 0)
            except ValueError:
                updated = 0.0
            raw = PROGRESS_META_RE.sub("", raw)
        if updated and (_now() - updated) > max_age_days * 86400:
            return ""
        return raw.strip()

    def save_raw(self, slug: str, label: str, text: str, keep: int = 6) -> None:
        d = os.path.join(self.raw_dir, slug)
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", label)[:30] or "raw"
        path = os.path.join(d, "%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), safe))
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            olds = sorted((os.path.join(d, fn) for fn in os.listdir(d) if fn.endswith(".txt")),
                          key=os.path.getmtime)
            for old in olds[:-max(1, keep)]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError as exc:
            log.debug("原料没存下（不影响手册）：%s", exc)


class Searcher:
    """BM25（中文 bigram）。手册都是几百段，全量打分也就毫秒级。"""

    K1 = 1.2
    B = 0.75

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.tfs: list[Counter] = []
        self.heads: list[set[str]] = []
        self.df: Counter = Counter()
        self.lens: list[int] = []
        for ch in chunks:
            # 不把作品名算进正文：否则「只有标题、没有问话」时每一段都能命中，
            # 她会每轮随机抖三条资料出来（正是要避免的「报菜名」）。
            toks = tokenize("%s %s" % (ch.get("heading") or "", ch.get("text") or ""))
            tf = Counter(toks)
            self.tfs.append(tf)
            self.heads.append(set(tokenize(ch.get("heading") or "")))
            self.df.update(tf.keys())
            self.lens.append(len(toks))
        self.n = len(chunks)
        self.avgdl = (sum(self.lens) / self.n) if self.n else 1.0

    def search(self, query: str, slugs: set[str] | None = None, k: int = 3) -> list[dict]:
        qt = [t for t in tokenize(query) if t not in STOP_BIGRAMS]
        if not qt or not self.n:
            return []
        qset = Counter(qt)
        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self.tfs):
            if slugs and self.chunks[i]["slug"] not in slugs:
                continue
            score = 0.0
            for tok, qn in qset.items():
                f = tf.get(tok, 0)
                if not f:
                    continue
                idf = math.log(1.0 + (self.n - self.df[tok] + 0.5) / (self.df[tok] + 0.5))
                denom = f + self.K1 * (1.0 - self.B + self.B * self.lens[i] / self.avgdl)
                score += idf * f * (self.K1 + 1.0) / denom * min(qn, 2)
                if tok in self.heads[i]:
                    score += idf * 2.5          # 问的词正好是小节名：这一节八成就是答案
            if score > 0:
                if slugs and self.chunks[i]["slug"] in slugs:
                    score *= 1.35          # 当前正在玩的，优先一点
                scored.append((score, i))
        scored.sort(reverse=True)
        if not scored:
            return []
        # 问的正好是某个小节（「角色」「界面」「术语」…）：先把那一节提到前面，
        # 免得「角色」这两个字在别的段落里出现两次就把「角色」小节挤下去。
        prio: list[int] = []
        for tok in qset:
            if tok not in HEADING_WORDS:
                continue
            for i, head in enumerate(self.heads):
                if tok in head and i not in prio:
                    if not slugs or self.chunks[i]["slug"] in slugs:
                        prio.append(i)
        if prio:
            rest = [(s, i) for s, i in scored if i not in prio]
            ordered = [(next(s for s, j in scored if j == i), i) for i in prio] + rest
        else:
            ordered = scored
        top = ordered[0][0]
        out = []
        for score, i in ordered[:max(1, k)]:
            if score < max(0.5, top * 0.28):
                continue
            ch = dict(self.chunks[i])
            ch["score"] = round(score, 3)
            out.append(ch)
        return out


# --------------------------------------------------------------------- 抓资料
class Collector:
    """萌娘百科 + B站攻略视频。抓不到就少写，绝不编。"""

    def __init__(self, cfg: dict):
        conf = cfg.get("lore") or {}
        self.moegirl = bool((conf.get("moegirl") or {}).get("enabled", True))
        self.moegirl_chars = int((conf.get("moegirl") or {}).get("max_chars", 12000))
        bili = conf.get("bili") or {}
        self.bili_enabled = bool(bili.get("enabled", True))
        self.bili_videos = max(0, int(bili.get("videos", 2)))
        self.bili_chars = int(bili.get("max_chars", 2400))
        self.bili_timeout = float(bili.get("timeout", 150))
        cli = str(bili.get("cli") or cfg.get("bili_cli") or "bili")
        if not os.path.isabs(cli) and not shutil.which(cli):
            cli = DEFAULT_BILI if os.path.exists(DEFAULT_BILI) else cli
        self.bili_cli = cli

    # ---- 萌娘百科
    def moegirl_page(self, name: str) -> list[tuple[str, str]]:
        if not self.moegirl:
            return []
        variants = []
        for v in (name, name.replace("：", ":"), name.replace(":", "："),
                  re.sub(r"[（(].*?[)）]", "", name).strip()):
            v = v.strip()
            if v and v not in variants:
                variants.append(v)
        for title in variants:
            url = "%s?action=query&prop=extracts&explaintext=1&redirects=1&format=json&titles=%s" % (
                MOEGIRL_API, urllib.parse.quote(title))
            try:
                data = _http_json(url)
            except Exception as exc:  # noqa: BLE001
                log.info("萌娘百科连不上（%s）：%s", title, exc)
                continue
            pages = ((data.get("query") or {}).get("pages") or {})
            for page in pages.values():
                if page.get("missing") is not None:
                    continue
                text = (page.get("extract") or "").strip()
                if len(text) < 200:
                    continue
                real = str(page.get("title") or title)
                return [("萌娘百科·" + real, _wiki_plain(text)[:self.moegirl_chars])]
        return []

    # ---- B站
    def _bili(self, args: list[str]) -> dict | None:
        cmd = [self.bili_cli, "--compact"] + args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.bili_timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            log.info("bili 调不动（%s）：%s", " ".join(args[:3]), exc)
            return None
        out = proc.stdout.decode("utf-8", "replace").strip()
        try:
            data = json.loads(out)
        except ValueError:
            log.info("bili 输出不是 JSON（%s）：%s", " ".join(args[:3]), (out or proc.stderr)[:120])
            return None
        if not data.get("ok"):
            log.info("bili 说不行（%s）：%s", " ".join(args[:3]),
                     str((data.get("error") or {}).get("message"))[:100])
            return None
        return data.get("data") or {}

    def _pick_videos(self, name: str, kind: str) -> list[dict]:
        if not self.bili_enabled or self.bili_videos <= 0:
            return []
        # 一个关键词容易搜到 EP/二创/整活，换两三个词再把明显不相关的剔掉
        keywords = (["%s 新手 攻略" % name, "%s 玩法 机制" % name] if kind != "anime"
                    else ["%s 剧情 解说" % name])
        wanted = ("攻略", "新手", "萌新", "入坑", "教程", "指南", "基础", "玩法", "机制",
                  "术语", "黑话", "讲解", "介绍", "开荒", "教学", "怎么玩")
        # 这些标题里的内容对「陪玩小抄」没营养，宁可不看
        banned = ("英语", "单词", "音乐", "ep", "主题曲", "片头曲", "片尾曲", "翻唱", "钢琴",
                  "吉他", "mmd", "手书", "二创", "同人", "剪辑", "混剪", "直播回放", "切片",
                  "电台", "asmr", "配音演员", "声优", "cosplay", "整活", "鬼畜", "沙雕",
                  "角色演示", "pv", "预告", "版本前瞻")
        pool: list[dict] = []
        seen: set[str] = set()
        for kw in keywords:
            data = self._bili(["search", "video", kw, "--size", "15"])
            for it in (data or {}).get("items") or []:
                bvid = str(it.get("bvid") or "")
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                pool.append(it)
        picked: list[dict] = []
        for it in pool:
            title = str(it.get("title") or "")
            desc = str(it.get("desc") or "")
            hay = (title + " " + desc).lower()
            if name not in title:
                continue
            if any(b in hay for b in banned):
                continue
            if not any(w in hay for w in wanted):
                continue
            secs = 0
            try:
                parts = str(it.get("duration") or "0:0").split(":")
                secs = int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else 0
            except ValueError:
                secs = 0
            if secs and not (90 <= secs <= 3000):
                continue
            picked.append(it)
        # 先看播放量（热门一般更靠谱），同量级再看标题里有没有「新手/攻略」
        picked.sort(key=lambda it: (-int(it.get("play") or 0),
                                    0 if any(w in str(it.get("title") or "") for w in wanted) else 1))
        return picked[:self.bili_videos]

    def bili_sources(self, name: str, kind: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for it in self._pick_videos(name, kind):
            bvid = str(it.get("bvid") or "")
            if not bvid:
                continue
            data = self._bili(["video", "watch", bvid,
                               "--max-chars", str(self.bili_chars),
                               "--comments", "0", "--peaks", "0",
                               "--frames", "0", "--no-danmaku"])
            if not data:
                continue
            title = str(data.get("title") or it.get("title") or bvid)
            bits = ["视频标题：%s" % title]
            owner = (data.get("owner") or {}).get("name")
            if owner:
                bits.append("UP：%s" % owner)
            desc = str(data.get("desc") or "").strip()
            if desc and desc != "-":
                bits.append("简介：%s" % desc[:400])
            summary = data.get("ai_summary")
            if isinstance(summary, dict):
                summary = summary.get("summary") or summary.get("text") or ""
            if summary:
                bits.append("B站AI摘要：%s" % str(summary)[:1200])
            chapters = data.get("chapters") or []
            if chapters:
                names = []
                for ch in chapters[:20]:
                    if isinstance(ch, dict):
                        t = ch.get("title") or ch.get("name")
                    else:
                        t = str(ch)
                    if t:
                        names.append(str(t))
                if names:
                    bits.append("章节：" + " / ".join(names))
            transcript = data.get("transcript") or {}
            text = str((transcript or {}).get("text") or "").strip()
            if text:
                bits.append("字幕/转写摘录：%s" % text[:self.bili_chars])
            out.append(("B站·" + title[:40], "\n".join(bits)))
        return out

    def collect(self, name: str, kind: str = "game") -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        sources.extend(self.moegirl_page(name))
        if kind != "anime":
            sources.extend(self.bili_sources(name, kind))
        return sources


# --------------------------------------------------------------------- 总结
GAME_PROMPT = """你是桌面宠物「泡泡」的资料整理员。下面是关于游戏《{name}》的原料（百科摘录 + 攻略视频摘要/字幕）。

请整理成一份**她自己看的陪玩小抄**，只输出 markdown 正文，不要解释。

格式（一级、二级标题必须一字不差，没有内容的二级标题就整节省略）：

# {name}

## 一句话
（这是什么类型的游戏、大概在干什么，一句话）

## 怎么玩
（核心循环，3~5 条短句：日常做什么、怎么变强、抽卡/养成怎么转）

## 操作与战斗
（按键、闪避/格挡/破韧/连招/连携这类机制，只写原料里有的）

## 界面长什么样
（主界面有哪些入口、抽卡/编队/背包/结算界面长什么样；看到什么画面能认出这是什么界面）

## 术语黑话
（玩家和弹幕会说的词，每条「词：意思」，最多 8 条）

## 角色
（先列原料里出现的角色，最多 8 个；如果不足 6 个，可以补你**确定**会在这款游戏的画面里出现的角色，
每人一句「名字：他是谁/干什么的」。**只写身份、阵营、剧情定位、怎么获得，不要写发色/服装/长相/地标**——
外观识别由 titles.json 的 visual 单独负责。不确定的一律不写，别编人物关系。）

## 版本沿革
（原料里出现的版本号 + 那个版本新增/改动的内容，一条一行，不带日期。原料没写就省略）

## 时效
（只写**游戏内**的版本/卡池/活动/更新，而且原料里要明确带着日期，格式：内容（2026-03-12）。
原料里没写日期就整节省略——不要用今天的日期充数，也不要写「原料没写日期」这种话。
不要写公司新闻、法律纠纷、版权/侵权、联动公告这类。）

要求：
- 通篇用大白话，像玩过的人随手记的，不要百科腔；不要「以上」「总之」「值得注意的是」。
- 总长 600~1100 字，宁缺勿滥。
- 原料里没有的、你不确定的，一律不要写；宁可少一条（只有「角色」这一节允许补你确定的角色）。
- **外观（发色/服装/长相/地标）不要写进手册**：那属于 titles.json 的 `visual`，手册只管玩法、剧情、术语。
- 不写公司/厂商（米哈游、腾讯这类）、平台、发售时间、评分、销量、媒体评价、玩家评价。
- 不要写「你可以告诉玩家」这种元话语，也不要写「原料没提」。
"""

ANIME_PROMPT = """你是桌面宠物「泡泡」的资料整理员。下面是关于《{name}》的原料（百科摘录）。

请整理成一份**她自己看的看剧小抄**，只输出 markdown 正文，不要解释。

格式（标题必须一字不差，没有内容的二级标题就整节省略）：

# {name}

## 一句话
（这是什么作品、大概讲什么，一句话）

## 讲的是什么
（剧情梗概，3~5 句，不剧透结局）

## 主要人物
（先列原料里的主要人物，最多 8 个；不足 6 个可以补你确定的主要人物，
每人一句「名字：他是谁/在这部里干什么」。**只写身份和剧情定位，不要写发色/服装/长相**——
外观识别由 titles.json 的 visual 单独负责。不确定的不写。）

## 角色简称
（从上面的主要人物里挑**全名超过 4 个字或念起来拗口**的，每条「全名：常用简称」，
最多 8 条。简称只取原料/粉丝常用叫法，或取名字里最自然的 2～3 个字；
不确定就不写，不要自己发明。没有长名就整节省略。）

## 梗和术语
（观众/弹幕常说的词，每条「词：意思」，最多 6 条）

## 时效
（只写原料里**明确带着日期**的季数/新作/上映信息，格式：内容（2026-03-12）。
原料里没写日期就整节省略——不要用今天的日期充数。）

要求：
- 大白话，不要百科腔，不要「以上」「总之」。
- 总长 400~800 字。
- 原料里没有的别写，别编人物关系（只有「主要人物」这一节允许补你确定的人）。
- **外观（发色/服装/长相/场景地标）不要写进手册**：那属于 titles.json 的 `visual`，手册只管剧情、人物身份、梗。
- `角色简称`只写确实能用的短叫法：全名超过 4 字才写；只取原料/粉丝常用叫法或名字里最自然的 2～3 个字，别自己发明，不确定就不写。
- 没有内容的二级标题整节省略；不要写「原料没提/没给/没细说」这类说明。
- 不写公司/厂商、平台、评分、销量。
"""


# --------------------------------------------------------------------- 主类
class Lore:
    def __init__(self, cfg: dict, gateway=None, titles_path: str = TITLES_PATH):
        conf = cfg.get("lore") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.base = os.path.join(APP_DIR, str(conf.get("dir") or "lore"))
        self.max_chunks = max(1, int(conf.get("max_chunks", 2)))
        self.budget = max(160, int(conf.get("budget_chars", 320)))
        self.stale_days = float(conf.get("stale_days", 60))
        self.refresh_days = float(conf.get("refresh_days", 30))
        self.auto_learn = bool(conf.get("auto_learn", True))
        self.progress_max_age = float((conf.get("progress") or {}).get("max_age_days", 7))
        # 总结用非推理模型：v4-flash 的思考 token 会把 1600 的输出预算吃光，正文只剩半截
        self.model = str(conf.get("model") or "deepseek-chat")
        self.summarize_tokens = max(800, int(conf.get("summarize_max_tokens", 2400)))
        self.titles_path = titles_path
        self.store = Store(self.base, stale_days=self.stale_days,
                           chunk_chars=int(conf.get("chunk_chars", 380)))
        self.collector = Collector(cfg)
        self.keeper = None
        self._titles: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._titles_mtime = 0.0
        try:
            self.store.reload()
        except Exception as exc:  # noqa: BLE001
            log.warning("资料库初次加载失败（不影响别的）：%s", exc)

    # ---- titles.json（自己读一份，免得和 gateway 互相 import）
    def _load_titles(self) -> None:
        try:
            mtime = os.path.getmtime(self.titles_path)
        except OSError:
            return
        if mtime == self._titles_mtime:
            return
        try:
            with open(self.titles_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return
        notes: dict[str, str] = {}
        kinds: dict[str, str] = {}
        for k, v in (data or {}).items():
            key = str(k).strip().lower()
            if not key:
                continue
            if isinstance(v, dict):
                note = str(v.get("note") or "").strip()
                kind = str(v.get("kind") or "").strip().lower()
            else:
                note, kind = str(v).strip(), ""
            if note:
                notes[key] = note
                kinds[key] = kind
        self._titles, self._kinds, self._titles_mtime = notes, kinds, mtime

    def name_for(self, app: str = "", title: str = "") -> tuple[str, str]:
        """这个前台窗口在 titles.json 里叫什么、什么类型（优先中文键）。"""
        self._load_titles()
        hay = ("%s %s" % (app or "", title or "")).lower()
        hits = [k for k in self._titles if k and k in hay]
        if not hits:
            return "", ""
        hits.sort(key=lambda k: (0 if _has_cjk(k) else 1, -len(k)))
        key = hits[0]
        name = key
        if not _has_cjk(name):
            note = self._titles.get(key)
            for k, v in self._titles.items():
                if v == note and _has_cjk(k):
                    name = k
                    break
        return name.strip(), (self._kinds.get(key) or "")

    # ---- 查询 / 注入
    def has(self, name: str) -> bool:
        return bool(self.store.resolve(name=name))

    def retrieve(self, query: str, game: str = "", k: int | None = None) -> list[dict]:
        slugs = None
        if game:
            slug = self.store.resolve(name=game)
            if slug:
                slugs = {slug}
        return self.store.search(query, slugs=slugs, k=k or self.max_chunks)

    def block(self, *, app: str = "", title: str = "", query: str = "",
              allow_queue: bool = False) -> str:
        """识屏/语音用：取一两段最相关的资料（带「用不上别提」的闸门）。"""
        if not self.enabled:
            return ""
        try:
            query = clean_query(query)
            name, kind = self.name_for(app, title)
            if not name and query:
                name, kind = self.name_for("", query)
            slug = self.store.resolve(app=app, title=title, name=name)
            if not slug and query:
                # 手动学过的作品不一定在 titles.json 里：拿整句去对一遍别名
                slug = self.store.resolve(title=query)
            if not slug:
                if name and kind in ("game", "anime"):
                    if allow_queue:
                        self.ensure(name, kind)
                    return ("【这款你还不太熟】你脑子里还没有《%s》的资料。别装懂、别编玩法；"
                            "就说画面里看得到的，或者老实说「这个我还没记住」。" % name)
                return ""
            # 只有标题、没有实质问话时不要主动递资料：否则每段都能挂上标题，
            # 她会每轮随机抖几条名词出来——正是要避免的「报菜名」。
            words = [t for t in tokenize(query) if t not in STOP_BIGRAMS]
            if len(words) < 2:
                return ""
            q = " ".join(x for x in (query, title, name) if x)
            hits = self.store.search(q, slugs={slug}, k=self.max_chunks)
            if not hits:
                return ""
            real = self.store.entry(slug).get("name") or name or slug
            lines = ["【你肚子里的资料·%s】（你自己攒的笔记，不是让你背的稿子）" % real]
            used = 0
            for ch in hits:
                raw = re.sub(r"^\s*[-*·•]\s*", "", ch.get("text") or "")
                text = re.sub(r"\s*\n\s*[-*·•]\s*", "；", raw)
                text = re.sub(r"\s+", " ", text)
                text = re.sub(r"[。；;]\s*；", "；", text)
                text = re.sub(r"[。；;]{2,}", "；", text).strip("； ")
                if not text:
                    continue
                line = "- （%s）%s" % (ch.get("heading") or "资料", text)
                if used + len(line) > self.budget:
                    break
                lines.append(line)
                used += len(line)
            if len(lines) < 2:
                return ""
            progress = self.store.read_progress(slug, self.progress_max_age)
            if progress:
                lines.append("【你记得老大的进度】（可能过时，别当准，也别主动报）")
                lines.append(re.sub(r"\s*\n\s*", "；", progress)[:160])
            lines.append("【怎么用】只挑跟眼下画面/问题有关的一两条，用你自己的话说；"
                         "资料里没有的别编，不确定就说「记不太清」。**用不上就一个字都别提，别背、别报菜名。**")
            log.info("资料：%s 检索到 %d 段（%s）", real, len(hits),
                     "、".join((h.get("heading") or "") for h in hits[:3]))
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("资料检索出错（不影响识屏）：%s", exc)
            return ""

    def headline(self, *, app: str = "", title: str = "") -> str:
        """只给「一句话」那一段：游戏模式里没问话时用，免得她一句话都接不上。"""
        if not self.enabled:
            return ""
        try:
            name, _kind = self.name_for(app, title)
            slug = self.store.resolve(app=app, title=title, name=name)
            if not slug:
                return ""
            self.store.ensure_fresh()
            for ch in self.store.chunks:
                if ch.get("slug") == slug and (ch.get("heading") or "") in ("一句话", "讲的是什么"):
                    text = re.sub(r"\s+", " ", ch.get("text") or "").strip()
                    if not text:
                        continue
                    real = self.store.entry(slug).get("name") or name
                    return ("【你肚子里的资料·%s】%s\n（这是你自己记的，顺着画面聊就行；"
                            "别背、别报菜名，画面里没有的别硬提。）" % (real, text))
            return ""
        except Exception as exc:  # noqa: BLE001
            log.debug("资料一句话取不出来：%s", exc)
            return ""

    # ---- 学习
    def need_learn(self, name: str, kind: str = "") -> bool:
        slug = self.store.resolve(name=name)
        if not slug:
            return True
        meta = self.store.entry(slug)
        updated = float(meta.get("updated") or 0)
        if not updated:
            return True
        return (_now() - updated) > self.refresh_days * 86400

    def ensure(self, name: str, kind: str = "", aliases: list[str] | None = None) -> None:
        if not self.enabled or not self.auto_learn or not name.strip():
            return
        if self.keeper is not None:
            self.keeper.enqueue(name.strip(), kind or "game", aliases or [])
            return
        # 没有 keeper（比如 CLI 里直接 new 的）：起一个一次性线程
        threading.Thread(target=self.learn_now, args=(name.strip(), kind or "game", aliases or []),
                         name="lore-learn-once", daemon=True).start()

    def learn_now(self, name: str, kind: str = "game",
                  aliases: list[str] | None = None) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "资料库没开（config.lore.enabled）"}
        if self.has(name) and not self.need_learn(name, kind):
            return {"ok": True, "name": name, "slug": self.store.resolve(name=name),
                    "skipped": "已经学过，还没到更新的时候"}
        if self.gateway is None:
            return {"ok": False, "error": "没有网关，没法调模型总结"}
        t0 = _now()
        try:
            sources = self.collector.collect(name, kind)
        except Exception as exc:  # noqa: BLE001
            log.warning("查资料出错（%s）：%s", name, exc)
            sources = []
        if not sources:
            return {"ok": False, "error": "没抓到资料（百科和B站都没给东西）", "name": name}
        for label, text in sources:
            self.store.save_raw(_slug(name), label, text)
        markdown = self.summarize(name, kind, sources)
        if not markdown:
            return {"ok": False, "error": "总结失败", "name": name}
        markdown = sanitize_dates(markdown, sources)
        labels = [lb for lb, _ in sources]
        path = self.store.save(name, kind, markdown, labels, aliases=aliases)
        took = round(_now() - t0, 1)
        log.info("资料：给《%s》做了本手册（%d 字，%d 个来源，%.1f 秒）：%s",
                 name, len(markdown), len(sources), took, path)
        return {"ok": True, "name": name, "slug": _slug(name), "path": path,
                "chars": len(markdown), "sources": labels, "seconds": took}

    def summarize(self, name: str, kind: str, sources: list[tuple[str, str]]) -> str:
        material = []
        total = 0
        for label, text in sources:
            piece = "## 来源：%s\n%s" % (label, text.strip())
            if total + len(piece) > 16000:
                piece = piece[:max(0, 16000 - total)]
            if not piece.strip():
                break
            material.append(piece)
            total += len(piece)
        prompt = (ANIME_PROMPT if kind == "anime" else GAME_PROMPT).format(name=name)
        payload = {
            "model": self.model,
            "max_tokens": self.summarize_tokens,
            "temperature": 0.3,
            "stream": False,
            "messages": [
                {"role": "system", "content": "你在给桌面宠物整理资料笔记，只输出 markdown。"},
                {"role": "user", "content": prompt + "\n\n以下是原料：\n\n" + "\n\n".join(material)},
            ],
        }
        try:
            data = self.gateway.post_once(payload)
            choice = data["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            if not text:
                log.info("总结《%s》返回空（finish=%s，usage=%s）", name,
                         choice.get("finish_reason"), json.dumps(data.get("usage"), ensure_ascii=False))
            elif choice.get("finish_reason") == "length":
                log.info("总结《%s》被截断（max_tokens=%d，%d 字）", name,
                         self.summarize_tokens, len(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("总结《%s》失败：%s", name, exc)
            return ""
        text = _strip_fence(text)
        if "##" not in text:
            log.info("总结《%s》没按格式来，丢掉：%s", name, text[:80])
            return ""
        return text[:4000]

    def forget(self, name: str) -> dict:
        """删掉一本手册（连带进度和原料）。名字对不上就返回错误。"""
        slug = self.store.resolve(name=name)
        if not slug:
            return {"ok": False, "error": "没有这本：%s" % name}
        removed = []
        for path in (os.path.join(self.store.games_dir, slug + ".md"),
                     os.path.join(self.store.anime_dir, slug + ".md"),
                     os.path.join(self.store.progress_dir, slug + ".md")):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
        raw = os.path.join(self.store.raw_dir, slug)
        if os.path.isdir(raw):
            shutil.rmtree(raw, ignore_errors=True)
            removed.append(raw)
        self.store._checked_at = 0.0
        self.store.reload()
        log.info("资料：删掉《%s》的手册（%d 项）", name, len(removed))
        return {"ok": True, "name": name, "removed": removed}

    def status(self) -> dict:
        self.store.ensure_fresh()
        items = []
        for slug, meta in self.store.entries.items():
            items.append({
                "slug": slug, "name": meta.get("name"), "kind": meta.get("kind"),
                "updated": int(meta.get("updated") or 0),
                "age_hours": round((_now() - float(meta.get("updated") or _now())) / 3600, 1),
                "sources": meta.get("sources") or [],
            })
        items.sort(key=lambda x: -x["updated"])
        return {"ok": True, "enabled": self.enabled, "dir": self.base,
                "count": len(items), "items": items,
                "keeper": self.keeper.status() if self.keeper is not None else None}


# --------------------------------------------------------------------- 后台学习
class LoreKeeper(threading.Thread):
    """盯着前台：认得出是游戏/番剧、但手册没有或太旧，就排队去查（一次一个，别刷接口）。"""

    def __init__(self, cfg: dict, gateway, lore: Lore):
        super().__init__(name="lore-keeper", daemon=True)
        conf = cfg.get("lore") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.lore = lore
        self.enabled = bool(conf.get("enabled", True)) and bool(conf.get("auto_learn", True))
        self.poll = max(15.0, float(conf.get("poll_seconds", 45)))
        self.cooldown = float(conf.get("cooldown_minutes", 720)) * 60
        self.prebuild = bool(conf.get("prebuild", True))
        self.q: queue.Queue = queue.Queue()
        self._queued: set[str] = set()
        self._queued_names: set[str] = set()
        self._attempted: dict[str, float] = {}
        self._busy = ""
        self._last_done = ""
        self._worker = threading.Thread(target=self._work, name="lore-worker", daemon=True)
        self._bootstrap_done = False

    # ---- 排队
    def enqueue(self, name: str, kind: str = "game", aliases: list[str] | None = None) -> None:
        if not name.strip():
            return
        if not self.lore.need_learn(name, kind):
            return                            # 已经学过、还没到该更新的时候
        slug = _slug(name)
        if slug in self._queued:
            return
        if any(_same_name(other, name) for other in self._queued_names):
            return                            # 「星穹铁道 / 崩坏：星穹铁道」别当两门课
        if _now() - self._attempted.get(slug, 0.0) < self.cooldown:
            return
        self._queued.add(slug)
        self._queued_names.add(name)
        self.q.put({"name": name, "kind": kind or "game",
                    "aliases": [str(a) for a in (aliases or []) if str(a).strip()]})

    def status(self) -> dict:
        return {"queued": list(self._queued), "busy": self._busy,
                "last": self._last_done, "cooldown_min": round(self.cooldown / 60)}

    # ---- 线程
    def run(self) -> None:
        if not self.enabled:
            return
        self._worker.start()
        while True:
            try:
                if self.prebuild and not self._bootstrap_done:
                    self._bootstrap_done = True
                    self._prebuild()
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("资料库自动补课出错：%s", exc)
            time.sleep(self.poll)

    def _prebuild(self) -> None:
        """把 titles.json 里已知的游戏/番剧先排上（第一次启动时慢慢补）。

        注意别把这个方法叫 `_bootstrap`：那会盖掉 `threading.Thread._bootstrap`，
        `start()` 就会永远等一个不会到来的「已启动」信号。
        """
        self.lore._load_titles()
        items: list[dict] = []
        for key, note in self.lore._titles.items():
            kind = self.lore._kinds.get(key) or ""
            if kind not in ("game", "anime") or not note:
                continue
            name = key
            if not _has_cjk(name):
                # 「zenlesszonezero」这种进程序名：从小抄里《…》摘出正式名
                m = re.search(r"《(.+?)》", note)
                if m:
                    name = m.group(1).strip()
            item = next((it for it in items if _same_name(it["name"], name)), None)
            if item is None:
                items.append({"name": name, "kind": kind, "keys": [key]})
            else:
                item["keys"].append(key)
                if not item["kind"] and kind:
                    item["kind"] = kind
        for it in items:
            if self.lore.need_learn(it["name"], it["kind"]):
                self.enqueue(it["name"], it["kind"], aliases=it["keys"])

    def _tick(self) -> None:
        name, kind = self.lore.name_for(*_foreground())
        if not name or kind not in ("game", "anime"):
            return
        if self.lore.need_learn(name, kind):
            self.enqueue(name, kind)

    def _work(self) -> None:
        while True:
            job = self.q.get()
            slug = _slug(job["name"])
            self._busy = job["name"]
            try:
                result = self.lore.learn_now(job["name"], job["kind"], aliases=job["aliases"])
                if result.get("ok"):
                    self._last_done = "%s（%d 字）" % (job["name"], result.get("chars") or 0)
                    # 学到了就重置试错计时，但别马上又排
                    self._attempted[slug] = _now()
                else:
                    self._last_done = "%s 失败：%s" % (job["name"], result.get("error") or "?")
                    self._attempted[slug] = _now()
            except Exception as exc:  # noqa: BLE001
                self._last_done = "%s 出错：%s" % (job["name"], exc)
                self._attempted[slug] = _now()
            finally:
                self._queued.discard(slug)
                self._queued_names = {n for n in self._queued_names if not _same_name(n, job["name"])}
                self._busy = ""
                self.q.task_done()
                time.sleep(2.0)


def _foreground() -> tuple[str, str]:
    try:
        from video_watch import foreground

        return foreground()
    except Exception:  # noqa: BLE001
        return "", ""


# --------------------------------------------------------------------- 进度笔记
PROGRESS_PROMPT = """下面是老大在玩《{name}》时，桌宠泡泡的识屏观察记录（时间从早到晚）。请提炼「老大在这游戏里的进度和偏好」。

规则：
- 最多 3 条，每条一句话、不超过 30 字，用中文。
- 只写记录里**明确出现**的：打到第几章/哪个关卡/哪个模式、在用什么角色、想抽谁、卡在哪、反复在刷什么。
- 这些记录是泡泡自己的观察，可能有错；拿不准的、只是猜的，一律不写。
- 不要写时间、不要写情绪、不要写「他在认真玩」这种空话。
- 一条都确定不了，就只输出：（暂无）

只输出这几条，每行一条，不要解释、不要编号以外的符号。
"""


class ProgressKeeper(threading.Thread):
    """隔一阵从识屏账本里提炼「老大在游戏里的进度」，写进 lore/progress/<slug>.md。"""

    def __init__(self, cfg: dict, gateway, lore: Lore):
        super().__init__(name="lore-progress", daemon=True)
        conf = (cfg.get("lore") or {}).get("progress") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.lore = lore
        self.enabled = bool(conf.get("enabled", True)) and lore.enabled
        self.minutes = max(5.0, float(conf.get("minutes", 30)))
        self.min_rows = max(1, int(conf.get("min_rows", 2)))
        self.window = float(conf.get("window_minutes", 40))
        self.max_chars = max(60, int(conf.get("max_chars", 120)))
        self.model = str((cfg.get("lore") or {}).get("model") or "deepseek-chat")
        self._last: dict[str, float] = {}

    def run(self) -> None:
        if not self.enabled:
            return
        time.sleep(85)                      # 起步晚一点，别和预热抢
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("进度笔记出错（不影响别的）：%s", exc)
            time.sleep(60)

    def tick(self) -> None:
        if self.gateway is None:
            return
        mode = self.gateway.mode.get() if hasattr(self.gateway, "mode") else ""
        app, title = _foreground()
        name, kind = self.lore.name_for(app, title)
        slug = self.lore.store.resolve(app=app, title=title, name=name)
        if not slug or (kind != "game" and mode != "game"):
            return
        if _now() - self._last.get(slug, 0.0) < self.minutes * 60:
            return
        self._last[slug] = _now()
        rows = self._rows_for(app, title)
        if len(rows) < self.min_rows:
            return
        text = self._extract(name, rows)
        if not text:
            return
        self.lore.store.save_progress(slug, text, len(rows))
        log.info("进度笔记：%s 记下 %d 条（%d 条观察）", name, len(text), len(rows))

    def _rows_for(self, app: str, title: str) -> list[dict]:
        try:
            rows = self.gateway.ledger.rows()
        except Exception:  # noqa: BLE001
            return []
        cutoff = _now() - self.window * 60
        out = []
        key = (app or "").strip().lower()
        for row in rows:
            if float(row.get("epoch") or 0) < cutoff:
                continue
            if str(row.get("scenario") or "") != "vision":
                continue
            row_app = ""
            try:
                row_app = str(self.gateway.ledger.app_of(row) or "").strip().lower()
            except Exception:  # noqa: BLE001
                row_app = ""
            if key and row_app and row_app != key:
                continue
            said = str(row.get("assistant") or "").strip()
            if said:
                out.append({"ts": row.get("ts", ""), "said": said[:150]})
        return out[-12:]

    def _extract(self, name: str, rows: list[dict]) -> list[str]:
        body = "\n".join("- %s" % r["said"] for r in rows)
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "temperature": 0.2,
            "stream": False,
            "messages": [
                {"role": "system", "content": "你只做信息提炼，只输出短句，不解释。"},
                {"role": "user", "content": PROGRESS_PROMPT.format(name=name) + "\n\n记录：\n" + body},
            ],
        }
        try:
            data = self.gateway.post_once(payload)
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.debug("进度提炼调用失败：%s", exc)
            return []
        text = _strip_fence(text)
        lines = []
        for line in text.splitlines():
            line = line.strip().lstrip("-·•*0123456789.、）) ")
            if not line or line in ("（暂无）", "(暂无)", "暂无"):
                continue
            if len(line) > 40:
                continue
            lines.append(line)
        return lines[:3]


# --------------------------------------------------------------------- 命令行
def _cli_http(method: str, path: str, body: dict | None = None, port: int = 9888,
              timeout: float = 300.0):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="泡泡的资料库（作品手册）")
    ap.add_argument("--port", type=int, default=9888)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出已有手册")
    p = sub.add_parser("show", help="看一本手册")
    p.add_argument("name")
    p = sub.add_parser("learn", help="现在就去查+总结一本（走网关）")
    p.add_argument("name")
    p.add_argument("--kind", default="game", choices=["game", "anime", "video"])
    p = sub.add_parser("search", help="按一句话检索")
    p.add_argument("query")
    p.add_argument("--game", default="")
    p = sub.add_parser("eval", help="拿几个常见问题试检索覆盖（看「她能不能拿到对的段落」）")
    p.add_argument("name")
    p = sub.add_parser("forget", help="删掉一本手册")
    p.add_argument("name")
    sub.add_parser("status", help="资料库/后台队列状态")

    args = ap.parse_args(argv)
    if args.cmd == "list":
        lore = Lore({"lore": {"enabled": True}})
        st = lore.status()
        print("%d 本手册 · %s" % (st["count"], st["dir"]))
        for it in st["items"]:
            print("  %-10s %-6s %s 小时前  %s" % (
                it["name"], it["kind"] or "?", it["age_hours"],
                "、".join(it["sources"][:2])))
        return 0
    if args.cmd == "show":
        lore = Lore({"lore": {"enabled": True}})
        slug = lore.store.resolve(name=args.name)
        if not slug:
            print("没有这本：%s" % args.name)
            return 1
        meta = lore.store.entry(slug)
        kind = (meta.get("kind") or "").lower()
        path = os.path.join(lore.base, "anime" if kind == "anime" else "games", slug + ".md")
        with open(path, "r", encoding="utf-8-sig") as f:
            print(f.read())
        prog = lore.store.read_progress(slug, 3650)
        if prog:
            print("\n--- 进度 ---\n" + prog)
        return 0
    if args.cmd == "search":
        lore = Lore({"lore": {"enabled": True}})
        for ch in lore.retrieve(args.query, game=args.game, k=5):
            print("[%s/%s] %.2f\n%s\n" % (ch["name"], ch["heading"], ch["score"], ch["text"]))
        return 0
    if args.cmd == "eval":
        lore = Lore({"lore": {"enabled": True}})
        slug = lore.store.resolve(name=args.name)
        if not slug:
            print("没有这本：%s" % args.name)
            return 1
        kind = (lore.store.entry(slug).get("kind") or "game")
        if kind == "anime":
            qs = ["这是什么番", "讲的是什么", "这个角色是谁", "这个弹幕刷的什么梗"]
        else:
            qs = ["这是什么游戏", "怎么玩", "这个界面是干嘛的", "这个角色是谁",
                  "这个机制怎么操作", "最近有什么版本"]
        hit = 0
        for q in qs:
            hits = lore.retrieve(q, game=args.name, k=3)
            heads = [h["heading"] for h in hits]
            print("%-16s -> %s" % (q, "、".join(heads) if heads else "MISS"))
            hit += 1 if hits else 0
        print("覆盖 %d/%d（不是答对率，只看能不能检索到相关段落）" % (hit, len(qs)))
        return 0
    if args.cmd == "learn":
        try:
            res = _cli_http("POST", "/lore", {"action": "learn", "name": args.name,
                                             "kind": args.kind}, port=args.port)
        except Exception as exc:  # noqa: BLE001
            print("网关没在跑？%s" % exc)
            return 2
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "forget":
        res = _cli_http("POST", "/lore", {"action": "forget", "name": args.name},
                        port=args.port)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "status":
        res = _cli_http("GET", "/lore", port=args.port)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
