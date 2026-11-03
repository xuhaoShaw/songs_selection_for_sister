"""对比同一条密钥下不同模型名的可用性，用来区分"密钥坏了"还是"模型名变了"。

背景：部分网关对「未授权/已下线的模型」返回 401 而不是 404，于是日志里
只剩一个 Unauthorized，很容易误判成密钥失效。这个脚本把两个变量拆开测。

用法：
  python tools/diagnose_modelverse.py
  python tools/diagnose_modelverse.py deepseek-v4.1-flash deepseek-v4.1-flash-1017

只发 max_tokens=1 的最小请求，不打印密钥（仅打印哈希前缀用于核对）。
"""
import os
import sys
from hashlib import sha256

import requests


BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.modelverse.cn/v1").rstrip("/")
RAW_KEY = os.environ.get("MODELVERSE_API_KEY", "")
API_KEY = RAW_KEY.strip()
DEFAULT_MODELS = ["deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"]

REASONS = {
    400: "请求参数或模型名不合法",
    401: "密钥无效，或该密钥无权使用这个模型",
    402: "余额或额度不足",
    403: "账号被拒（权限/风控）",
    404: "接口或模型不存在",
    429: "被限流",
}


def fingerprint(key: str) -> str:
    return sha256(key.encode()).hexdigest()[:8]


def probe(model: str) -> tuple:
    """返回 (状态码, 说明)；网络异常时状态码为 None。"""
    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        return None, f"网络错误 {type(error).__name__}"
    if resp.status_code < 400:
        return resp.status_code, "可用"
    reason = REASONS.get(resp.status_code, "未预期状态")
    body = (resp.text or "").strip().replace("\n", " ")[:160]
    return resp.status_code, f"{reason}｜{body}" if body else reason


def main(argv: list) -> int:
    if not API_KEY:
        print("错误：未设置 MODELVERSE_API_KEY。")
        return 2
    if API_KEY != RAW_KEY:
        print("提示：密钥首尾含空白或换行，已忽略。")

    models = argv or DEFAULT_MODELS
    print(f"接口 {BASE_URL}")
    print(f"密钥指纹 {fingerprint(API_KEY)}，长度 {len(API_KEY)}")
    print("")

    usable = []
    for model in models:
        status, detail = probe(model)
        shown = "网络异常" if status is None else f"HTTP {status}"
        print(f"{model:<32} {shown:<12} {detail}")
        if status is not None and status < 400:
            usable.append(model)

    print("")
    if usable:
        print("可用模型：" + "、".join(usable))
        print("更新仓库 Secrets 后，把 workflow 里的 LLM_MODEL 改成上面任意一个即可。")
        return 0
    print("所有候选模型都不可用：优先检查密钥本身，其次确认模型名是否已更换。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
