#!/usr/bin/env python3
"""
生成「中文用户向」更新日志，供 manifest / GitHub Release / FnDepot 索引共用。

## 三个来源（不再局限于跟随上游）

  1. **上游代码改动** —— 只算「对原生 FPK 有影响」的那部分。
     上游是 Docker 项目，`docker-compose.yml` / `.env.example` / `tests/` /
     `.github/` / `*.md` 的改动装进我们这套原生包后**一行都不会生效**。
     实测 1.1.8→1.1.9 整个区间只有 4 个文件改动，全部属于这一类，
     旧版脚本仍渲染成「跟随上游更新（9a4d72b）」，等于向用户报告了一件
     根本没发生的事。
  2. **本仓库打包改动** —— 我们自己的 patch、依赖、瘦身、生命周期脚本变更。
     这些恰恰是用户装新包后能感知的差异，旧版脚本完全没纳入。
  3. 上游 CHANGELOG.md 的日期小节 —— 仅作「确有功能更新」的旁证，不取文案
     （原文是英文技术日志，与 manifest 要求的「简洁、面向用户」不符）。

## 为什么不用机器翻译

  实测把 "treat a refused browser send as a failure" 机翻成「将浏览器发送视为失败」
  这类不可读的中文，因此维护一张只覆盖本项目高频关键词的词表 VOCAB；
  命中不了的整条**保留英文原文**（宁可英文也不要错译的假中文）。
  中文提交（我们自己的改动）直接原样通过。

## 两种长度

  * `--out`      → 完整版，给 Release 正文 / FnDepot 索引看，上限 FULL_LEN
  * `--manifest` → 压缩版，manifest 的 changelog 是单行 INI 值，上限 MAX_LEN

用法:
    python3 gen-changelog.py --repo halfwaystudent/douyin-sparkflow \
        --prev-sha <40hex> --new-sha <40hex> \
        --manifest manifest --out changelog.txt

    # --own-since 不传时自动推断为「上一次自动发布的提交」，
    # 即在那个提交与 HEAD 之间查找我们自己的打包改动。
环境变量:
    GITHUB_TOKEN   可选，提高 API 限额
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── conventional commit 前缀 → 中文标签 ──────────────────────────────
# 只收用户能感知的类型；docs/test/ci/chore/refactor 对最终用户无意义。
TYPE_LABEL = {
    "fix": "修复",
    "feat": "新增",
    "perf": "优化",
}
# 明确丢弃的类型（不是丢整条提交，是只丢它的文案理由）
SKIP_TYPES = {"docs", "test", "ci", "chore", "refactor", "style", "build", "merge"}

# 每个前缀最多取几条，防止一次上游大版本把 changelog 撑爆
MAX_PER_TYPE = 4
MAX_TOTAL = 8

# manifest 的 changelog 是单行 INI 值，长度上限按应用中心能正常展示来定
MAX_LEN = 420
# Release 正文 / FnDepot 索引可以更长
FULL_LEN = 1200
# 打包侧最多列几条：列全了会变成开发流水账，用户只需要知道最要紧的几处
MAX_OWN = 4

# ── 对原生 FPK 无影响的路径 ──────────────────────────────────────────
# 上游用 Docker 交付，这些文件在我们的原生包里根本不参与运行。
# 命中全部文件才判定为「无关」：只要有一个文件落在别的路径上就算有关。
IRRELEVANT_PATH_RE = re.compile(
    r"(?ix)^(?:"
    # 容器 / 环境变量：原生包完全不读
    r"docker-compose[^/]*\.ya?ml"
    r"|dockerfile[^/]*"
    r"|\.dockerignore"
    r"|\.env(?:\.[^/]+)?"
    # CI 与测试：进不了包
    r"|\.github/.*"
    r"|(?:[^/]+/)*(?:tests?|__tests__)/.*"
    r"|(?:[^/]+/)*conftest\.py"
    r"|(?:[^/]+/)*test_[^/]*\.py"
    r"|(?:[^/]+/)*[^/]*_test\.py"
    # 文档与仓库杂项
    r"|(?:[^/]+/)*docs?/.*"
    r"|[^/]*\.md"
    r"|\.gitignore"
    r"|license[^/]*"
    r")$"
)


def api(url: str, attempts: int = 6) -> object:
    """
    GitHub API GET。

    必须处理 IncompleteRead：上游 compare 30 个提交时响应超过 600 KB，
    分块传输会在流末被掐断（实测 "626114 bytes read, 16674 more expected"），
    这是传输层失败而非内容差异，退避后重试即可 —— 直接放弃会让整个构建中断。

    但 4xx 是**确定性**失败，重试只会把一条明确的错误拖成 60 秒的静默等待
    （实测 SHA 写错时 6 次退避后才报 404）。401/403/404/422 立即抛出。
    """
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "fnos-fpk-changelog",
                    **(gh_auth(url)),
                },
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                # 读到 EOF 为止；不依赖单次 read() 返回完整 body
                chunks = []
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 422):
                hint = {
                    401: "凭据无效（token 过期或被撤销）",
                    403: "匿名配额耗尽或被拒",
                    404: "SHA 不存在，或不在该仓库",
                }.get(exc.code, "请求被拒")
                raise RuntimeError(f"GitHub API {exc.code}: {hint}: {url}") from exc
            last = exc
            wait = min(2 ** attempt, 30)
            print(f"  [重试 {attempt + 1}/{attempts}] HTTP {exc.code}，{wait}s 后再试",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as exc:  # noqa: BLE001 - 传输层失败统一重试
            last = exc
            wait = min(2 ** attempt, 30)
            print(f"  [重试 {attempt + 1}/{attempts}] {type(exc).__name__}，{wait}s 后再试",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GitHub API 重试 {attempts} 次仍失败: {url}: {last}")


def gh_auth(url: str) -> dict:
    """
    只对 api.github.com 带 token。

    实测：release 资产 URL 会 302 到带签名的 release-assets CDN，
    把 API 凭据一起带过去会被判 401（两套凭据冲突）。
    """
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        return {}
    host = urllib.parse.urlsplit(url).hostname or ""
    return {"Authorization": f"Bearer {tok}"} if host == "api.github.com" else {}


def split_conventional(message: str):
    """返回 (type, subject)；非 conventional 返回 (None, 首行)。"""
    first = message.strip().splitlines()[0] if message.strip() else ""
    m = re.match(r"^(\w+)(\([^)]*\))?!?:\s*(.+)$", first)
    if not m:
        # 无前缀但首字母大写（Fix xxx / Added xxx …）也算用户向
        return None, first
    return m.group(1).lower(), m.group(3).strip()


def clean_subject(subject: str) -> str:
    """去掉尾部 PR 号、句号、多余空白。"""
    s = re.sub(r"\s*\(#\d+\)\s*$", "", subject)
    s = re.sub(r"\s*\[(?:skip ci)\]\s*$", "", s, flags=re.I)
    return s.strip().rstrip(".").strip()


# ── 领域词表：英文 → 中文用户向措辞 ──────────────────────────────────
# 机器翻译会把 "treat a refused browser send as a failure" 译成
# 「将浏览器发送视为失败」这类不可读的中文，因此维护一张**只覆盖本项目高频关键词**
# 的词表；命中不了的整条退回英文原文 —— 宁可输出英文，也不输出错译的假中文。
VOCAB = {
    # 账号 / 登录
    "logins": "登录", "login": "登录", "relogin": "重新登录", "relogins": "重新登录",
    "account": "账号", "accounts": "账号", "sub-account": "子账号",
    "identity": "身份", "credential": "凭据", "session": "会话",
    "qr": "二维码", "qr code": "二维码",
    # 发送火花
    "send": "发送", "sends": "发送", "sent": "发送", "sending": "发送",
    "spark": "火花", "sparkflow": "火花", "streak": "火花",
    "browser": "浏览器", "protocol": "协议", "web": "网页", "console": "控制台",
    "page": "页面", "button": "按钮", "dialog": "弹窗", "notice": "提示",
    "setting": "设置", "settings": "设置", "panel": "面板", "dashboard": "面板",
    "friend": "好友", "friends": "好友", "friend list": "好友列表",
    "scan": "扫描", "scanned": "扫描", "verifier": "验证码", "verify": "验证",
    # 结果 / 状态
    "failure": "失败", "failed": "失败", "fail": "失败", "error": "错误",
    "success": "成功", "refused": "被拒", "rejected": "被拒",
    "timeout": "超时", "time out": "超时", "times out": "超时",
    "expired": "过期", "expiry": "过期", "stale": "失效", "dead": "失效",
    "retry": "重试", "resend": "重新发送", "resend-all": "全部重发",
    "cooldown": "冷却", "cooldowns": "冷却", "pause": "暂停", "paused": "已暂停",
    "cancel": "取消", "refresh": "刷新", "reload": "重新加载", "navigate": "跳转",
    "progress": "进度", "result": "结果", "status": "状态", "state": "状态",
    "scheduling": "定时调度", "schedule": "定时调度", "scheduler": "定时调度",
    "notification": "通知", "notifications": "通知", "webhook": "机器人推送",
    "telegram": "Telegram", "dingtalk": "钉钉", "wecom": "企业微信",
    "marker": "标记", "lock": "锁", "pid": "进程",
    "profile": "浏览器配置", "proxy": "代理", "cache": "缓存",
    "performance": "性能", "faster": "更快", "speed": "速度",
    "mobile": "手机", "phone": "手机", "desktop": "桌面", "popup": "弹窗",
    "detail": "明细", "summary": "汇总", "daily summary": "每日汇总",
    "target": "目标", "targets": "目标", "unconfirmed": "未确认", "confirmed": "已确认",
    "network": "网络", "localhost": "本机", "direct": "直连", "proxies": "代理",
    "keyboard": "键盘", "focus ring": "焦点框", "tab bar": "底部标签栏",
    "start": "启动", "startup": "启动", "idle": "空闲", "shutdown": "关闭",
    "responsive": "响应式", "config": "配置", "environment": "环境",
    "secret": "密钥", "secrets": "密钥", "download": "下载", "upload": "上传",
}

# 结构虚词：丢弃，不参与翻译
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "by", "for", "with", "and", "or",
    "not", "as", "into", "from", "up", "out", "off", "over", "under", "its", "it",
    "every", "once", "per", "dash", "short", "long", "own", "self", "is", "are",
    "was", "were", "be", "been", "being", "only", "also", "just", "dead", "live",
}

# 人工确认的中文整句（精确匹配，键为小写英文 subject）
_PHRASE = {
    "treat a refused browser send as a failure, not a success":
        "抖音发送被拒绝时不再误记为成功",
    "treat a qr request that times out as a busy page, not a dead service":
        "取二维码超时时显示「处理中」而非误判服务异常",
    "bind relogins to verified douyin identity":
        "重新登录时校验抖音身份，避免登错账号",
    "read real friend names and only trust a complete scan":
        "好友列表读取真实昵称，且仅在扫描完整时采信结果",
    "do not read a failed page load as a logout":
        "页面加载失败不再被误判为掉线",
    "let a sub-account manage its own douyin accounts":
        "支持子账号管理自己的抖音账号",
    "save a scanned login at once and verify it in the background":
        "扫码登录后立即保存，并在后台校验登录态",
    "open the login workspace faster and show progress from the click":
        "扫码登录页打开更快，点击后立即显示进度",
    "push run failures and daily summaries to im bots":
        "新增运行失败提醒与每日汇总推送到 IM 机器人",
    "keep the qr wait alive through page navigation and never start on a stale login":
        "等待二维码期间页面跳转不再中断，也不会从未登录态开始扫描",
    "answer a login save at once and show it is working":
        "保存登录态立即有反馈并显示进行中状态",
}


# 中英同形或品牌词，翻译后保留拉丁字母是正常的
_KEEP_LATIN = {"telegram", "webhook", "douyin", "sparkflow", "xkb", "noVNC", "ID", "URL"}


def has_cjk(text: str) -> bool:
    """是否含至少 2 个汉字 —— 我们自己写的提交信息已经是面向用户的中文。"""
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff") >= 2


def localize(subject: str) -> tuple[str, bool]:
    """
    把英文 commit subject 本地化成中文用户向描述。

    返回 (文本, 是否纯中文)。未命中词表时返回 (原文, False)，由调用方决定
    是保留英文还是丢弃 —— 宁可输出英文原文，也不输出错译的假中文。

    已经含中文的 subject（本仓库自己的提交）直接原样通过：它本身就是
    面向用户写的，再过一遍词表只会被当成「未命中」丢掉。
    """
    if has_cjk(subject):
        return subject.strip(), True

    low = subject.lower().strip()

    # 1) 人工整句映射优先
    for en, zh in _PHRASE.items():
        if low == en:
            return zh, True

    # 2) 词表替换（长词优先，"relogin" 必须先于 "login"）
    keys = sorted(VOCAB, key=len, reverse=True)
    pieces, cursor = [], 0
    used = False
    for m in re.finditer(r"[A-Za-z][A-Za-z-]*", low):
        token = m.group(0)
        if token not in VOCAB:
            continue
        zh = VOCAB[token]
        if zh == token:      # 中英同形（Telegram/钉钉…）
            continue
        pieces.append(low[cursor:m.start()])
        pieces.append(zh)
        cursor = m.end()
        used = True
    if not used:
        return subject, False
    pieces.append(low[cursor:])
    text = "".join(pieces)

    # 3) 去掉残留英文虚词与标点，压缩空白
    text = re.sub(r"\b(?:%s)\b" % "|".join(sorted(_STOPWORDS, key=len, reverse=True)), " ", text)
    text = text.replace(" ,", ",").replace(" .", ".").replace("..", ".")
    text = re.sub(r"\s{2,}", " ", text).strip(" .,;:")

    if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return subject, False

    # 4) 质量门：拒绝中英混杂。
    # 实测反例（词表部分命中的脏输出）：
    #   "label 登录 workspace pill 状态, colour"      ← 残留 3 个英文实词
    #   "give 二维码 poll room finish say why stopped" ← 几乎整句未翻译
    # 只要还有未翻译的英文实词（≥1 个连续英文短语，或 ≥2 个孤立英文词）就丢弃该条，
    # 宁可 changelog 少一条，也不要让用户在应用中心看到半中半英的说明。
    leftovers = [
        w for w in re.findall(r"[A-Za-z][A-Za-z-]*", text)
        if w.lower() not in _STOPWORDS and w.lower() not in _KEEP_LATIN
    ]
    if leftovers:
        return subject, False
    return text, True


def collect_commits(repo: str, prev_sha: str, new_sha: str, fallback_n: int = 30):
    """
    取 prev..new 区间的 commit（旧→新）。

    prev_sha 为空（首次更新，此前从未 pin 过任何版本）时，退化为取上游最近
    fallback_n 个 commit —— 不能再拿本仓库的 git log 顶上，那是**另一个仓库**的
    SHA，跨仓库 compare 会直接失败。
    """
    if not prev_sha:
        print("  无上一版 pin，改用上游最近 "
              f"{fallback_n} 个提交作为区间")
        data = api(f"https://api.github.com/repos/{repo}/commits?per_page={fallback_n}")
        if not isinstance(data, list):
            raise RuntimeError(f"commits 返回异常: {str(data)[:200]}")
        # 只保留 new_sha 及其祖先；API 已按时间倒序，无需再按日期过滤
        return list(reversed(data))
    data = api(f"https://api.github.com/repos/{repo}/compare/{prev_sha}...{new_sha}")
    if not isinstance(data, dict) or "commits" not in data:
        raise RuntimeError(f"compare 返回异常（SHA 不存在，或两者不在同一仓库）: {str(data)[:200]}")
    return list(data["commits"])


def changelog_sections(repo: str, ref: str):
    """解析上游 CHANGELOG.md 的 (日期, [条目]) 小节。"""
    try:
        data = api(f"https://api.github.com/repos/{repo}/contents/CHANGELOG.md?ref={ref}")
    except Exception:
        return {}
    import base64
    text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    out = {}
    for m in re.finditer(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$\\n(.*?)(?=^##\s+|\Z)",
                         text, re.M | re.S):
        items = [ln.strip("- ").strip() for ln in m.group(2).splitlines() if ln.strip().startswith("-")]
        out[m.group(1)] = items
    return out


# ── 上游改动的相关性判定 ────────────────────────────────────────────
_FILE_CACHE: dict[str, list[str]] = {}
_MAX_FILE_LOOKUPS = 25


def commit_files(repo: str, sha: str) -> list[str]:
    """取单个 commit 改动的文件名列表；失败返回 []（保守当作相关）。"""
    if sha in _FILE_CACHE:
        return _FILE_CACHE[sha]
    try:
        data = api(f"https://api.github.com/repos/{repo}/commits/{sha}")
        raw = data.get("files") if isinstance(data, dict) else None
        files = [f["filename"] for f in (raw or [])]
        # 超大提交（依赖锁、二进制）API 会省略 files，此时保守当作相关
        if not files:
            files = ["<unknown>"]
    except Exception as exc:  # noqa: BLE001
        print(f"  取 {sha[:7]} 改动文件失败（{type(exc).__name__}），保守当作相关",
              file=sys.stderr)
        files = ["<unknown>"]
    _FILE_CACHE[sha] = files
    return files


def files_affect_native(files: list[str]) -> bool:
    """
    这批文件里是否有**原生 FPK 会用到**的。

    `<unknown>` 是 API 未返回 files 的情况，保守判定为相关，宁可多报也不漏报。
    """
    for f in files:
        if f == "<unknown>":
            return True
        if not IRRELEVANT_PATH_RE.match(f):
            return True
    return False


def areas_of(files: list[str], limit: int = 3) -> str:
    """把改动文件归纳成「涉及模块」，用于无法翻译时的兜底描述。"""
    seen: list[str] = []
    for f in files:
        if f == "<unknown>":
            continue
        parts = f.split("/")
        key = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        if key not in seen:
            seen.append(key)
    seen = seen[:limit]
    return "、".join(seen)


def build_from_commits(repo: str, commits):
    """
    commit 列表 → (picked, stats)

    stats 里记下被跳过的原因分布，供兜底文案使用：
      irrelevant  只动了 Docker/测试/文档，原生包无影响
      skipped     类型无意义（chore/docs/ci…）
      dropped     有相关改动但无法翻译成干净中文
    """
    buckets = {label: [] for label in set(TYPE_LABEL.values())}
    stats = {"irrelevant": 0, "skipped": 0, "dropped": 0,
             "irrelevant_files": [], "relevant_files": [],
             "dropped_subjects": [], "relevant_total": 0}
    lookups = 0

    for c in reversed(commits):  # 旧→新
        message = c["commit"]["message"]
        ctype, subject = split_conventional(message)
        if not subject:
            continue
        if ctype in SKIP_TYPES:
            stats["skipped"] += 1
            continue
        subject = clean_subject(subject)
        if not subject:
            continue

        # 相关性：先花一次 API 调用看它到底动了什么
        if lookups < _MAX_FILE_LOOKUPS:
            files = commit_files(repo, c["sha"])
            lookups += 1
        else:
            files = ["<unknown>"]
        if not files_affect_native(files):
            stats["irrelevant"] += 1
            stats["irrelevant_files"].extend(files)
            continue
        stats["relevant_total"] += 1
        stats["relevant_files"].extend(files)

        label = TYPE_LABEL.get(ctype or "")
        if label is None:
            if not subject[0].isupper():
                stats["skipped"] += 1
                continue
            label = "修复"
        zh, ok = localize(subject)
        if not ok:
            # 不再是静默丢弃：记下原文，兜底时至少告诉用户改了哪里
            stats["dropped"] += 1
            stats["dropped_subjects"].append((label, zh))
            continue
        buckets[label].append(zh)

    picked = []
    for label in ("新增", "修复", "优化"):  # 固定顺序，保证输出稳定
        for s in buckets.get(label, [])[-MAX_PER_TYPE:]:
            picked.append((label, s))
    return picked[:MAX_TOTAL], stats


# ── 本仓库自己的打包改动 ────────────────────────────────────────────
# 这些提交是「我们」做的，不是上游的；用户装新包时能感知的差异主要来自这里。
_OWN_SKIP_RE = re.compile(
    r"^(?:chore:\s*update upstream pin|Add files via upload|Merge (?:pull request|branch))",
    re.I,
)
# 作用域是 ci/docs/test 的提交改的是流水线本身，装进包里的东西没变，
# 不该出现在用户的更新说明里（实测这类提交占了打包侧改动的一半以上）。
_PIPELINE_SCOPES = {"docs", "test", "ci", "chore", "build"}
_OWN_SKIP_TYPE_RE = re.compile(r"^(?:docs|test|ci|chore|style|refactor)!?:", re.I)

# 只在构建期起作用、装进包后没有任何区别的路径。
# 全部改动都落在这里的提交，不进用户更新说明。
OWN_BUILD_ONLY_RE = re.compile(
    r"(?ix)^(?:"
    r"\.github/.*"
    r"|scripts/(?:prepare-deps\.sh|prune-display\.py|build-fpk\.sh|verify-fpk\.sh|package\.sh)"
    r"|(?:[^/]+/)*docs?/.*"
    r"|[^/]*\.md"
    r"|changelog\.txt"
    r"|\.gitignore"
    r")$"
)


def own_changes(since_ref: str) -> list[str]:
    """
    取 since_ref..HEAD 之间本仓库**会影响成包内容**的提交 subject（旧→新）。

    只改 `prepare-deps.sh` / `prune-display.py` / `.github/` / 文档的提交不算：
    实测这类占了打包侧改动的 2/3（ldd 门槛、镜像回退、workflow token…），
    但它们改的是「怎么构建」，装进包里的东西一模一样，写进用户说明只会变成
    开发流水账。真正用户能感知的是 `patch-src.py`（改应用源码）、`manifest`、
    `config/`、`ui/` 这类直接进包的改动。

    since_ref 为空、或仓库是浅克隆（CI 默认 fetch-depth: 1）时返回空列表，
    由调用方降级 —— 绝不能因此让整条流水线失败。
    """
    if not since_ref:
        return []
    try:
        rng = f"{since_ref}..HEAD"
        out = subprocess.run(
            ["git", "log", "--no-merges", "--reverse", "--name-only",
             "--format=@@%s", rng],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"  读取本仓库改动失败（{type(exc).__name__}）：浅克隆？已跳过",
              file=sys.stderr)
        return []

    items: list[str] = []
    subject, files = "", []
    for line in out.splitlines() + ["@@"]:
        if line.startswith("@@"):
            if subject and not _is_build_only(files):
                s = clean_subject(subject)
                if s and not _OWN_SKIP_RE.match(s):
                    m = re.match(r"^(\w+)\(([^)]*)\)!?:\s*(.*)$", s)
                    if m:
                        if (m.group(1).lower() in SKIP_TYPES
                                or m.group(2).lower() in _PIPELINE_SCOPES):
                            s = ""
                        else:
                            s = m.group(3).strip()   # 去掉 fix(scope): 前缀
                    else:
                        # 无作用域写法 `fix: xxx`，同样要剥前缀；
                        # 实测这类占多数，漏掉会让用户直接看到 `perf: 精简包体…`
                        m3 = re.match(r"^(\w+)!?:\s*(.+)$", s)
                        if m3:
                            if m3.group(1).lower() in SKIP_TYPES:
                                s = ""
                            else:
                                s = m3.group(2).strip()
                        elif _OWN_SKIP_TYPE_RE.match(s):
                            s = ""
                    if s:
                        items.append(s)
            subject, files = line[2:], []
        elif line.strip():
            files.append(line.strip())
    return items


def _is_build_only(files: list[str]) -> bool:
    """全部改动文件都落在「只在构建期起作用」的路径上。"""
    if not files:
        return True
    return all(OWN_BUILD_ONLY_RE.match(f) for f in files)


def guess_own_since() -> str:
    """自动推断上一次自动发布的位置（auto-update 的 chore 提交）。"""
    try:
        out = subprocess.run(
            ["git", "log", "--format=%H", "--grep=^chore: update upstream pin", "-1"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


# ── 渲染 ────────────────────────────────────────────────────────────
def _upstream_sentence(picked, stats, sha: str) -> str:
    """
    上游部分的一句话概述。

    优先级：能翻译的条目 > 改动模块 > 明确的「无影响」说明。
    **任何情况下都不再输出裸的「跟随上游更新（hash）」**：那既没告诉用户
    改了什么，也没告诉他这改动跟他装的包有没有关系。
    """
    if picked:
        groups = {}
        for label, s in picked:
            groups.setdefault(label, []).append(s)
        parts = []
        for label in ("新增", "修复", "优化"):
            if label in groups:
                parts.append(f"{label}：{'；'.join(groups[label])}")
        return "。".join(parts) + "。"

    if stats["dropped_subjects"]:
        # 有相关改动但翻不干净 —— 报模块，不报错译
        areas = areas_of(stats["relevant_files"]) or "上游核心代码"
        subj = stats["dropped_subjects"][0][1]
        hint = f"（如 {subj[:60]}）" if subj else ""
        return (f"上游 {len(stats['dropped_subjects'])} 处改动涉及{areas}{hint}，"
                f"详见上游仓库。")

    if stats["irrelevant"] and not stats["relevant_total"]:
        return "上游本次仅改动 Docker 部署、测试与文档，原生包功能无变化。"

    if stats["relevant_total"] == 0 and stats["skipped"]:
        return "上游本次仅有维护类提交，功能无变化。"

    return f"上游 {sha[:7]} 未带来原生包可见的功能变化。"


def render_full(picked, stats, own, sha: str, include_upstream: bool = True) -> str:
    """完整版：Release 正文 / FnDepot 索引用。"""
    parts = []
    if include_upstream:
        parts.append(_upstream_sentence(picked, stats, sha))
    if own:
        # 取最近几条（own 是旧→新），最近的改动才是这次发包里的
        recent = own[-MAX_OWN:]
        parts.append("打包侧：" + "；".join(recent) + "。")
    if not parts:
        return "本次发布未包含上游或打包侧的可见改动。"
    text = "".join(parts)
    if len(text) > FULL_LEN:
        text = text[: FULL_LEN - 1].rstrip("；，。 ") + "…"
    return text


def render_compact(full: str) -> str:
    """
    压缩版：manifest 单行用。

    只截断，不做二次改写 —— 两处文案必须同源，否则用户在应用中心和
    Release 页面会看到两种说法。
    """
    if len(full) <= MAX_LEN:
        return full
    cut = full[: MAX_LEN - 1]
    # 尽量在分句处断开，避免半句
    for sep in ("；", "。", "，"):
        pos = cut.rfind(sep)
        if pos > MAX_LEN // 2:
            return cut[: pos + 1].rstrip("；，。 ") + "…"
    return cut.rstrip("；，。 ") + "…"


def set_manifest_changelog(manifest: Path, text: str) -> bool:
    """幂等写回 manifest 的 changelog 单行字段；返回是否改动。"""
    src = manifest.read_text(encoding="utf-8")
    line = f"changelog             = {text}"
    m = re.search(r"(?m)^changelog[ \t]*=.*$", src)
    if m:
        if m.group(0) == line:
            return False
        out = src[: m.start()] + line + src[m.end():]
    else:
        # 插在 checksum 之前（manifest 末尾附近），避免破坏对齐风格
        anchor = re.search(r"(?m)^checksum[ \t]*=", src)
        pos = anchor.start() if anchor else len(src)
        out = src[:pos] + line + "\n" + src[pos:]
    manifest.write_text(out, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="halfwaystudent/douyin-sparkflow")
    ap.add_argument("--prev-sha", default="",
                    help="上一版 pin 的上游 SHA；首次更新可留空")
    ap.add_argument("--new-sha", default="",
                    help="本次要构建的上游 SHA；--own-only 时可省略")
    ap.add_argument("--own-since", default=None,
                    help="本仓库起点 ref（默认自动推断上次自动发布点）")
    ap.add_argument("--own-only", action="store_true",
                    help="只输出打包侧改动，完全不调上游 API")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--out", type=Path, help="完整版 changelog 写到该文件")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.own_only and a.new_sha:
        ap.error("--own-only 与 --new-sha 不能同时使用")
    if not a.own_only and not re.fullmatch(r"[0-9a-f]{40}", a.new_sha):
        ap.error("--new-sha 必须是 40 位小写 commit SHA（或用 --own-only）")
    if a.prev_sha and not re.fullmatch(r"[0-9a-f]{40}", a.prev_sha):
        ap.error("--prev-sha 必须是 40 位小写 commit SHA")

    if a.own_only:
        print("模式：只取打包侧改动（不查上游）")
        commits, sections, picked = [], {}, []
        stats = {"irrelevant": 0, "skipped": 0, "dropped": 0,
                 "irrelevant_files": [], "relevant_files": [],
                 "dropped_subjects": [], "relevant_total": 0}
    else:
        print(f"拉取 {a.repo} 区间 {(a.prev_sha[:7] or '<首次>')}..{a.new_sha[:7]} …")
        commits = collect_commits(a.repo, a.prev_sha, a.new_sha)
        print(f"  区间内 {len(commits)} 个提交")
        sections = changelog_sections(a.repo, a.new_sha)
        print(f"  上游 CHANGELOG 小节: {sorted(sections)[-3:] if sections else '无'}")

        dates = sorted({c["commit"]["committer"]["date"][:10] for c in commits})
        hit = [d for d in dates if d in sections]
        if hit:
            print(f"  命中上游 CHANGELOG 小节 {hit[-1]}（{len(sections[hit[-1]])} 条手写摘要，"
                  f"仅作校验，文案仍取 commit）")

        picked, stats = build_from_commits(a.repo, commits)
    print(f"  上游：相关 {stats['relevant_total']} 条 / 无关（Docker·测试·文档）"
          f"{stats['irrelevant']} 条 / 类型跳过 {stats['skipped']} 条 / "
          f"无法中文化 {stats['dropped']} 条 → 采信 {len(picked)} 条")

    since = a.own_since if a.own_since is not None else guess_own_since()
    own = own_changes(since)
    print(f"  打包侧：{'起点 ' + since[:7] if since else '无起点'} → {len(own)} 条自己的改动")
    for s in own[:8]:
        print(f"    · {s}")

    full = render_full(picked, stats, own, a.new_sha,
                       include_upstream=not a.own_only)
    compact = render_compact(full)
    print(f"\n完整版（{len(full)} 字符）:\n  {full}")
    print(f"\nmanifest 压缩版（{len(compact)} 字符）:\n  {compact}")

    if a.dry_run:
        print("\n[dry-run] 未写任何文件")
        return 0
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(full + "\n", encoding="utf-8")
        print(f"已写入 {a.out}")
    if a.manifest:
        changed = set_manifest_changelog(a.manifest, compact)
        print(f"manifest changelog: {'已更新' if changed else '无变化'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
