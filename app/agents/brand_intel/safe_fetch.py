"""
Fetching a web page from a link a user typed, safely.

A pasted link is attacker-controlled input to a server-side request. Without care it can reach
Uri's own network: the AWS metadata endpoint (169.254.169.254), a database, an internal service.
So every request here:

  - accepts only http/https on the standard ports, with no username/password in the link;
  - resolves the hostname itself and refuses unless EVERY address is public;
  - connects to the address it checked (the hostname is kept for the Host header and TLS), so a
    DNS answer that changes between the check and the connection can't redirect it inward;
  - follows redirects by hand, re-checking each hop;
  - stops at a size and time limit, and only keeps HTML (or CSS/robots.txt when asked).
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "UriBot/1.0 (+https://urisocial.com/bot; reads your site to set up your Uri Playbook)"
MAX_BYTES = 1_500_000
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_REDIRECTS = 5
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".corp", ".home")


class FetchError(Exception):
    """A reason a page couldn't be read, in words the owner can act on."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message, self.code = message, code


@dataclass
class Page:
    url: str           # final URL after redirects
    status: int
    content_type: str
    text: str
    body: bytes = b""  # the raw bytes, for images


def normalize_url(raw: str) -> str:
    """'magen.ng' -> 'https://magen.ng/'. Raises FetchError for anything that isn't a web address."""
    s = (raw or "").strip()
    if not s:
        raise FetchError("Add your website address, like yourbusiness.com.", "empty")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        s = "https://" + s
    parts = urlsplit(s)
    if parts.scheme.lower() not in ("http", "https"):
        raise FetchError("That isn't a website address. It should look like yourbusiness.com.", "scheme")
    if parts.username or parts.password:
        raise FetchError("Remove the username or password from the link and try again.", "credentials")
    host = (parts.hostname or "").strip(".").lower()
    if not host or "." not in host and not _is_ip(host):
        raise FetchError("That address looks incomplete. It should look like yourbusiness.com.", "host")
    if parts.port not in (None, 80, 443):
        raise FetchError("We can only read websites on the normal web ports.", "port")
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), host if parts.port is None else f"{host}:{parts.port}", path, parts.query, ""))


def site_key(url: str) -> str:
    """The site a URL belongs to, for caching: host without a leading www."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved)


async def resolve_public(host: str) -> str:
    """One public address for `host`, or FetchError if it has none or any address is private."""
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise FetchError("That address isn't a public website.", "private")
    if _is_ip(host):
        if not _public(host):
            raise FetchError("That address isn't a public website.", "private")
        return host
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=5)
    except (socket.gaierror, asyncio.TimeoutError):
        raise FetchError("We couldn't find that website. Check the spelling of the address.", "dns")
    addrs = sorted({info[4][0] for info in infos})
    if not addrs:
        raise FetchError("We couldn't find that website. Check the spelling of the address.", "dns")
    if not all(_public(a) for a in addrs):
        raise FetchError("That address isn't a public website.", "private")
    return addrs[0]


async def fetch(url: str, *, accept: tuple[str, ...] = ("text/html", "application/xhtml"),
                client: Optional[httpx.AsyncClient] = None, max_bytes: int = MAX_BYTES) -> Page:
    """GET `url` under the rules above. Raises FetchError with an owner-readable reason."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        current = normalize_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            host = parts.hostname or ""
            ip = await resolve_public(host)
            target_host = f"[{ip}]" if ":" in ip else ip
            pinned = urlunsplit((parts.scheme, target_host if parts.port is None else f"{target_host}:{parts.port}",
                                 parts.path or "/", parts.query, ""))
            extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
            try:
                req = client.build_request("GET", pinned, headers={"Host": parts.netloc}, extensions=extensions)
                resp = await client.send(req, stream=True)
            except httpx.TimeoutException:
                raise FetchError("Your website took too long to answer.", "timeout")
            except httpx.HTTPError:
                raise FetchError("We couldn't connect to your website.", "connect")
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = normalize_url(urljoin(current, resp.headers["location"]))
                    continue
                if resp.status_code in (401, 403):
                    raise FetchError("Your website doesn't let tools read it.", "blocked")
                if resp.status_code == 404:
                    raise FetchError("That page doesn't exist on your website.", "not_found")
                if resp.status_code >= 400:
                    raise FetchError("Your website returned an error when we tried to read it.", "http_error")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if accept and ctype and not ctype.startswith(accept):
                    raise FetchError("That link isn't a web page.", "not_html")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        break
                encoding = resp.encoding or "utf-8"
                try:
                    text = bytes(body[:max_bytes]).decode(encoding, errors="replace")
                except LookupError:
                    text = bytes(body[:max_bytes]).decode("utf-8", errors="replace")
                return Page(url=current, status=resp.status_code, content_type=ctype, text=text,
                            body=bytes(body[:max_bytes]))
            finally:
                await resp.aclose()
        raise FetchError("Your website redirects too many times.", "redirects")
    finally:
        if own:
            await client.aclose()


async def robots_allows(base_url: str, path: str, client: Optional[httpx.AsyncClient] = None) -> bool:
    """Respect robots.txt for our user agent (or *). A missing or unreadable file allows."""
    parts = urlsplit(base_url)
    try:
        page = await fetch(f"{parts.scheme}://{parts.netloc}/robots.txt", accept=("text/plain", "text/html"),
                           client=client, max_bytes=200_000)
    except FetchError:
        return True
    return robots_permits(page.text, path)


def robots_permits(robots_txt: str, path: str, agent: str = "uribot") -> bool:
    groups: list[tuple[set[str], list[tuple[str, str]]]] = []
    agents: set[str] = set()
    rules: list[tuple[str, str]] = []
    last_was_agent = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not last_was_agent and (agents or rules):
                groups.append((agents, rules))
                agents, rules = set(), []
            agents.add(value.lower())
            last_was_agent = True
        elif field in ("allow", "disallow"):
            rules.append((field, value))
            last_was_agent = False
    if agents or rules:
        groups.append((agents, rules))
    chosen = next((r for a, r in groups if any(agent in x for x in a if x != "*")), None)
    if chosen is None:
        chosen = next((r for a, r in groups if "*" in a), [])
    best_len, allowed = -1, True
    for kind, prefix in chosen:
        if kind == "disallow" and not prefix:
            continue
        if path.startswith(prefix) and len(prefix) > best_len:
            best_len, allowed = len(prefix), kind == "allow"
    return allowed
