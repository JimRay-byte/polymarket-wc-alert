"""验证 TLS context 构建逻辑：无证书回退、坏证书不崩溃"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from config import Settings, ServerSettings
from main import _build_ssl_context

# 1. 无证书配置 → 应返回 None（回退明文 ws）
s1 = Settings(server=ServerSettings(host="127.0.0.1", auth_token="a" * 32,
                                    ssl_cert="", ssl_key=""))
ctx1 = _build_ssl_context(s1)
print(f"无证书: ssl_ctx={ctx1}  (None=明文ws)")

# 2. 证书路径错误 → 应返回 None 并打日志（不抛异常）
s2 = Settings(server=ServerSettings(host="127.0.0.1", auth_token="a" * 32,
                                    ssl_cert="/nonexist/cert.pem",
                                    ssl_key="/nonexist/key.pem"))
ctx2 = _build_ssl_context(s2)
print(f"证书路径错误: ssl_ctx={ctx2}  (None=优雅回退)")

assert ctx1 is None, "无证书应返回 None"
assert ctx2 is None, "坏证书应优雅回退 None"
print("=== TLS 代码路径验证通过（无证书/坏证书均不崩溃）===")
