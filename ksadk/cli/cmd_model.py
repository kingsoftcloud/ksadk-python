"""
agentengine model - 切换模型
"""

import os
import click
import httpx
import questionary
from pathlib import Path
from dotenv import set_key, find_dotenv, load_dotenv
from ksadk.cli.error_utils import print_exception
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


@click.command()
def model():
    """切换默认模型 (修改 .env)

    从 OPENAI_BASE_URL 获取可用模型列表，并更新 .env 中的 OPENAI_MODEL_NAME
    """
    # 智能初始化 (加载 .env + 默认配置，支持自动推导 API Key/Base)
    from ksadk.configs import setup_environment

    setup_environment(Path.cwd())
    print_title("模型切换")

    # 支持两种环境变量名 (OPENAI_BASE_URL 优先, OPENAI_API_BASE 兼容旧版)
    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("OPENAI_API_KEY")
    current_model = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")  # 兼容旧版

    if not api_base:
        print_error("未找到 OPENAI_BASE_URL")
        print_info("请先在 .env 文件中配置 API 地址 (OPENAI_BASE_URL)")
        return

    # 有些兼容接口可能不需要 Key，但通常都需要
    if not api_key:
        # 尝试匿名访问或提示 warning
        pass

    print_kv("正在获取模型列表", api_base, value_style="#58a6ff")

    try:
        # 处理 API Base URL，防止重复添加 /v1
        base_url = api_base.rstrip("/")
        if base_url.endswith("/v1"):
            url = f"{base_url}/models"
        else:
            url = f"{base_url}/v1/models"

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 增加 verify=False 以防自签证书 (和 invoke -k 保持一致比较好，但这里默认开启验证)
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Parse models
        # OpenAI format: {"data": [{"id": "model-id", ...}, ...]}
        # 兼容某些非标准接口可能直接返回 list
        if isinstance(data, list):
            models = [m["id"] if isinstance(m, dict) else m for m in data]
        else:
            models = [m["id"] for m in data.get("data", [])]

        models.sort()

        if not models:
            print_warn("接口返回了空模型列表")
            return

        # Mark current model in the list
        choices = []
        default_choice = None

        for m in models:
            if m == current_model:
                # 使用 kwargs 避免参数顺序错误
                c = questionary.Choice(title=f"{m} (当前)", value=m)
                choices.append(c)
                default_choice = c  # 记录同一个对象引用
            else:
                choices.append(m)

        # 构建选项列表
        # Questionary 默认支持按键搜索
        selected = questionary.select(
            "Select model:",
            choices=choices,
            default=default_choice,
            style=questionary.Style(
                [
                    ("qmark", "fg:green bold"),
                    ("question", "bold"),
                    ("answer", "fg:green"),
                    ("pointer", "fg:cyan bold"),
                    ("highlighted", "fg:cyan bold"),
                ]
            ),
        ).ask()

        if selected:
            if selected == current_model:
                print_success(f"模型未变更 ({selected})")
            else:
                # Update .env
                env_file = find_dotenv(usecwd=True)
                if not env_file:
                    env_file = Path.cwd() / ".env"
                    # 如果不存在则创建? 或者是报错
                    print_warn("未找到 .env 文件，将在当前目录创建")
                    env_file = Path.cwd() / ".env"
                    env_file.touch()

                # set_key 会保留注释和格式
                success, key, value = set_key(env_file, "OPENAI_MODEL_NAME", selected, quote_mode="never")
                if success:
                    print_success(f"已切换模型为: {selected}")
                    print_info(f"已更新 {env_file}")
                else:
                    print_error("更新 .env 失败")

    except Exception as e:
        print_exception("获取模型失败", e)
        if "401" in str(e):
            print_info("提示: 请检查 OPENAI_API_KEY 是否正确")
        elif "404" in str(e):
            print_info("提示: 接口地址可能不正确，请检查 /v1/models 是否存在")
