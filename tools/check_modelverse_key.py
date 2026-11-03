"""上线前自检：确认 MODELVERSE_API_KEY 可用、模型可访问。

放在选题任务之前运行：密钥一旦失效，立刻给出明确原因，
而不是等到生成报告时抛出一个只有状码的 401。

环境变量：
  MODELVERSE_API_KEY  必填
  LLM_BASE_URL        可选，默认 https://api.modelverse.cn/v1
  LLM_MODEL           可选，默认 deepseek-v4.1-flash
"""
import os
import sys
from hashlib import sha256

import requests


BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.modelverse.cn/v1").rstrip("/")
RAW_KEY = os.environ.get("MODELVERSE_API_KEY", "")
API_KEY = RAW_KEY.strip()
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash")


def fail(message: str) -> int:
    """输出 Actions 注解并以失败退出。"""
    print(f"::error::{message}")
    return 1


def fingerprint(key: str) -> str:
    """只输出哈希前缀，既能核对是否同一把密钥，又不泄露密钥本身。"""
    return sha256(key.encode()).hexdigest()[:8]


def main() -> int:
    if not API_KEY:
        return fail("未配置 MODELVERSE_API_KEY：请在仓库 Settings → Secrets and variables → Actions 中新增。")
    if API_KEY != RAW_KEY:
        print("::warning::MODELVERSE_API_KEY 首尾含空白或换行，本次已忽略；建议重新录入该 Secret。")
    print(f"密钥指纹 {fingerprint(API_KEY)}，长度 {len(API_KEY)}；与本地不一致说明 Secret 里是另一把密钥。")

    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        return fail(f"无法访问 ModelVerse（{type(error).__name__}）：检查网络出口与 LLM_BASE_URL={BASE_URL}。")

    if resp.status_code == 401:
        return fail(f"鉴权失败（HTTP 401）：LLM_MODEL={MODEL} 可能已改名或无权使用，"
                    "也可能是 MODELVERSE_API_KEY 无效/已吊销。"
                    "本地执行 `python tools/diagnose_modelverse.py` 可对比不同模型名的结果。")
    if resp.status_code == 402:
        return fail("ModelVerse 余额或额度不足（HTTP 402）：请充值或确认套餐额度。")
    if resp.status_code == 403:
        return fail("ModelVerse 拒绝访问（HTTP 403）：确认密钥权限与账号状态。")
    if resp.status_code == 404:
        return fail(f"接口或模型不存在（HTTP 404）：确认 LLM_MODEL={MODEL} 是否仍然可用。")
    if resp.status_code == 429:
        return fail("ModelVerse 限流（HTTP 429）：稍后重试。")
    if resp.status_code >= 400:
        return fail(f"ModelVerse 返回 HTTP {resp.status_code}：{resp.text[:200]}")

    print(f"ModelVerse 密钥可用，模型 {MODEL} 响应正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
