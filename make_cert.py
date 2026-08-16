"""生成本地 HTTPS 证书 —— 安卓安装 PWA 的"安装态"前置。

为什么需要：安卓 Chrome 只在**安全上下文**（HTTPS / localhost）下才允许安装 PWA
（beforeinstallprompt 不触发）。局域网 IP http://<你的IP>:8000 不是安全上下文，
所以必须给控制层套一层 HTTPS，并在手机上信任一个本地根证书。

用法（在项目目录下）：
    python make_cert.py [--ip <你的局域网IP>] [--force]
    不指定 --ip 时自动探测本机局域网 IP。

产物（certs/ 目录）：
    ca.crt / ca.key          本地根证书（手机信任这一个即可，有效期 10 年）
                              ⚠ ca.key 是 CA 私钥，不要外传/提交
    server.crt / server.key  服务器证书（SAN 含局域网 IP + 127.0.0.1 + localhost，
                              有效期 397 天，符合 Chrome 叶子证书时长限制）

三步走（详细步骤见 docs/安卓安装态.md）：
  1. 先 HTTP 跑控制层（run.bat，此时还没证书）
  2. 手机浏览器打开 http://<IP>:8000/ca.crt 下载并安装根证书
     （安装会要求先设好锁屏 PIN/图案，属安卓正常安全提示）
  3. 重启 run.bat → 自动走 HTTPS → 手机开 https://<IP>:8000 →
     Chrome 菜单"添加到主屏幕 / 安装应用"即得可离线安装的 PWA

更换局域网 IP 时重跑：python make_cert.py --ip <新IP> --force
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BASE_DIR = Path(__file__).resolve().parent
CERT_DIR = BASE_DIR / "certs"


def _detect_lan_ip() -> str:
    """自动探测本机局域网 IPv4（连一个公网 UDP 套接字，取本机出口地址）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


DEFAULT_IP = _detect_lan_ip()          # 局域网 IP：默认自动探测，可 --ip 覆盖
CA_VALID_DAYS = 3650                  # 根证书：10 年（手机只需装一次）
SERVER_VALID_DAYS = 397               # 服务器证书：Chrome 要求叶子证书 ≤398 天
KEY_SIZE = 2048


def _pem(cert_or_key, is_private: bool = False) -> bytes:
    enc = serialization.Encoding.PEM
    if is_private:
        return cert_or_key.private_bytes(
            enc, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
    return cert_or_key.public_bytes(enc)


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    print(f"  已生成 {path.name}")


def _make_ca(now: dt.datetime) -> tuple:
    print("[1/2] 生成本地根证书 (ComfyUI Remote Local CA)...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ComfyUI Remote Local CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ComfyUI Remote (local only)"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=True, crl_sign=True,
                          encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_server(now: dt.datetime, ca_key, ca_cert, ip_str: str) -> tuple:
    print(f"[2/2] 生成服务器证书（SAN 含 {ip_str} / 127.0.0.1 / localhost）...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    ips = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address(ip_str)]
    san = x509.SubjectAlternativeName(
        [x509.IPAddress(ip) for ip in ips] + [x509.DNSName("localhost")])
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"ComfyUI Remote @ {ip_str}"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=SERVER_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_encipherment=True,
                          content_commitment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def main() -> int:
    ap = argparse.ArgumentParser(description="生成局域网 HTTPS 证书（安卓安装 PWA 用）")
    ap.add_argument("--ip", default=DEFAULT_IP,
                    help=f"控制层局域网 IP（默认 {DEFAULT_IP}，与 run.bat 的 NO_PROXY 一致）")
    ap.add_argument("--force", action="store_true", help="已存在证书时强制重新生成")
    args = ap.parse_args()
    ip_str = args.ip
    try:
        ipaddress.ip_address(ip_str)
    except ValueError:
        print(f"错误：'{ip_str}' 不是合法的 IP 地址", file=sys.stderr)
        return 2

    CERT_DIR.mkdir(exist_ok=True)
    server_crt, server_key = CERT_DIR / "server.crt", CERT_DIR / "server.key"
    ca_crt, ca_key = CERT_DIR / "ca.crt", CERT_DIR / "ca.key"
    if not args.force and (server_crt.exists() or ca_crt.exists()):
        print("证书已存在。重新生成会覆盖（手机上要重装根证书）——")
        print("  用 --force 强制重建，或直接删除 certs\\ 目录回到纯 HTTP 模式。")
        print("  若只是证书过期，建议重跑：python make_cert.py --ip %s --force" % ip_str)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    ca_key_obj, ca_cert = _make_ca(now)
    _write(ca_crt, _pem(ca_cert))
    _write(ca_key, _pem(ca_key_obj, is_private=True))

    srv_key_obj, srv_cert = _make_server(now, ca_key_obj, ca_cert, ip_str)
    _write(server_crt, _pem(srv_cert))
    _write(server_key, _pem(srv_key_obj, is_private=True))

    print()
    print("完成。接下来：")
    print(f"  1. 保持 HTTP 跑着，手机浏览器打开 http://{ip_str}:8000/ca.crt 下载根证书")
    print("  2. 下载完点通知栏/文件管理器里的 ca.crt → 选『CA 证书』安装")
    print("     （若提示先设锁屏 PIN/图案，照做即可——安卓装证书必须的）")
    print("  3. 重启 run.bat（这次自动走 HTTPS）→ 手机开 https://%s:8000" % ip_str)
    print("  4. Chrome 菜单『添加到主屏幕 / 安装应用』安装 PWA")
    print()
    print(f"服务器证书有效期 {SERVER_VALID_DAYS} 天，到期重跑上面的命令加 --force。")
    print("⚠ ca.key 是根证书私钥，只留在本机，别发给别人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
