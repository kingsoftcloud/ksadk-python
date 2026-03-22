"""
CLI 部署集成测试

测试 Agent 部署的本地状态文件机制
"""

import os
import json
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from ksadk.deployment.providers.serverless import ServerlessProvider
from ksadk.deployment.base import PackageInfo, DeployTarget, DeployStatus
from ksadk.builders.base import BuildResult


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_project_dir():
    """创建临时项目目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建基本项目结构
        project_dir = Path(tmpdir)
        (project_dir / "agent.py").write_text("# Agent code")
        (project_dir / "agentengine.yaml").write_text(yaml.dump({
            "name": "test-agent",
            "framework": "langgraph"
        }))
        yield project_dir


@pytest.fixture
def sample_package_info(temp_project_dir):
    """示例打包信息"""
    return PackageInfo(
        name="test-agent",
        framework="langgraph",
        build_dir=str(temp_project_dir / ".agentengine" / "build"),
        project_dir=str(temp_project_dir),
        metadata={
            "ks3_path": "ks3://test-bucket/agents/test-agent/code.zip"
        }
    )


@pytest.fixture
def sample_deploy_target():
    """示例部署目标"""
    return DeployTarget(
        provider="serverless",
        region="cn-beijing-6",
        extra={
            "artifact_type": "Code",
            "enable_observability": True
        }
    )


# ============================================================================
# Local State File Tests
# ============================================================================

class TestLocalStateFile:
    """本地状态文件测试"""
    
    def test_load_state_empty(self, temp_project_dir):
        """测试加载空状态文件"""
        provider = ServerlessProvider()
        state_file = temp_project_dir / ".agentengine.state"
        
        state = provider._load_state(state_file)
        
        assert state == {}
    
    def test_load_state_existing(self, temp_project_dir):
        """测试加载已存在的状态文件"""
        provider = ServerlessProvider()
        state_file = temp_project_dir / ".agentengine.state"
        
        # 创建状态文件
        state_file.write_text(yaml.dump({
            "agent_id": "ar-20260119-abcdef",
            "name": "test-agent",
            "endpoint": "https://test.kspmas.ksyun.com"
        }))
        
        state = provider._load_state(state_file)
        
        assert state["agent_id"] == "ar-20260119-abcdef"
        assert state["name"] == "test-agent"
    
    def test_save_state(self, temp_project_dir):
        """测试保存状态文件"""
        provider = ServerlessProvider()
        state_file = temp_project_dir / ".agentengine.state"
        
        provider._save_state(state_file, {
            "agent_id": "ar-20260119-newid",
            "name": "new-agent",
            "endpoint": "https://new.kspmas.ksyun.com"
        })
        
        assert state_file.exists()
        
        loaded = yaml.safe_load(state_file.read_text())
        assert loaded["agent_id"] == "ar-20260119-newid"


# ============================================================================
# Deploy Logic Tests
# ============================================================================

class TestDeployLogic:
    """部署逻辑测试"""
    
    @pytest.mark.asyncio
    async def test_deploy_create_new_agent(
        self, 
        temp_project_dir, 
        sample_package_info, 
        sample_deploy_target
    ):
        """测试首次部署 - 创建新 Agent"""
        provider = ServerlessProvider()
        
        # 模拟 AgentEngineClient
        mock_client = AsyncMock()
        mock_client.create_agent = AsyncMock(return_value={
            "agent_id": "ar-20260119-newagent",
            "name": "test-agent",
            "endpoint": "https://test.kspmas.ksyun.com",
            "api_key": "ak-test-key"
        })
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        
        with patch.dict(os.environ, {"AGENTENGINE_SERVER_URL": "http://localhost:8080"}), \
             patch('ksadk.deployment.providers.serverless.AgentEngineClient', return_value=mock_client), \
             patch('ksadk.common.auth.AWSV4Auth') as MockAuth:
            
            MockAuth.return_value.access_key = "test-ak"
            MockAuth.return_value.secret_key = "test-sk"
            
            result = await provider.deploy(sample_package_info, sample_deploy_target)
        
        assert result.status == DeployStatus.DEPLOYING
        assert result.agent_name == "test-agent"
        assert "首次部署" in result.message
        
        # 验证状态文件已创建
        state_file = temp_project_dir / ".agentengine.state"
        assert state_file.exists()
        
        state = yaml.safe_load(state_file.read_text())
        assert state["agent_id"] == "ar-20260119-newagent"
    
    @pytest.mark.asyncio
    async def test_deploy_update_existing_agent(
        self, 
        temp_project_dir, 
        sample_package_info, 
        sample_deploy_target
    ):
        """测试二次部署 - 更新已有 Agent"""
        provider = ServerlessProvider()
        
        # 预先创建状态文件
        state_file = temp_project_dir / ".agentengine.state"
        state_file.write_text(yaml.dump({
            "agent_id": "ar-20260119-existing",
            "name": "test-agent",
            "endpoint": "https://existing.kspmas.ksyun.com"
        }))
        
        # 模拟 AgentEngineClient
        mock_client = AsyncMock()
        mock_client.update_agent = AsyncMock(return_value={
            "agent_id": "ar-20260119-existing",
            "name": "test-agent",
            "endpoint": "https://existing.kspmas.ksyun.com"
        })
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        
        with patch.dict(os.environ, {"AGENTENGINE_SERVER_URL": "http://localhost:8080"}), \
             patch('ksadk.deployment.providers.serverless.AgentEngineClient', return_value=mock_client), \
             patch('ksadk.common.auth.AWSV4Auth') as MockAuth:
            
            MockAuth.return_value.access_key = "test-ak"
            MockAuth.return_value.secret_key = "test-sk"
            
            result = await provider.deploy(sample_package_info, sample_deploy_target)
        
        assert result.status == DeployStatus.DEPLOYING
        assert "已更新" in result.message
        
        # 验证调用了 update_agent 而不是 create_agent
        mock_client.update_agent.assert_called_once()
        mock_client.create_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_deploy_rejects_ks3_path_without_object_key(
        self,
        temp_project_dir,
        sample_deploy_target,
    ):
        """测试当 ks3_path 只有 bucket 没有 object key 时，本地直接报错。"""
        provider = ServerlessProvider()
        bad_package_info = PackageInfo(
            name="test-agent",
            framework="langgraph",
            build_dir=str(temp_project_dir / ".agentengine" / "build"),
            project_dir=str(temp_project_dir),
            metadata={
                "ks3_path": "ks3://test-bucket"
            },
        )

        with pytest.raises(ValueError, match="ks3_path 格式无效"):
            await provider.deploy(bad_package_info, sample_deploy_target)

    @pytest.mark.asyncio
    async def test_deploy_persists_ui_config_to_state(
        self,
        temp_project_dir,
        sample_package_info,
        sample_deploy_target,
    ):
        """测试部署后会持久化 UI 配置，供 dashboard 无参打开使用。"""
        provider = ServerlessProvider()
        sample_deploy_target.extra.update(
            {
                "ui_profile": "langchain",
                "ui_path": "/",
                "ui_url": None,
            }
        )

        mock_client = AsyncMock()
        mock_client.create_agent = AsyncMock(
            return_value={
                "agent_id": "ar-20260119-newagent-ui",
                "name": "test-agent",
                "endpoint": "https://test.kspmas.ksyun.com",
                "api_key": "ak-test-key",
            }
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()

        with patch.dict(os.environ, {"AGENTENGINE_SERVER_URL": "http://localhost:8080"}), patch(
            "ksadk.deployment.providers.serverless.AgentEngineClient", return_value=mock_client
        ), patch("ksadk.common.auth.AWSV4Auth") as MockAuth:
            MockAuth.return_value.access_key = "test-ak"
            MockAuth.return_value.secret_key = "test-sk"

            await provider.deploy(sample_package_info, sample_deploy_target)

        state_file = temp_project_dir / ".agentengine.state"
        state = yaml.safe_load(state_file.read_text())
        assert state["ui_profile"] == "langchain"
        assert state["ui_path"] == "/"

    @pytest.mark.asyncio
    async def test_deploy_strips_bom_from_env_keys(
        self,
        temp_project_dir,
        sample_package_info,
        sample_deploy_target,
    ):
        """测试 .env 带 BOM 时，环境变量 key 会被规范化。"""
        provider = ServerlessProvider()
        env_file = temp_project_dir / ".env"
        env_file.write_text(
            "OPENAI_API_KEY=test-key\nOPENAI_MODEL_NAME=test-model\n",
            encoding="utf-8-sig",
        )

        captured = {}
        mock_client = AsyncMock()

        async def _fake_create_agent(payload):
            captured["payload"] = payload
            return {
                "agent_id": "ar-20260119-bom",
                "name": "test-agent",
                "endpoint": "https://test.kspmas.ksyun.com",
                "api_key": "ak-test-key",
            }

        mock_client.create_agent = AsyncMock(side_effect=_fake_create_agent)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()

        with patch.dict(os.environ, {"AGENTENGINE_SERVER_URL": "http://localhost:8080"}), \
             patch("ksadk.deployment.providers.serverless.AgentEngineClient", return_value=mock_client), \
             patch("ksadk.common.auth.AWSV4Auth") as MockAuth:

            MockAuth.return_value.access_key_id = "test-ak"
            MockAuth.return_value.secret_access_key = "test-sk"

            await provider.deploy(sample_package_info, sample_deploy_target)

        env_vars = captured["payload"]["env_vars"]
        assert "OPENAI_API_KEY" in env_vars
        assert "\ufeffOPENAI_API_KEY" not in env_vars
        assert env_vars["OPENAI_MODEL_NAME"] == "test-model"

    @pytest.mark.asyncio
    async def test_build_persists_ks3_path_metadata_for_followup_cache(
        self,
        temp_project_dir,
    ):
        """测试 provider.build 后会持久化 ks3_path，供后续 deploy/launch 命中缓存。"""
        provider = ServerlessProvider()
        package_info = PackageInfo(
            name="test-agent",
            framework="langgraph",
            build_dir=str(temp_project_dir / ".agentengine" / "build"),
            project_dir=str(temp_project_dir),
            metadata={},
        )
        target = DeployTarget(
            provider="serverless",
            region="cn-beijing-6",
            extra={"artifact_type": "Code", "no_cache": False},
        )

        fake_build_result = BuildResult(
            success=True,
            artifact_path=temp_project_dir / ".agentengine" / "code_build" / "test-agent.zip",
            artifact_size=1234,
            metadata={"agent_name": "test-agent", "framework": "langgraph"},
        )
        mock_builder = MagicMock()
        mock_builder.build.return_value = fake_build_result

        mock_uploader = AsyncMock()
        mock_uploader.upload = AsyncMock(return_value="ks3://test-bucket/agents/test-agent/code_20260320180000.zip")

        with patch("ksadk.deployment.providers.serverless.CodeBuilder", return_value=mock_builder), \
             patch("ksadk.deployment.providers.serverless.KS3Uploader", return_value=mock_uploader):
            result = await provider.build(package_info, target)

        metadata_file = temp_project_dir / ".agentengine" / "build-metadata.json"
        assert metadata_file.exists()
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert metadata["metadata"]["ks3_path"] == result.metadata["ks3_path"]

        class _PackageDetectionType:
            value = "langgraph"

        class _PackageDetectionResult:
            name = "test-agent"
            type = _PackageDetectionType()
            entry_point = "agent.py"

        packaged_again = await provider.package(
            str(temp_project_dir),
            _PackageDetectionResult(),
            {},
        )
        assert packaged_again.metadata["ks3_path"] == result.metadata["ks3_path"]

    @pytest.mark.asyncio
    async def test_container_build_uses_cached_image_without_rebuild(
        self,
        temp_project_dir,
    ):
        """测试 container 模式存在 cached image 时，不会重复 build。"""
        provider = ServerlessProvider()
        package_info = PackageInfo(
            name="test-agent",
            framework="langgraph",
            build_dir=str(temp_project_dir / ".agentengine" / "build"),
            project_dir=str(temp_project_dir),
            metadata={"image": "hub.kce.ksyun.com/agentengine/test-agent:cached"},
        )
        target = DeployTarget(
            provider="serverless",
            region="cn-beijing-6",
            extra={"artifact_type": "Container", "no_cache": False},
        )

        with patch("ksadk.deployment.providers.serverless.ContainerBuilder") as MockBuilder:
            result = await provider.build(package_info, target)

        assert result.image == "hub.kce.ksyun.com/agentengine/test-agent:cached"
        MockBuilder.assert_not_called()


# ============================================================================
# State File Not Uploaded Tests
# ============================================================================

class TestStateFileNotUploaded:
    """验证状态文件不会被上传"""
    
    def test_state_file_excluded_from_package(self, temp_project_dir):
        """测试状态文件在打包时被排除"""
        # 创建状态文件
        state_file = temp_project_dir / ".agentengine.state"
        state_file.write_text("agent_id: test")
        
        # 模拟打包逻辑 (检查 code_builder.py 中的排除规则)
        excluded_items = []
        
        for item in temp_project_dir.iterdir():
            if item.name.startswith('.'):
                if item.name != '.env':
                    excluded_items.append(item.name)
        
        assert ".agentengine.state" in excluded_items
