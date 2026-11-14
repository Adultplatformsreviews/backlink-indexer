from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.com",
}


def normalize_submitted(url: str) -> str:
    url = (url or "").strip().strip("\ufeff").strip('"').strip("'")
    if not url or url.startswith("#"):
        return ""
    if url.lower() in {"url", "urls", "link", "links"}:
        return ""
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}([/:].*)?$", url, re.I):
            url = "https://" + url
        else:
            return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def registrable_host(host: str) -> str:
    host = (host or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches(host: str, target: str, include_subdomains: bool = True) -> bool:
    host = registrable_host(host)
    target = registrable_host(target)
    if not host or not target:
        return False
    if host == target:
        return True
    if include_subdomains and host.endswith("." + target):
        return True
    return False


def same_document(a: str, b: str) -> bool:
    def parts(u: str):
        p = urlparse(u)
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        host = registrable_host(p.hostname or "")
        return host, path, p.query

    try:
        return parts(a) == parts(b)
    except Exception:
        return a.rstrip("/") == b.rstrip("/")


def resolve_url(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    try:
        return urljoin(base, href)
    except Exception:
        return ""


def is_safe_public_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Only http and https URLs are allowed."
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no host."
    if host in BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        return False, "Local or metadata hosts are blocked."
    if host == "169.254.169.254":
        return False, "Link-local metadata address is blocked."
    try:
        if _is_private_host(host):
            return False, "Private or reserved IP addresses are blocked."
    except socket.gaierror:
        # DNS failure is not treated as unsafe; the fetch will report the error.
        return True, ""
    except Exception as exc:
        return False, f"Could not validate host: {exc}"
    return True, ""


def _is_private_host(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in PRIVATE_NETS)
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    for info in infos:
        ip = info[4][0]
        addr = ipaddress.ip_address(ip)
        if any(addr in net for net in PRIVATE_NETS):
            return True
    return False


def origin(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


def robots_url(url: str) -> str:
    return origin(url) + "/robots.txt"
