#!/usr/bin/env python3
"""
KS3 上传调试脚本
测试 bucket 创建和文件上传
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env (支持多种位置)
for env_path in [Path(".env"), Path("/tmp/my-agent/.env"), Path.home() / ".env"]:
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✓ 加载 .env: {env_path}")
        break

ak = os.environ.get("KSYUN_ACCESS_KEY")
sk = os.environ.get("KSYUN_SECRET_KEY")

print("AK: 已设置" if ak else "AK: 未设置")
print("SK: 已设置" if sk else "SK: 未设置")

BUCKET_NAME = "agentengine"
REGION = "cn-beijing"
HOST = f"ks3-{REGION}.ksyuncs.com"

print(f"\nHost: {HOST}")
print(f"Bucket: {BUCKET_NAME}")

try:
    from ks3.connection import Connection
    
    conn = Connection(ak, sk, host=HOST)
    print(f"\n✓ 连接成功")
    
    # 列出所有 bucket
    print("\n现有 Buckets:")
    buckets = conn.get_all_buckets()
    for b in buckets:
        print(f"  - {b.name}")
    
    # 检查目标 bucket 是否存在
    bucket_exists = any(b.name == BUCKET_NAME for b in buckets)
    
    if not bucket_exists:
        print(f"\n⚠️  Bucket '{BUCKET_NAME}' 不存在，尝试创建...")
        try:
            new_bucket = conn.create_bucket(BUCKET_NAME)
            print(f"✓ Bucket 创建成功: {new_bucket.name}")
        except Exception as e:
            print(f"✗ 创建失败: {e}")
            # 可能需要指定 location
            print("\n尝试使用 location 参数创建...")
            try:
                new_bucket = conn.create_bucket(BUCKET_NAME, location=REGION.upper())
                print(f"✓ Bucket 创建成功: {new_bucket.name}")
            except Exception as e2:
                print(f"✗ 仍然失败: {e2}")
    else:
        print(f"\n✓ Bucket '{BUCKET_NAME}' 已存在")
    
    # 测试上传
    print("\n测试上传...")
    bucket = conn.get_bucket(BUCKET_NAME)
    test_key = bucket.new_key("test/hello.txt")
    result = test_key.set_contents_from_string("Hello from KsADK!")
    print(f"上传结果: {result}")
    
    if result and result.status == 200:
        print("✓ 测试上传成功!")
        # 读取验证
        content = test_key.get_contents_as_string()
        print(f"读取内容: {content}")
    else:
        print(f"✗ 上传返回非 200: {result}")

except ImportError as e:
    print(f"✗ ks3sdk 未安装: {e}")
except Exception as e:
    print(f"✗ 错误: {type(e).__name__}: {e}")
