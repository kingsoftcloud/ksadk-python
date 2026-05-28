"""
通用常量定义
"""

# Serverless Endpoint (public control plane)
DEFAULT_SERVERLESS_ENDPOINT = "https://aicp.api.ksyun.com"

# KS3 Region 映射表
# 用户输入的 region (如 cn-beijing-6) -> (外网endpoint, 内网endpoint, region_code)
KS3_REGION_MAP = {
    # 北京
    "cn-beijing": ("ks3-cn-beijing.ksyuncs.com", "ks3-cn-beijing-internal.ksyuncs.com", "BEIJING"),
    # 上海
    "cn-shanghai": (
        "ks3-cn-shanghai.ksyuncs.com",
        "ks3-cn-shanghai-internal.ksyuncs.com",
        "SHANGHAI",
    ),
    # 广州
    "cn-guangzhou": (
        "ks3-cn-guangzhou.ksyuncs.com",
        "ks3-cn-guangzhou-internal.ksyuncs.com",
        "GUANGZHOU",
    ),
    # 宁波
    "cn-ningbo": ("ks3-cn-ningbo.ksyuncs.com", "ks3-cn-ningbo-internal.ksyuncs.com", "NINGBO"),
    # 青海
    "cn-qinghai": ("ks3-cn-qinghai.ksyuncs.com", "ks3-cn-qinghai-internal.ksyuncs.com", "QINGHAI"),
    # 庆阳
    "cn-qingyang": (
        "ks3-cn-qingyang.ksyuncs.com",
        "ks3-cn-qingyang-internal.ksyuncs.com",
        "QINGYANG",
    ),
    # 香港
    "cn-hongkong": (
        "ks3-cn-hongkong.ksyuncs.com",
        "ks3-cn-hongkong-internal.ksyuncs.com",
        "HONGKONG",
    ),
    # 俄罗斯
    "rus": ("ks3-rus.ksyuncs.com", "ks3-rus-internal.ksyuncs.com", "RUSSIA"),
    # 新加坡
    "sgp": ("ks3-sgp.ksyuncs.com", "ks3-sgp-internal.ksyuncs.com", "SINGAPORE"),
}


def get_ks3_endpoints(region: str) -> tuple[str, str]:
    """获取 KS3 Endpoints (public, internal)

    支持模糊匹配，如 cn-beijing-6 会匹配到 cn-beijing
    """
    info = _get_ks3_info(region)
    return info[0], info[1]


def get_ks3_region_code(region: str) -> str:
    """获取 KS3 Region Code (如 BEIJING)"""
    return _get_ks3_info(region)[2]


def _get_ks3_info(region: str) -> tuple[str, str, str]:
    """获取 KS3 完整信息"""
    # 1. 精确匹配
    if region in KS3_REGION_MAP:
        return KS3_REGION_MAP[region]

    # 2. 前缀匹配 (如 cn-beijing-6 -> cn-beijing)
    # 尝试去掉最后的数字后缀
    parts = region.split("-")
    if len(parts) > 2 and parts[-1].isdigit():
        region_base = "-".join(parts[:-1])
        if region_base in KS3_REGION_MAP:
            return KS3_REGION_MAP[region_base]

    # 3. 遍历匹配
    for prefix, info in KS3_REGION_MAP.items():
        if region.startswith(prefix):
            return info

    # 4. 默认 (北京)
    return KS3_REGION_MAP["cn-beijing"]
