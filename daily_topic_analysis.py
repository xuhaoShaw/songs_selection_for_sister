# -*- coding: utf-8 -*-
"""每日楼道清唱选题分析 —— 平台无关单脚本实现。

把「选题分析」+「飞书推送」两个阶段合并为一个定时任务：
  阶段一：调用 LLM 生成 5 首选题 -> 渲染 HTML 报告落盘
  阶段二：读取报告 -> 生成摘要 -> 推送飞书群（webhook 或 企业应用 API）

依赖 requests + lunar-python，不依赖 lark-cli，可部署到 cron / GitHub Actions / 云函数。

环境变量：
  MODELVERSE_API_KEY    必填，ModelVerse API Key
  LLM_BASE_URL          可选，默认 https://api.modelverse.cn/v1
  LLM_MODEL             可选，默认 deepseek-v4.1-flash
  REPORT_DIR            可选，报告目录，默认 daily-analysis
  HISTORY_FILE          可选，持久化歌曲历史路径，默认 song-history.json
  FEISHU_WEBHOOK        可选，飞书群自定义机器人 webhook（推荐）
  FEISHU_APP_ID         可选，企业自建应用 app_id（webhook 缺失时用）
  FEISHU_APP_SECRET     可选，企业自建应用 app_secret
  FEISHU_CHAT_ID        可选，目标群 ID，默认 oc_7218400c9a4ca099f4cecb2e3d32111e
"""
import datetime
from html import escape
import json
import os
import re
import sys
import tempfile
import unicodedata
from zoneinfo import ZoneInfo

from holiday_calendar import get_holiday_context
from cover_image import generate_cover

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 requests，请先执行: pip install requests\n")
    raise

# ----------------------------- 配置 -----------------------------
REPORT_DIR = os.environ.get("REPORT_DIR", "daily-analysis")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "song-history.json")
LLM_API_KEY = os.environ.get("MODELVERSE_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.modelverse.cn/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash")
COVER_ENABLED = os.environ.get("COVER_ENABLED", "true").lower() not in ("false", "0", "no")
COVER_MODEL = os.environ.get("COVER_MODEL", "gpt-image-2")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_7218400c9a4ca099f4cecb2e3d32111e")

BLOGGER_PROFILE = (
    "诗濛（有关必回），女声清唱，非科班，INFJ-T，河南IP，粉丝200+。"
    "避坑：不使用薛之谦等极度内卷歌手赛道。"
    "优先：女声友好、清唱适合、情绪标签强、非内卷赛道的歌曲。"
)

BACKUP_SONGS = "阿楚姑娘、甲乙丙丁、乱徵、心似烟火、亲爱的你啊"


def today_date() -> datetime.date:
    """统一按北京时间划分推荐日期，与运行机器的时区无关。"""
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date()


def song_key(name: str) -> str:
    """按歌名去重；换歌手、Live/清唱版本仍视作同一首歌。"""
    name = unicodedata.normalize("NFKC", name).casefold().strip()
    name = re.sub(
        r"[（(【\[]\s*(?:live|现场(?:版)?|清唱(?:版)?|翻唱(?:版)?|cover|acoustic|伴奏版)\s*[）)】\]]",
        "", name,
    )
    return "".join(c for c in name if c.isalnum())


