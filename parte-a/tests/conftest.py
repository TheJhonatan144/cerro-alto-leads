"""Aislamiento común: los tests no leen el .env real, no dependen del entorno de la shell
y no pueden abrir conexiones fuera de loopback."""
import ipaddress
import socket

import pytest

from app import config

VARIABLES = (
    "DB_PATH", "DUPLICADO_VENTANA_DIAS", "WORKER_ACTIVO", "WORKER_INTERVALO_S",
    "TRABAJO_LEASE_S", "TRABAJO_MAX_INTENTOS",
    "API_PACIENTES_URL", "API_PACIENTES_TOKEN", "PACIENTES_LIMITE_POR_MIN", "PACIENTES_MAX_PAGINAS",
)


class RedExternaBloqueada(OSError):
    """Un test intentó conectarse a un host que no es loopback. Es OSError, como una red
    caída de verdad, para que el código bajo prueba siga su camino real de error."""


def es_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def entorno_aislado(monkeypatch):
    monkeypatch.setattr(config, "rutas_dotenv", lambda: [])
    for nombre in VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
    # Sin hilo trabajador salvo en los tests que lo activan explícitamente.
    monkeypatch.setenv("WORKER_ACTIVO", "0")


@pytest.fixture(autouse=True)
def sin_red_externa(monkeypatch):
    """Bloquea connect y resolución DNS hacia hosts que no sean loopback."""
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def verificar(sock, direccion):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not es_loopback(direccion[0]):
            raise RedExternaBloqueada(f"conexión a red externa bloqueada en tests: {direccion[0]}")

    def connect_vigilado(self, direccion):
        verificar(self, direccion)
        return connect(self, direccion)

    def connect_ex_vigilado(self, direccion):
        verificar(self, direccion)
        return connect_ex(self, direccion)

    def getaddrinfo_vigilado(host, *args, **kwargs):
        if not es_loopback(host):
            raise RedExternaBloqueada(f"resolución DNS externa bloqueada en tests: {host}")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect_vigilado)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex_vigilado)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo_vigilado)
