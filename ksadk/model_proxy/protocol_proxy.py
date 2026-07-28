"""可独立运行的转换 proxy 入口:读 env → ProxyConfig → uvicorn。

用法:
  KSPMAS_API_KEY=xxx python -m ksadk.model_proxy.protocol_proxy   # 默认 127.0.0.1:8899
"""

import os

import uvicorn

from .config import ProxyConfig
from .server import create_app


def main():
    config = ProxyConfig.from_env()
    port = int(os.environ.get("PORT", "8899"))
    uvicorn.run(create_app(config), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
