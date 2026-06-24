import os
from openai import OpenAI
from config.config import LLM_API_KEY, LLM_BASE_URL, LLM_DEFAULT_MODEL

MY_API_KEY = (
    LLM_API_KEY
    or os.getenv("DEEPSEEK_API_KEY")
    or os.getenv("QWEN_API_KEY")
    or os.getenv("DASHSCOPE_API_KEY")
    or os.getenv("OPENAI_API_KEY")
)
if not MY_API_KEY:
    raise RuntimeError("Missing LLM API key. Set DEEPSEEK_API_KEY, QWEN_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY.")

print(f"🔄 Connecting to: {LLM_BASE_URL}")
print(f"🤖 Using Model: {LLM_DEFAULT_MODEL}")

try:
    client = OpenAI(
        api_key=MY_API_KEY,
        base_url=LLM_BASE_URL,
    )

    completion = client.chat.completions.create(
        model=LLM_DEFAULT_MODEL,
        messages=[{"role": "user", "content": "Hello, verify connection."}]
    )
    
    print("\n✅ Success! Response:")
    print(completion.choices[0].message.content)

except Exception as e:
    print(f"\n❌ Failed: {e}")
