"""HTTP transport selection, including a small standard-library SOCKS5 client."""
from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import struct
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class Socks5Settings:
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_socks5(value, username=None, password=None):
    """Parse ``[socks5://][user:pass@]host:port`` without exposing it in errors."""
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(
            value if "://" in value else "socks5://" + value
        )
        if parsed.scheme.lower() != "socks5" or not parsed.hostname or not parsed.port:
            raise ValueError
        user = username if username is not None else parsed.username
        secret = password if password is not None else parsed.password
        return Socks5Settings(
            parsed.hostname, parsed.port,
            urllib.parse.unquote(user) if user is not None else None,
            urllib.parse.unquote(secret) if secret is not None else None,
        )
    except (ValueError, TypeError):
        # The input can contain a password, so never repeat it in diagnostics.
        raise ValueError("invalid SOCKS5 setting; expected [user:pass@]host:port") from None


def _read_exact(sock, length):
    data = b""
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise OSError("SOCKS5 proxy closed the connection")
        data += part
    return data


def socks5_connect(settings, destination_host, destination_port, timeout=None):
    """Open a SOCKS5 stream, sending DNS hostnames to the proxy for resolution."""
    sock = socket.create_connection((settings.host, settings.port), timeout)
    try:
        methods = b"\x00"
        if settings.username is not None or settings.password is not None:
            methods += b"\x02"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _read_exact(sock, 2)
        if version != 5 or method == 0xFF:
            raise OSError("SOCKS5 proxy rejected authentication methods")
        if method == 2 and b"\x02" in methods:
            user = (settings.username or "").encode("utf-8")
            password = (settings.password or "").encode("utf-8")
            if len(user) > 255 or len(password) > 255:
                raise ValueError("SOCKS5 credentials are too long")
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if _read_exact(sock, 2) != b"\x01\x00":
                raise OSError("SOCKS5 authentication failed")
        elif method != 0:
            raise OSError("SOCKS5 proxy selected an unsupported authentication method")

        try:
            address = ipaddress.ip_address(destination_host)
        except ValueError:
            encoded = destination_host.encode("idna")
            if len(encoded) > 255:
                raise ValueError("destination hostname is too long")
            target = b"\x03" + bytes([len(encoded)]) + encoded
        else:
            target = (b"\x01" if address.version == 4 else b"\x04") + address.packed
        sock.sendall(b"\x05\x01\x00" + target + struct.pack("!H", destination_port))
        version, status, reserved, atyp = _read_exact(sock, 4)
        if version != 5 or reserved != 0 or status != 0:
            raise OSError(f"SOCKS5 connection failed (status {status})")
        lengths = {1: 4, 4: 16}
        if atyp == 3:
            length = _read_exact(sock, 1)[0]
        elif atyp in lengths:
            length = lengths[atyp]
        else:
            raise OSError("SOCKS5 proxy returned an invalid address")
        _read_exact(sock, length + 2)
        return sock
    except Exception:
        sock.close()
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, socks_settings, **kwargs):
        self._socks_settings = socks_settings
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = socks5_connect(
            self._socks_settings, self.host, self.port, self.timeout
        )


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, socks_settings, **kwargs):
        self._socks_settings = socks_settings
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = socks5_connect(
            self._socks_settings, self.host, self.port, self.timeout
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def http_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _SocksHTTPConnection(
                host, socks_settings=self.settings, **kwargs
            ), request,
        )


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _SocksHTTPSConnection(
                host, socks_settings=self.settings, **kwargs
            ), request,
        )


class Transport:
    """A per-client URL opener; it never changes process-wide socket behavior."""
    def __init__(self, socks5=None):
        self.socks5 = socks5
        self._opener = (
            urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _HTTPHandler(socks5),
                _HTTPSHandler(socks5),
            )
            if socks5 else None
        )

    def open(self, request, timeout=None):
        if self._opener:
            return self._opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)


def transport_from_config(config):
    return Transport(parse_socks5(
        config.socks5, config.socks5_username, config.socks5_password
    ))
