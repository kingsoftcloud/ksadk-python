"""
AgentEngine Server API 客户端

支持 AWS V4 签名认证，用于通过 KOP 网关访问 AgentEngine Server。
"""

import os
import json
import uuid
import logging
from typing import Optional, Dict, Any

import requests

from ksadk.common.auth import AWSV4Auth

logger = logging.getLogger(__name__)


class DryRunExit(Exception):
    """DryRun 模式退出异常"""
    pass


class AgentEngineClient:
    """AgentEngine Server API 客户端
    
    使用 AWS V4 签名认证访问 KOP 网关后的 AgentEngine Server。
    
    凭证来源 (优先级从高到低):
    1. 构造函数参数
    2. 环境变量 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY
    3. 环境变量 KS3_ACCESS_KEY / KS3_SECRET_KEY
    """

    def __init__(
        self, 
        base_url: Optional[str] = None, 
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "cn-beijing-6",
        service: str = "agentengine",
        timeout: float = 60.0,
        dry_run: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = (
            base_url 
            or os.getenv("AGENTENGINE_SERVER_URL") 
            or "http://localhost:8081"
        )
        self.timeout = timeout
        self.region = region
        self.dry_run = dry_run
        self.extra_headers = extra_headers or {}
        
        # AWS V4 签名
        self._auth = AWSV4Auth(
            access_key_id=access_key or "",
            secret_access_key=secret_key or "",
            region=region,
            service=service,
        )
        
        if self._auth.is_enabled:
            logger.debug(f"AgentEngineClient initialized with V4Auth: {self.base_url}")
        else:
            logger.warning("AgentEngineClient: No credentials, signing disabled")
            
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _get_host(self) -> str:
        """提取 Host (用于签名)"""
        host = self.base_url
        if host.startswith("http://"):
            host = host[7:]
        elif host.startswith("https://"):
            host = host[8:]
        return host.split("/")[0].rstrip("/")

    def _build_headers(self, request_id: str = "") -> Dict[str, str]:
        if not request_id:
            request_id = str(uuid.uuid4())
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": self._get_host(),
            "X-Ksc-Request-Id": request_id,
            "X-Ksc-Region": self.region,
        }
        if self.extra_headers:
            headers.update(self.extra_headers)
        return headers

    def _request(
        self, 
        method: str,
        path: str, 
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步 HTTP 请求"""
        headers = self._build_headers()
        full_url = f"{self.base_url}{path}"
        body_str = json.dumps(body, ensure_ascii=False) if body else ""
        
        # DryRun 模式
        if self.dry_run:
            print("=" * 60)
            print(f"Dry Run Mode: {method} {full_url}")
            print("=" * 60)
            print(f"Headers: {json.dumps(headers, indent=2)}")
            if body:
                print(f"Body: {json.dumps(body, indent=2, ensure_ascii=False)}")
            
            # 生成 Curl 命令
            curl_cmd = f"curl -X {method} \"{full_url}\" \\\n"
            for k, v in headers.items():
                curl_cmd += f"  -H \"{k}: {v}\" \\\n"
            if body_str:
                curl_cmd += f"  -d '{body_str}'"
            
            print("\nCurl Command:")
            print(curl_cmd)
            print("=" * 60)
            
            # 抛出异常以中断流程 (CLI 应捕获此异常)
            raise DryRunExit("Dry Run finished.")
            
        logger.debug(f"Request: {method} {full_url}")
        
        session = self._get_session()
        
        response = session.request(
            method=method,
            url=full_url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            auth=self._auth.get_auth(),  # AWS V4 签名
            timeout=self.timeout,
        )
        
        logger.debug(f"Response: {response.status_code}")
        
        response.raise_for_status()
        
        if response.text:
            return response.json()
        return {}
    
    # Async 包装 (保持兼容性)
    async def close(self):
        if self._session:
            self._session.close()
            self._session = None
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ===== Action API (推荐使用) =====
    
    def _action(self, action: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """通用 Action API 调用"""
        body = params or {}
        result = self._request("POST", f"/agentengine/api/v1/{action}", body)
        
        # 检查错误
        if result.get("Error"):
            raise Exception(result["Error"].get("Message", "Unknown error"))
        
        return result.get("Data", result)

    # ===== Agent Actions =====

    async def create_agent(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建 Agent"""
        params = {
            "Name": data.get("name"),
            "Description": data.get("description"),
            "Framework": data.get("framework", "langgraph"),
            "ArtifactType": data.get("artifact_type", "Code"),
            "ArtifactPath": data.get("artifact_path"),
            "Region": data.get("region", "cn-beijing-6"),
            "Cpu": data.get("resources", {}).get("cpu", "2"),
            "Memory": data.get("resources", {}).get("memory", "4Gi"),
            "MinReplicas": data.get("scaling", {}).get("min_replicas", 1),
            "MaxReplicas": data.get("scaling", {}).get("max_replicas", 10),
            "Concurrency": data.get("scaling", {}).get("concurrency", 20),
        }
        if data.get("ks3"):
            params["KS3AccessKey"] = data["ks3"].get("access_key")
            params["KS3SecretKey"] = data["ks3"].get("secret_key")
            params["KS3Region"] = data["ks3"].get("region")
            params["KS3Bucket"] = data["ks3"].get("bucket")
        if data.get("model"):
            params["ModelName"] = data["model"].get("name")
            params["ModelApiBase"] = data["model"].get("api_base")
            params["ModelApiKey"] = data["model"].get("api_key")
        return self._action("CreateAgent", params)

    async def get_agent(self, agent_id: str) -> Dict[str, Any]:
        """获取 Agent 详情"""
        return self._action("GetAgent", {"Id": agent_id})
        
    async def list_agents(self, region: Optional[str] = None) -> Dict[str, Any]:
        """列出 Agents"""
        params = {}
        if region:
            params["Region"] = region
        return self._action("ListAgents", params)

    async def delete_agent(self, agent_id: str) -> bool:
        """删除 Agent"""
        try:
            self._action("DeleteAgent", {"Id": agent_id})
            return True
        except Exception:
            return False

    async def get_agent_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称查询 Agent"""
        try:
            result = self._action("GetAgentByName", {"Name": name})
            return result.get("Agent")
        except Exception:
            return None

    async def update_agent(self, agent_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新 Agent (热更新)"""
        params = {"Id": agent_id}
        if data.get("artifact_path"):
            params["ArtifactPath"] = data["artifact_path"]
        if data.get("description"):
            params["Description"] = data["description"]
        if data.get("resources"):
            params["Cpu"] = data["resources"].get("cpu")
            params["Memory"] = data["resources"].get("memory")
        if data.get("scaling"):
            params["MinReplicas"] = data["scaling"].get("min_replicas")
            params["MaxReplicas"] = data["scaling"].get("max_replicas")
            params["Concurrency"] = data["scaling"].get("concurrency")
        if data.get("ks3"):
            params["KS3AccessKey"] = data["ks3"].get("access_key")
            params["KS3SecretKey"] = data["ks3"].get("secret_key")
            params["KS3Region"] = data["ks3"].get("region")
            params["KS3Bucket"] = data["ks3"].get("bucket")
        return self._action("UpdateAgent", params)

    # ===== Session Actions =====
    
    async def create_session(self, agent_id: str, user_id: Optional[str] = None, expires_hours: int = 24) -> Dict[str, Any]:
        """创建会话"""
        return self._action("CreateSession", {
            "AgentId": agent_id,
            "UserId": user_id,
            "ExpiresHours": expires_hours
        })
    
    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """获取会话详情"""
        return self._action("GetSession", {"Id": session_id})
    
    async def list_sessions(self, agent_id: str, page: int = 1, size: int = 20) -> Dict[str, Any]:
        """列出会话"""
        return self._action("ListSessions", {"AgentId": agent_id, "Page": page, "Size": size})
    
    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        try:
            self._action("DeleteSession", {"Id": session_id})
            return True
        except Exception:
            return False

    # ===== MCP Actions =====
    
    async def create_mcp(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建 MCP"""
        params = {
            "Name": data.get("name"),
            "Description": data.get("description"),
            "ArtifactType": data.get("artifact_type", "Code"),
            "ArtifactPath": data.get("artifact_path"),
            "Region": data.get("region", "cn-beijing-6"),
            "Cpu": data.get("resources", {}).get("cpu", "1"),
            "Memory": data.get("resources", {}).get("memory", "2Gi"),
            "MinReplicas": data.get("scaling", {}).get("min_replicas", 1),
            "MaxReplicas": data.get("scaling", {}).get("max_replicas", 5),
            "Concurrency": data.get("scaling", {}).get("concurrency", 20),
            "EnableAuth": data.get("enable_auth", False),
        }
        if data.get("metadata"):
            params["McpVariable"] = data["metadata"].get("mcp_variable")
            params["Tools"] = data["metadata"].get("tools")
        if data.get("ks3"):
            params["KS3AccessKey"] = data["ks3"].get("access_key")
            params["KS3SecretKey"] = data["ks3"].get("secret_key")
            params["KS3Region"] = data["ks3"].get("region")
            params["KS3Bucket"] = data["ks3"].get("bucket")
        return self._action("CreateMCP", params)
    
    async def get_mcp(self, mcp_id: str) -> Dict[str, Any]:
        """获取 MCP 详情"""
        return self._action("GetMCP", {"Id": mcp_id})
    
    async def list_mcps(self) -> Dict[str, Any]:
        """列出 MCP (注册中心)"""
        result = self._action("ListMCPs", {"Page": 1, "Size": 100})
        # 兼容返回格式
        return {"mcps": result.get("MCPs", []), "total": result.get("Total", 0)}
    
    async def update_mcp(self, mcp_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新 MCP (热更新)"""
        params = {"Id": mcp_id}
        if data.get("artifact_path"):
            params["ArtifactPath"] = data["artifact_path"]
        if data.get("description"):
            params["Description"] = data["description"]
        if data.get("enable_auth") is not None:
            params["EnableAuth"] = data["enable_auth"]
        if data.get("ks3"):
            params["KS3AccessKey"] = data["ks3"].get("access_key")
            params["KS3SecretKey"] = data["ks3"].get("secret_key")
            params["KS3Region"] = data["ks3"].get("region")
            params["KS3Bucket"] = data["ks3"].get("bucket")
        return self._action("UpdateMCP", params)
    
    async def delete_mcp(self, mcp_id: str) -> bool:
        """删除 MCP"""
        try:
            self._action("DeleteMCP", {"Id": mcp_id})
            return True
        except Exception:
            return False
    
    async def get_mcp_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称查询 MCP"""
        # 使用 ListMCPs 然后过滤 (Action API 暂不支持 by-name)
        try:
            result = await self.list_mcps()
            for mcp in result.get("mcps", []):
                if mcp.get("name") == name:
                    return mcp
            return None
        except Exception:
            return None

    # ===== Upload Actions =====
    
    async def get_presigned_url(self, filename: str) -> Dict[str, Any]:
        """获取 KS3 预签名上传 URL"""
        result = self._action("GetPresignedUrl", {"Filename": filename})
        # 兼容返回格式
        return {
            "presigned_url": result.get("PresignedUrl"),
            "object_key": result.get("ObjectKey"),
            "bucket": result.get("Bucket"),
            "expires_in": result.get("ExpiresIn")
        }

    # ===== Chat Actions =====
    
    async def chat(self, agent_id: str, message: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """调用 Agent"""
        params = {
            "AgentId": agent_id,
            "Messages": [{"role": "user", "content": message}],
            "Stream": False
        }
        if session_id:
            params["SessionId"] = session_id
        return self._action("InvokeAgent", params)

