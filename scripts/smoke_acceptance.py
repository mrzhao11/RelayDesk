#!/usr/bin/env python3
"""Run RelayDesk's eight minimum acceptance scenarios against a live API."""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid


CASES = [
    ("你好", {"agents": {"general"}, "knowledge": False}),
    ("忘记企业账号密码怎么办？", {"agents": {"technical"}}),
    ("登录一直报401。", {"agents": {"technical"}}),
    ("页面出现500错误应该怎么处理？", {"agents": {"technical"}}),
    ("如何修改发票抬头？", {"agents": {"billing"}}),
    ("为什么被重复扣款？", {"agents": {"billing"}}),
    ("登录报401，而且还被重复扣款。", {"agents": {"technical", "billing"}}),
    ("我要投诉，请帮我转人工。", {"agents": {"escalation"}, "knowledge": False}),
]


def post_chat(base_url, payload, timeout):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def validate(data, expected):
    actual_agents = set(data.get("agent_types") or [])
    if not actual_agents and data.get("agent_type"):
        actual_agents.add(data["agent_type"])
    missing = expected["agents"] - actual_agents
    if missing:
        return False, f"缺少 Agent: {sorted(missing)}，实际: {sorted(actual_agents)}"
    if "knowledge" in expected and bool(data.get("knowledge_used")) != expected["knowledge"]:
        return False, f"knowledge_used={data.get('knowledge_used')}"
    if not str(data.get("response", "")).strip():
        return False, "响应为空"
    return True, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--user-id", default="acceptance_user")
    args = parser.parse_args()

    failed = 0
    for index, (message, expected) in enumerate(CASES, start=1):
        payload = {
            "message": message,
            "user_id": args.user_id,
            "conv_id": f"acceptance-{uuid.uuid4()}",
        }
        try:
            data = post_chat(args.base_url, payload, args.timeout)
            ok, detail = validate(data, expected)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as ex:
            ok, detail = False, str(ex)
        print(f"{'PASS' if ok else 'FAIL'} {index}. {message}{' — ' + detail if detail else ''}")
        failed += int(not ok)

    print(f"\n结果: {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
