import os
from openai import OpenAI
from config.config import LLM_BASE_URL, LLM_DEFAULT_MODEL

# 1. 这里尝试直接从代码里硬编码 KEY，先跑通再说！
# 替换成你真实的 Key，例如 "sk-..."
MY_API_KEY = "sk-871f422c6" 

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