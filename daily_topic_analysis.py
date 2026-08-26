# -*- coding: utf-8 -*-
"""每日楼道清唱选题分析 —— 平台无关单脚本实现。

把「选题分析」+「飞书推送」两个阶段合并为一个定时任务：
  阶段一：调用 LLM 生成 5 首选题 -> 渲染 HTML 报告落盘
  阶段二：读取报告 -> 生成摘要 -> 推送飞书群（webhook 或 企业应用 API）

依赖 requests + openai，不依赖 lark-cli，可部署到 cron / GitHub Actions / 云函数。

环境变量：
  DEEPSEEK_API_KEY      必填，DeepSeek API Key
  LLM_BASE_URL          可选，默认 https://api.deepseek.com
  LLM_MODEL             可选，默认 deepseek-v4-pro（DeepSeek 当前最强模型）
  ENABLE_SEARCH         可选，1=开启联网搜索(web_search 工具)，0=关闭，默认 1
  REPORT_DIR            可选，报告目录，默认 daily-analysis
  FEISHU_WEBHOOK        可选，飞书群自定义机器人 webhook（推荐）
  FEISHU_APP_ID         可选，企业自建应用 app_id（webhook 缺失时用）
  FEISHU_APP_SECRET     可选，企业自建应用 app_secret
  FEISHU_CHAT_ID        可选，目标群 ID，默认 oc_7218400c9a4ca099f4cecb2e3d32111e
"""
import datetime
import json
import os
import re
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 requests，请先执行: pip install requests\n")
    raise

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 openai，请先执行: pip install openai\n")
    raise

# ----------------------------- 配置 -----------------------------
REPORT_DIR = os.environ.get("REPORT_DIR", "daily-analysis")
LLM_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-pro")
ENABLE_SEARCH = os.environ.get("ENABLE_SEARCH", "1") != "0"
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


# ----------------------------- 阶段一 -----------------------------
def call_llm(prompt: str) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")

    today = datetime.date.today().strftime("%Y年%m月%d日")
    sys_prompt = (
        "你是小红书楼道清唱翻唱博主的选题分析师。"
        f"今天是{today}。博主画像：{BLOGGER_PROFILE} "
        f"若无法检索实时热歌，可用兜底曲库：{BACKUP_SONGS}。"
        "只输出 JSON，不要输出任何解释文字或 markdown 代码块。"
    )
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    tools = [{"type": "web_search"}] if ENABLE_SEARCH else None
    resp = client.responses.create(
        model=LLM_MODEL,
        instructions=sys_prompt,
        input=prompt,
        tools=tools,
        temperature=0.8,
    )
    return resp.output_text


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


def gen_topics() -> dict:
    prompt = (
        "请为本博主生成今日 5 首翻唱选题，返回 JSON，结构如下："
        '{"action":"一条行动建议",'
        '"songs":[{"song":"歌名","artist":"歌手","reason":"一句话推荐理由",'
        '"title":"首推标题（有情绪钩子、反模板）","tags":["标签1","标签2","标签3"]}]}'
        "要求：标题每篇独一无二，禁止出现『翻唱《X》，有没有唱进你心里』式模板；"
        "标签建立场景词矩阵（#楼道清唱/#清唱/#翻唱）+ 情绪词 + 歌名/歌手词。"
    )
    raw = call_llm(prompt)
    data = parse_json(raw)
    if not isinstance(data.get("songs"), list) or not data["songs"]:
        raise RuntimeError("LLM 返回的选题为空")
    return data


def render_html(data: dict) -> str:
    date = datetime.date.today()
    date_cn = date.strftime("%Y.%m.%d")
    title = f"{date.strftime('%Y%m%d')} 每日选题分析"

    cards = []
    for i, s in enumerate(data["songs"], 1):
        tags_html = "".join(
            f'<span class="t">{t}</span>' for t in s.get("tags", [])
        )
        cards.append(
            f"""<div class="card"><div class="rank">推荐 {i}</div>
<div class="head">{s['song']}<span class="art"> {s['artist']}</span></div>
<div class="reason">{s['reason']}</div>
<div class="lbl">首推标题</div><div class="ttl">“{s['title']}”</div>
<div class="lbl">标签</div><div class="tags">{tags_html}</div></div>"""
        )

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
{''.join(cards)}
<div class="action"><b>今日行动建议：</b>{data.get('action', '')}</div>
<div class="foot">AI 选题助手自动生成 · 仅供内部参考</div></div></body></html>"""


# ----------------------------- 阶段二 -----------------------------
def build_summary(data: dict, link: str) -> str:
    date_cn = f"{datetime.date.today().month}月{datetime.date.today().day}日"
    lines = [f"【{date_cn} 选题分析】", "推荐歌曲："]
    for i, s in enumerate(data["songs"], 1):
        lines.append(f'{i}. {s["artist"]}《{s["song"]}》- "{s["title"]}"')
    lines.append(f'行动：{data.get("action", "")}')
    if link:
        lines.append(f"详细报告：{link}")
    return "\n".join(lines)[:300]


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


def send_feishu(text: str) -> None:
    if FEISHU_WEBHOOK:
        resp = requests.post(
            FEISHU_WEBHOOK,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=30,
        )
        resp.raise_for_status()
        if resp.json().get("code", 0) != 0:
            raise RuntimeError(f"webhook 失败: {resp.text}")
        return

    token = _tenant_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        },
        timeout=30,
    )
    resp.raise_for_status()
    if resp.json().get("code", 0) != 0:
        raise RuntimeError(f"发消息失败: {resp.text}")


def main(event=None, context=None) -> dict:
    """入口；云函数可传 event/context，缺省时忽略。"""
    os.makedirs(REPORT_DIR, exist_ok=True)
    result = {"ok": True, "report": None, "push": None}

    try:
        data = gen_topics()
        html = render_html(data)
        fname = datetime.date.today().strftime("%Y%m%d_选题分析.html")
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
        send_feishu(summary)
        result["push"] = "ok"
    except Exception as e:  # noqa: BLE001
        result["push"] = f"推送失败: {e}"
        _fail_file(f"推送失败: {e}\n\n摘要内容:\n{summary}")
    return result


def _fail_file(msg: str) -> None:
    with open(os.path.join(REPORT_DIR, "push-failed.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
