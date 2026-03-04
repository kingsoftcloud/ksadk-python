import os
import click
from ksadk.api import AgentEngineClient

async def auto_release_version(agent_id: str, region: str, deploy_name: str):
    """部署成功后自动创建版本快照"""
    from datetime import datetime
    
    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
    
    client = AgentEngineClient(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )
    
    try:
        description = "部署后自动发布"
        result = await client.release_version(
            agent_id=agent_id,
            tag=None,  # 自动生成
            description=description
        )
        
        click.echo("")
        click.secho(f"✓ 版本快照已创建: {result.get('tag')}", fg="green")
    except Exception as e:
        # 版本创建失败不阻断部署流程
        click.secho(f"⚠ 版本快照创建失败: {e}", fg="yellow")
    finally:
        await client.close()


async def auto_rollback_to_previous(agent_id: str, region: str):
    """部署失败时自动回滚到上一版本"""
    
    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
    
    client = AgentEngineClient(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )
    
    try:
        # 获取版本列表，找到最近的历史版本
        versions_result = await client.list_versions(agent_id, page=1, size=5)
        versions = versions_result.get("versions", [])
        
        # 找到需要回滚的目标版本 (统一 snake_case)
        target_version = None
        for v in versions:
            if (v.get("status") or "").lower() == "current":
                target_version = v
                break
        
        # 如果没有 Current，则找最新的 Historical
        if not target_version:
            for v in versions:
                if (v.get("status") or "").lower() == "historical":
                    target_version = v
                    break
        
        if not target_version:
            click.secho("⚠ 无可用稳定版本，跳过自动回滚", fg="yellow")
            return
        
        target_tag = target_version.get("tag")
        click.echo("")
        click.secho(f"⏪ 正在自动回滚到版本: {target_tag}...", fg="yellow")
        
        # 执行回滚
        result = await client.rollback_version(
            agent_id=agent_id,
            target_tag=target_tag,
            ks3_access_key=access_key,
            ks3_secret_key=secret_key
        )
        
        click.secho(f"✓ 已回滚到版本: {target_tag}", fg="green")
        
    except Exception as e:
        click.secho(f"⚠ 自动回滚失败: {e}", fg="red")
    finally:
        await client.close()
