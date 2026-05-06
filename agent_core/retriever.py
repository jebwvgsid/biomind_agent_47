import requests
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

class RAGFlowRetriever:
    def __init__(self, api_key, base_url, dataset_ids, rerank_id=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        # 默认搜所有库
        self.default_dataset_ids = dataset_ids if isinstance(dataset_ids, list) else []
        self.rerank_id = rerank_id

    def retrieve(self, query: str, top_k: int = 2, similarity_threshold: float = 0.3, target_datasets: list = None) -> str:
        """
        检索知识库。
        
        Args:
            query: 用户问题
            target_datasets: [新增] 指定本次要搜的知识库 ID 列表。如果不填，则搜默认列表。
        """
        url = f"{self.base_url}/api/v1/retrieval"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # 逻辑升级：使用传入的目标库，或者回退到默认全库
        search_ids = target_datasets if target_datasets else self.default_dataset_ids
        
        if not search_ids:
            return ""

        payload = {
            "question": query,
            "dataset_ids": search_ids,
            "page": 1,
            "page_size": top_k,
            "similarity_threshold": similarity_threshold
        }
        
        if self.rerank_id:
            payload["rerank_id"] = self.rerank_id

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            if data.get("code") != 0:
                logger.error(f"RAGFlow Error: {data.get('message')}")
                return ""
                
            chunks = data.get("data", {}).get("chunks", [])
            
            # 格式化输出
            results = []
            for chunk in chunks:
                content = chunk.get("content_with_weight", chunk.get("content", ""))
                if content:
                    results.append(content.strip())
            
            return "\n\n".join(results)
            
        except Exception as e:
            logger.error(f"Retrieval failed: {e}")
            return ""