"""歌曲选题日历：内容主题窗口，不代表法定休假安排。"""
import datetime

from lunar_python import Solar


PRIORITY = (
    "春节", "国庆", "中秋", "元宵", "端午", "七夕", "元旦",
    "母亲节", "父亲节", "教师节", "情人节", "圣诞节",
)
FIXED = {(1, 1): "元旦", (10, 1): "国庆", (2, 14): "情人节",
         (9, 10): "教师节", (12, 25): "圣诞节"}
LUNAR = {(1, 1): "春节", (1, 15): "元宵", (5, 5): "端午",
         (7, 7): "七夕", (8, 15): "中秋"}
THEMES = {
    "春节": "团圆、家乡、亲情、新年祝福",
    "国庆": "祖国、家国情感、山河与祝福",
    "中秋": "月亮、思念、团圆、家乡",
    "元宵": "团圆、灯火、温暖祝福",
    "端午": "家乡、传统记忆、安康祝福",
    "七夕": "爱情、陪伴、含蓄告白",
    "元旦": "新开始、希望、新年祝福",
    "母亲节": "母爱、亲情、感恩",
    "父亲节": "父爱、亲情、感恩",
    "教师节": "师恩、成长、感恩",
    "情人节": "爱情、陪伴、告白",
    "圣诞节": "圣诞、冬日温暖、相聚与祝福",
}


def _events_on(date: datetime.date) -> list:
    events = []
    fixed = FIXED.get((date.month, date.day))
    if fixed:
        events.append(fixed)
    lunar = Solar.fromYmd(date.year, date.month, date.day).getLunar()
    # 闰月不能再次触发同一传统节日。
    festival = LUNAR.get((lunar.getMonth(), lunar.getDay()))
    if festival:
        events.append(festival)
    if date.weekday() == 6:
        if date.month == 5 and 8 <= date.day <= 14:
            events.append("母亲节")
        if date.month == 6 and 15 <= date.day <= 21:
            events.append("父亲节")
    return events


def get_holiday_context(date: datetime.date) -> dict:
    """调用方提供北京时间日期；日历错误直接向上传播。"""
    active = [(name, "当天", 2) for name in _events_on(date)]
    for days_ago in range(1, 7):
        for name in _events_on(date - datetime.timedelta(days=days_ago)):
            if name in ("春节", "国庆"):
                active.append((name, "延续", 1))
    active.sort(key=lambda event: (event[1] != "当天", PRIORITY.index(event[0])))
    name, phase, quota = active[0] if active else (None, "日常", 0)
    tomorrow = sorted(_events_on(date + datetime.timedelta(days=1)), key=PRIORITY.index)
    preview = ""
    if tomorrow:
        preview = (
            f"明日主题：{'、'.join(tomorrow)}。练歌准备：围绕"
            f"{'；'.join(THEMES[event] for event in tomorrow)}选择舒适音区，"
            "练习最有表现力的段落并提前试录；今日不预占具体歌曲。"
        )
    return {
        "date": date.isoformat(), "theme": name, "phase": phase,
        "min_theme_songs": quota, "keywords": THEMES[name] if name else "",
        "secondary_themes": list(dict.fromkeys(event[0] for event in active[1:])),
        "tomorrow_preview": preview,
    }
