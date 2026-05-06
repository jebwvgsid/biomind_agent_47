import sys
import os
from pathlib import Path

# 1. 解决路径问题
current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# 2. 导入配置和模块
try:
    from config.config import RAGFLOW_ADDRESS, RAGFLOW_API_KEY, KB_MAP, RERANK_MODEL_ID
    from agent_core.retriever import RAGFlowRetriever
except ImportError as e:
    print(f"❌ Import Failed: {e}")
    print("Please make sure you are running this script from the project root.")
    sys.exit(1)

def test_rag_retrieval(tool_name="gatk"):
    print(f"{'='*50}")
    print(f"🔍 Testing RAG Retrieval for: {tool_name}")
    print(f"{'='*50}")

    # 1. 检查配置
    kb_id = KB_MAP.get("TOOLS")
    print(f"⚙️  RAG Address: {RAGFLOW_ADDRESS}")
    print(f"⚙️  Target KB ID (TOOLS): {kb_id}")
    
    if not kb_id:
        print("❌ Error: KB_MAP['TOOLS'] is not defined in config/config.py")
        return

    # 2. 初始化检索器
    try:
        retriever = RAGFlowRetriever(
            api_key=RAGFLOW_API_KEY,
            base_url=RAGFLOW_ADDRESS,
            dataset_ids=[kb_id],
            rerank_id=RERANK_MODEL_ID
        )
        print("✅ Retriever Initialized.")
    except Exception as e:
        print(f"❌ Init Failed: {e}")
        return

    # 3. 执行搜索
    query = f"How to install {tool_name} linux command line"
    print(f"❓ Sending Query: '{query}'")
    
    try:
        results = retriever.retrieve(
            query=query,
            target_datasets=[kb_id],
            top_k=2,
            similarity_threshold=0.3 # 可以在这里临时调低阈值测试
        )
        
        # 4. 展示结果
        if results:
            print(f"\n🎉 SUCCESS! Found content:\n{'-'*40}")
            print(results)
            print(f"{'-'*40}")
        else:
            print(f"\n⚠️  Retrieval successful, but returned EMPTY results.")
            print("Possible reasons:")
            print("1. The knowledge base is empty.")
            print("2. The query is too different from the content.")
            print("3. Similarity threshold is too high.")
            
    except Exception as e:
        print(f"\n❌ Retrieval Error: {e}")

if __name__ == "__main__":
    # 你可以修改这里的参数来测试不同的工具
    target_tool = sys.argv[1] if len(sys.argv) > 1 else "gatk"
    test_rag_retrieval(target_tool)