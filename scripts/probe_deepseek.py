"""Day1 前置探测：验证 DeepSeek 的 tool_calls 格式，用法：python scripts/probe_deepseek.py"""
import json
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    max_retries=0,
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_order",
        "description": "按订单号查询订单详情，返回收货城市、状态和金额",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号，例如 A1001"}
            },
            "required": ["order_id"],
        },
    },
}]

def probe(model):
    print(f"\n===== {model} =====")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "帮我查一下订单 A1001 的收货城市"}],
            tools=TOOLS,
            timeout=60,
        )
        msg = resp.choices[0].message
        print("finish_reason:", resp.choices[0].finish_reason)
        print("tool_calls:", json.dumps(msg.tool_calls and [tc.model_dump() for tc in msg.tool_calls],
                                        ensure_ascii=False, indent=2))
        print("content:", (msg.content or "")[:200])
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e)[:300])

if __name__ == "__main__":
    probe("deepseek-chat")
    probe("deepseek-reasoner")