def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {"version": 1, "entries": []}
    with open(HISTORY_FILE, encoding="utf-8") as f:
        history = json.load(f)
    if not isinstance(history, dict) or history.get("version") != 1 or not isinstance(history.get("entries"), list):
        raise ValueError("歌曲历史格式错误，停止推荐以免重复")
    for entry in history["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("songs"), list):
            raise ValueError("歌曲历史记录格式错误")
        datetime.date.fromisoformat(entry["date"])
        for song in entry["songs"]:
            if not isinstance(song, dict) or not isinstance(song.get("song"), str) or not song_key(song["song"]):
                raise ValueError("歌曲历史中的歌名无效")
    return history


def recent_songs(history: dict, today: datetime.date) -> dict:
    cutoff = today - datetime.timedelta(days=6)
    excluded = {}
    for entry in history["entries"]:
        if cutoff <= datetime.date.fromisoformat(entry["date"]) <= today:
            for song in entry["songs"]:
                excluded[song_key(song["song"])] = song["song"]
    return excluded


def save_history(history: dict, songs: list, today: datetime.date) -> None:
    """只记录推送成功的歌曲，保留 30 天；原子替换防止文件写到一半。"""
    entries = [entry for entry in history["entries"]
               if datetime.date.fromisoformat(entry["date"]) >= today - datetime.timedelta(days=29)]
    entries.append({"date": today.isoformat(), "songs": [
        {"song": s["song"], "artist": s["artist"]} for s in songs
    ]})
    path = os.path.abspath(HISTORY_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as f:
            tmp_path = f.name
            json.dump({"version": 1, "entries": entries}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ----------------------------- 阶段一 -----------------------------
def call_llm(prompt: str) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("未设置 MODELVERSE_API_KEY 环境变量")

    today = today_date().strftime("%Y年%m月%d日")
    sys_prompt = (
        "你是小红书楼道清唱翻唱博主的选题分析师。"
        f"今天是{today}。博主画像：{BLOGGER_PROFILE} "
        f"若无法检索实时热歌，可参考曲库：{BACKUP_SONGS}，也可选择其他已知歌曲。"
        "必须遵守用户给出的禁选歌单；参考曲库中的歌曲也不能违反禁选规则。"
        "根据已知歌曲选题，不要声称检索过实时热榜。"
        "只输出 JSON，不要输出任何解释文字或 markdown 代码块。"
    )
    resp = requests.post(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "max_tokens": 16384,
        },
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("ModelVerse 返回缺少 choices")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("ModelVerse 输出被 max_tokens 截断，无法生成完整选题")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("ModelVerse 返回的消息内容为空")
    return content


def parse_json(text: str):
    """从 LLM 输出中稳健提取 JSON 对象。"""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        text = match.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    return json.loads(text)


def gen_topics(excluded=None, holiday_context=None) -> dict:
    context = holiday_context if holiday_context is not None else get_holiday_context(today_date())
    quota = context["min_theme_songs"]
    if type(quota) is not int or not 0 <= quota <= 5 or bool(context["theme"]) != bool(quota):
        raise ValueError("节日主题配置无效")
    blocked = dict(excluded or {})
    candidates = []
    action = ""
    lead = None
    template = (
        "请为本博主生成今日 {count} 首翻唱选题，返回 JSON，结构如下："
        '{"action":"一条行动建议",'
        '"songs":[{"song":"歌名","artist":"歌手","reason":"一句话推荐理由",'
        '"title":"首推标题（有情绪钩子、反模板）","tags":["标签1","标签2","标签3"],'
        '"is_theme":false,"theme_reason":"与主主题的关联及清唱适配理由"}],'
        '"lead":{"song":"首推歌名","artist":"首推歌手",'
        '"reason":"明确当天关联、女声清唱适配性和首推理由",'
        '"singing_segment":"建议演唱哪个段落、如何起止以及情绪处理",'
        '"opening":"可执行的视频开场方式",'
        '"practice_tips":"练习难点和两到三个具体准备步骤",'
        '"cover_headline":"16字以内的封面情绪短句，不写楼道清唱、不编造个人经历",'
        '"title_options":["另一种标题角度1","另一种标题角度2"]}}'
        "要求：标题每篇独一无二，禁止出现『翻唱《X》，有没有唱进你心里』式模板；"
        "标签建立场景词矩阵（#楼道清唱/#清唱/#翻唱）+ 情绪词 + 歌名/歌手词。"
        "只为一首主推补充详细拍摄建议，四首副推保持原有歌曲字段。"
        "主推说明需具体但简洁，各详细字段控制在一到两句话；"
        "封面文案突出歌曲情绪或听歌场景，不宣称实时热度或保证涨粉；"
        "未核实具体音源时不要编造时间戳、歌词、调性或音域；"
        "不要编造博主的个人经历，开场建议以真实表达或直接开唱为主。"
    )
    # 保留日常候选作为降级备选；满五首但主题不足时继续补选主题。
    for _ in range(3):
        themed_count = sum(song["is_theme"] for song in candidates)
        missing_theme = max(0, quota - themed_count)
        count = max(5 - len(candidates), missing_theme)
        prompt = template.replace("{count}", str(count))
        prompt += (
            "程序提供的节日日历（不得自行改变日期或主题）："
            + json.dumps(context, ensure_ascii=False)
            + f"。本次至少补选 {missing_theme} 首主主题歌曲。"
            "只有歌曲本身表达主主题且适合女声清唱，才标记 is_theme=true 并给出具体理由；"
            "禁止仅凭标题或标签带节日词就判定为主题。普通日期 is_theme=false。"
            "候选满足时优先从主题歌中首推。结合已保留候选和新增歌曲重新生成 lead，"
            "说明须对应最终推荐歌曲，不复用被替换歌曲的说明。"
            "禁止声称必涨粉、独立验证了主题或检索过实时热度。已保留候选："
            + json.dumps(candidates, ensure_ascii=False)
        )
        prompt += (
            "以下歌曲在最近一周已推送或本轮已选中，禁止推荐，包括不同歌手的翻唱、Live 和清唱版本："
            + json.dumps(list(blocked.values()), ensure_ascii=False)
            + "。请扩展选曲范围，返回真实存在且互不重复的歌曲。"
        )
        raw = call_llm(prompt)
        try:
            data = parse_json(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("songs"), list):
            continue
        if isinstance(data.get("action"), str) and data["action"].strip():
            action = data["action"].strip()
        for song in data["songs"]:
            if not isinstance(song, dict):
                continue
            if any(not isinstance(song.get(field), str) or not song[field].strip()
                   for field in ("song", "artist", "reason", "title")):
                continue
            if not isinstance(song.get("tags"), list) or any(not isinstance(tag, str) for tag in song["tags"]):
                continue
            key = song_key(song["song"])
            if not key or key in blocked:
                continue
            song = dict(song)
            theme_reason = song.get("theme_reason", "")
            song["is_theme"] = bool(
                context["theme"] and song.get("is_theme") is True
                and isinstance(theme_reason, str) and theme_reason.strip()
            )
            song["theme_reason"] = theme_reason.strip() if song["is_theme"] else ""
            candidates.append(song)
            blocked[key] = song["song"]
        # 只采用最新一次有效响应的说明，补选后不会沿用旧候选的说明。
        lead = data.get("lead")
        if len(candidates) >= 5 and sum(s["is_theme"] for s in candidates) >= quota:
            break
    if len(candidates) < 5:
        raise RuntimeError(f"三次选曲后仅获得 {len(candidates)} 首合格歌曲，未能满足一周内不重复的要求")
    themed = [s for s in candidates if s["is_theme"]]
    eligible = themed or candidates
    first = eligible[0]
    lead_reason = ""
    if isinstance(lead, dict) and isinstance(lead.get("reason"), str) and lead["reason"].strip():
        for song in eligible:
            if song_key(str(lead.get("song", ""))) == song_key(song["song"]) and lead.get("artist") == song["artist"]:
                first = song
                lead_reason = lead["reason"].strip()
                break
    ordered = [first]
    for song in themed:
        if len(ordered) >= max(1, quota):
            break
        if song is not first:
            ordered.append(song)
    for song in candidates:
        if len(ordered) == 5:
            break
        if song not in ordered:
            ordered.append(song)
    actual = sum(s["is_theme"] for s in ordered)
    warning = f"主题配额不足：目标至少 {quota} 首，实际 {actual} 首；已用其他合格歌曲补齐五首。" if actual < quota else ""
    lead_details = _make_lead_details(first, lead if lead_reason else None)
    if not lead_reason:
        association = first["theme_reason"] or (
            "本日主题候选不足，选择日常备选" if context["theme"] else "今日为日常选题"
        )
        lead_reason = f"首推《{first['song']}》：{association}；女声清唱选题建议：{first['reason']}"
    return {
        "action": action, "songs": ordered, "holiday_context": context,
        "theme_count": actual, "quota_warning": warning,
        "lead": {"song": first["song"], "artist": first["artist"], "reason": lead_reason},
        "lead_details": lead_details,
    }


def _make_lead_details(song: dict, source=None) -> dict:
    """详细建议只绑定合格主推；缺失字段使用不依赖音源的保守建议。"""
    source = source if isinstance(source, dict) else {}
    defaults = {
        "singing_segment": "选择自己熟悉、能稳定清唱的副歌或情绪集中段落；试录后确定起止，不强行挑战高音。",
        "opening": "直接从熟悉段落开唱，先保证第一句的音准和情绪，避免过长铺垫。",
        "practice_tips": "先用舒适音区试唱，标记换气位置，再录一遍检查音准与咬字；不舒服时换段落或副推歌曲。",
    }
    result = {}
    for key, fallback in defaults.items():
        value = source.get(key)
        result[key] = value.strip() if isinstance(value, str) and value.strip() else fallback
    headline = source.get("cover_headline")
    result["cover_headline"] = (
        headline.strip() if isinstance(headline, str) and 0 < len(headline.strip()) <= 16
        and "\n" not in headline and "楼道" not in headline else ""
    )
    titles = source.get("title_options")
    result["title_options"] = []
    if isinstance(titles, list):
        for title in titles:
            if isinstance(title, str) and title.strip() and title.strip() != song["title"]:
                title = title.strip()
                if title not in result["title_options"]:
                    result["title_options"].append(title)
                if len(result["title_options"]) == 2:
                    break
    return result


def _lead_detail_items(data: dict) -> list:
    details = _make_lead_details(data["songs"][0], data.get("lead_details"))
    items = [("演唱段落", details["singing_segment"]),
             ("视频开场", details["opening"]),
             ("练习准备", details["practice_tips"])]
    if details["cover_headline"]:
        items.append(("封面文案", details["cover_headline"]))
    for i, title in enumerate(details["title_options"], 1):
        items.append((f"其他标题 {i}", title))
    return items


def _theme_label(data: dict) -> str:
    context = data.get("holiday_context", {})
    label = f"{context['theme']}（{context['phase']}）" if context.get("theme") else "日常选题"
    if context.get("secondary_themes"):
        label += f"；同时关注：{'、'.join(context['secondary_themes'])}"
    return label


def render_html(data: dict) -> str:
    date = today_date()
    date_cn = date.strftime("%Y.%m.%d")
    title = f"{date.strftime('%Y%m%d')} 每日选题分析"

    cards = []
    for i, s in enumerate(data["songs"], 1):
        tags_html = "".join(
            f'<span class="t">{escape(t)}</span>' for t in s.get("tags", [])
        )
        theme_reason = f'<div class="reason">主题关联（模型判断）：{escape(s["theme_reason"])}</div>' if s.get("is_theme") else ""
        lead_html = f'<div class="action">首推说明：{escape(data.get("lead", {}).get("reason", s["reason"]))}</div>' if i == 1 else ""
        if i == 1:
            lead_html += "".join(
                f'<div class="lbl">{label}</div><div class="reason">{escape(value)}</div>'
                for label, value in _lead_detail_items(data)
            )
            cover = data.get("cover", {})
            if cover.get("b64"):
                lead_html += (f'<div class="lbl">主推主题封面（AI 生成，发布前请检查文字）</div>'
                              f'<img style="width:100%;border-radius:10px" '
                              f'src="data:image/png;base64,{escape(cover["b64"], quote=True)}" '
                              f'alt="{escape(s["song"], quote=True)}主题封面">')
            if data.get("cover_warning"):
                lead_html += f'<div class="reason">{escape(data["cover_warning"])}</div>'
        cards.append(
            f"""<div class="card"><div class="rank">{'今日主推' if i == 1 else f'副推 {i - 1}'}</div>
<div class="head">{escape(s['song'])}<span class="art"> {escape(s['artist'])}</span></div>
<div class="reason">{escape(s['reason'])}</div>{theme_reason}{lead_html}
<div class="lbl">建议标题</div><div class="ttl">“{escape(s['title'])}”</div>
<div class="lbl">标签</div><div class="tags">{tags_html}</div></div>"""
        )

    warning = f'<div class="action">{escape(data["quota_warning"])}</div>' if data.get("quota_warning") else ""
    preview = data.get("holiday_context", {}).get("tomorrow_preview", "")
    preview_html = f'<div class="action">{escape(preview)}</div>' if preview else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>{title}</title><style>
body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:#fdf8f4;color:#2d2d2d;margin:0;padding:24px;line-height:1.6}}
.wrap{{max-width:680px;margin:0 auto}}h1{{font-size:22px}}h2{{font-size:16px;color:#c7523a}}
.sub{{color:#8c7b6e;font-size:13px}}.card{{background:#fff;border:1px solid #e8dcd0;border-radius:10px;padding:16px;margin:14px 0}}
.rank{{display:inline-block;background:#c7523a;color:#fff;font-size:12px;padding:2px 10px;border-radius:12px;margin-bottom:8px}}
.head{{font-size:18px;font-weight:700}}.art{{color:#8c7b6e;font-weight:400;font-size:14px}}
.reason{{font-size:14px;color:#555;margin:6px 0}}
.lbl{{font-size:11px;color:#c7523a;font-weight:700;margin-top:10px;letter-spacing:1px}}
.ttl{{background:#f4e8e4;border-left:3px solid #c7523a;padding:8px 10px;margin:4px 0;font-size:14px}}
.tags{{margin-top:4px}}.t{{display:inline-block;background:#e8f0f4;color:#5b7a8c;font-size:12px;padding:2px 10px;border-radius:12px;margin:2px 4px 2px 0}}
.action{{background:#faf3eb;border:1px solid #e8dcd0;border-radius:10px;padding:14px;font-size:14px;margin:18px 0}}
.foot{{color:#8c7b6e;font-size:12px;text-align:center;margin-top:24px}}</style></head>
<body><div class="wrap"><h1>{title}</h1>
<p class="sub">诗濛（有关必回）· 楼道清唱翻唱 · {date_cn}</p>
<h2>今日主题：{escape(_theme_label(data))}</h2>{warning}
{''.join(cards)}
<div class="action"><b>今日行动建议：</b>{escape(data.get('action', ''))}</div>
{preview_html}
<div class="foot">AI 选题助手自动生成 · 仅供内部参考</div></div></body></html>"""


# ----------------------------- 阶段二 -----------------------------
def _markdown_text(value: str) -> str:
    """模型内容作为普通文字展示，格式标记只由程序提供。"""
    value = escape(value, quote=False)
    for char in ("\\", "*", "_", "`", "[", "]", "~"):
        value = value.replace(char, "\\" + char)
    return value


def build_summary(data: dict, link: str) -> str:
    today = today_date()
    date_cn = f"{today.month}月{today.day}日"
    text = _markdown_text
    sections = [f"**【{date_cn} 选题分析】**", f"**今日主题：**{text(_theme_label(data))}"]
    if data.get("quota_warning"):
        sections.append(f'**主题配额提醒：**{text(data["quota_warning"])}')
    if data.get("cover_warning"):
        sections.append(f'**封面提醒：**{text(data["cover_warning"])}')
    for i, s in enumerate(data["songs"], 1):
        rank = "今日主推" if i == 1 else f"副推 {i - 1}"
        heading = f'{rank}：{s["artist"]}《{s["song"]}》'
        if i == 1:
            parts = [f"**{text(heading)}**",
                     f'**建议标题：**{text(s["title"])}',
                     f'**首推说明：**{text(data.get("lead", {}).get("reason", s["reason"]))}']
            parts.extend(f"**{label}：**{text(value)}" for label, value in _lead_detail_items(data))
            if s.get("tags"):
                parts.append(f'**主推标签：**{text(" ".join(s["tags"]))}')
        else:
            parts = [f'**{text(heading)}** - "{text(s["title"])}"']
        if s.get("is_theme"):
            parts.append(f'**主题关联（模型判断）：**{text(s["theme_reason"])}')
        # 主推各建议独立成段；四首副推各自成段，保留原有字段。
        sections.append(("\n\n" if i == 1 else "\n").join(parts))
    preview = data.get("holiday_context", {}).get("tomorrow_preview", "")
    if preview:
        sections.append(f"**明日准备：**{text(preview)}")
    sections.append(f'**行动：**{text(data.get("action", ""))}')
    if link:
        sections.append(f"**详细报告：**{text(link)}")
    return "\n\n".join(sections)


def build_feishu_card(text: str, image_key: str = "") -> dict:
    elements = [{"tag": "markdown", "content": text}]
    if image_key:
        # 插在主推详情后、副推之前；标记由 build_summary 生成。
        main, separator, secondary = text.partition("\n\n**副推 1：")
        elements = [{"tag": "markdown", "content": main},
                    {"tag": "img", "img_key": image_key,
                     "alt": {"tag": "plain_text", "content": "主推主题封面（AI 生成，发布前请检查文字）"}}]
        if separator:
            elements.append({"tag": "markdown", "content": "**副推 1：" + secondary})
    return {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }


def _tenant_token() -> str:
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        raise RuntimeError("未配置 FEISHU_APP_ID/FEISHU_APP_SECRET")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("tenant_access_token")
    if not token:
        raise RuntimeError(f"获取 tenant_access_token 失败: {resp.text}")
    return token


class FeishuMessageRejected(RuntimeError):
    """接口明确拒绝消息时才可安全重试，超时不重发以免重复。"""


def upload_feishu_image(path: str) -> str:
    token = _tenant_token()
    with open(path, "rb") as image:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (os.path.basename(path), image, "image/png")},
            timeout=60,
        )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0 or not body.get("data", {}).get("image_key"):
        raise RuntimeError("飞书图片上传失败，请检查机器人能力及 im:resource 权限")
    return body["data"]["image_key"]


def prepare_cover(data: dict, today) -> None:
    if not COVER_ENABLED:
        return
    try:
        data["cover"] = generate_cover(data, REPORT_DIR, today, LLM_API_KEY,
                                       LLM_BASE_URL, COVER_MODEL)
    except Exception as error:
        # 不把第三方响应、签名下载地址或密钥写到日志及群消息。
        data["cover_warning"] = f"主题封面生成失败（{type(error).__name__}），本次保留歌曲推荐。"
        return
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        data["cover_warning"] = "主题封面已生成，但缺少飞书图片上传配置；可从 HTML 报告或云端运行附件获取。"
        return
    try:
        data["cover"]["image_key"] = upload_feishu_image(data["cover"]["path"])
    except Exception as error:
        data["cover_warning"] = f"主题封面已生成，但飞书图片上传失败（{type(error).__name__}）；可从 HTML 报告或云端运行附件获取。"


def send_feishu(text: str, image_key: str = "") -> None:
    card = build_feishu_card(text, image_key)
    if FEISHU_WEBHOOK:
        resp = requests.post(
            FEISHU_WEBHOOK,
            json={"msg_type": "interactive", "card": card},
            timeout=30,
        )
        resp.raise_for_status()
        if resp.json().get("code", 0) != 0:
            raise FeishuMessageRejected(f"webhook 失败: {resp.text}")
        return

    token = _tenant_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        timeout=30,
    )
    resp.raise_for_status()
    if resp.json().get("code", 0) != 0:
        raise FeishuMessageRejected(f"发消息失败: {resp.text}")


def main(event=None, context=None) -> dict:
    """入口；云函数可传 event/context，缺省时忽略。"""
    os.makedirs(REPORT_DIR, exist_ok=True)
    result = {"ok": True, "report": None, "push": None}

    try:
        today = today_date()
        holiday_context = get_holiday_context(today)
        history = load_history()
        data = gen_topics(recent_songs(history, today), holiday_context)
        result["theme"] = _theme_label(data)
        result["theme_count"] = data["theme_count"]
        result["quota_warning"] = data["quota_warning"]
        prepare_cover(data, today)
        result["cover"] = data.get("cover", {}).get("path")
        result["cover_warning"] = data.get("cover_warning", "")
        html = render_html(data)
        fname = today.strftime("%Y%m%d_选题分析.html")
        fpath = os.path.join(REPORT_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(html)
        result["report"] = fpath
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["report"] = f"生成报告失败: {e}"
        _fail_file(f"生成报告失败: {e}")
        return result

    try:
        summary = build_summary(data, "")
        image_key = data.get("cover", {}).get("image_key", "")
        if image_key:
            try:
                send_feishu(summary, image_key)
            except FeishuMessageRejected:
                # 图片卡片被明确拒绝才降级重发；网络错误不重试。
                data["cover_warning"] = "飞书未接受图片卡片，本次发送文字推荐；封面可从 HTML 报告或云端运行附件获取。"
                result["cover_warning"] = data["cover_warning"]
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(render_html(data))
                summary = build_summary(data, "")
                send_feishu(summary)
        else:
            send_feishu(summary)
        result["push"] = "ok"
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["push"] = f"推送失败: {e}"
        _fail_file(f"推送失败: {e}\n\n摘要内容:\n{summary}")
        return result
    try:
        save_history(history, data["songs"], today)
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["history"] = f"推送已成功，但保存历史失败: {e}"
        _fail_file(result["history"])
    return result


def _fail_file(msg: str) -> None:
    with open(os.path.join(REPORT_DIR, "push-failed.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
