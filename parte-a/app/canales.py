"""Canales de aviso (D18): archivo, Telegram, n8n y compuesto (varios a la vez).

Interfaz: enviar(texto, registro, presupuesto=None) -> None, que lanza
ErrorCanal("temporal"|"permanente", código). Ningún canal registra, guarda en excepciones ni
muestra en su repr el token, la URL del webhook o el secreto: los errores de httpx se
convierten con "from None" y los loggers httpx/httpcore quedan en WARNING (app.pacientes).
"""
import json
import logging
import os
import re
from pathlib import Path

import httpx

from app import config, db
from app.pacientes import ApiNoDisponible, Presupuesto, leer_acotado  # silencia httpx/httpcore

log = logging.getLogger("leads.canales")

PRESUPUESTO_S = 10.0
TIMEOUT_CONEXION_S = 3.0
MAX_RESPUESTA_BYTES = 64 * 1024
MAX_TEXTO_TELEGRAM = 4000
_CHAT_NUMERICO = re.compile(r"-?\d{1,20}")


class ErrorCanal(Exception):
    """Fallo al enviar. tipo: 'temporal' (se reintenta) o 'permanente' (fallido directo).
    codigo: texto corto sin datos personales ni secretos."""

    def __init__(self, tipo: str, codigo: str):
        if tipo not in ("temporal", "permanente"):
            raise ValueError("tipo de ErrorCanal inválido")
        super().__init__(codigo)
        self.tipo, self.codigo = tipo, codigo


def _cliente_http(transport=None) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(PRESUPUESTO_S, connect=TIMEOUT_CONEXION_S),
                        follow_redirects=False, trust_env=False, transport=transport)


def _leer_hasta(r: httpx.Response, maximo: int) -> None:
    """Consume como mucho 'maximo' bytes de la respuesta (n8n: no se usa el cuerpo)."""
    leidos = 0
    try:
        for trozo in r.iter_bytes():
            leidos += len(trozo)
            if leidos >= maximo:
                break
    except httpx.HTTPError:
        pass  # el estado 2xx ya llegó; el cuerpo no importa


def _post(http: httpx.Client, url: str, cuerpo: dict, cabeceras: dict, presupuesto: Presupuesto | None,
          leer_json: bool):
    """(estado, datos JSON o None). Los fallos de red/tiempo son ErrorCanal temporal."""
    presupuesto = presupuesto or Presupuesto(PRESUPUESTO_S)
    try:
        timeout = presupuesto.timeout()
        with http.stream("POST", url, json=cuerpo, headers=cabeceras, timeout=timeout) as r:
            estado = r.status_code
            if 200 <= estado < 300:
                if not leer_json:
                    _leer_hasta(r, MAX_RESPUESTA_BYTES)
                    return estado, None
                crudo = leer_acotado(r, presupuesto, MAX_RESPUESTA_BYTES)
                try:
                    return estado, json.loads(crudo)
                except (ValueError, RecursionError):
                    raise ErrorCanal("temporal", "respuesta_ilegible") from None
            return estado, None
    # "from None": el mensaje de httpx podría incluir la URL (con el token o la ruta secreta).
    except httpx.TimeoutException:
        raise ErrorCanal("temporal", "timeout") from None
    except httpx.HTTPError:
        raise ErrorCanal("temporal", "error_de_red") from None
    except ApiNoDisponible as e:
        raise ErrorCanal("temporal", e.codigo) from None


class CanalArchivo:
    """Una línea JSON por envío. Contiene datos personales: permisos 0600 y fuera del control
    de versiones (data/ está en .gitignore)."""
    nombre = "archivo"

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)

    def __repr__(self) -> str:
        return "<CanalArchivo>"

    def enviar(self, texto: str, registro, presupuesto: Presupuesto | None = None) -> None:
        d = dict(registro)
        if "codigo" in d:  # incidencia del canal de errores
            fila = {"incidencia_id": d["id"], "codigo": d["codigo"], "ocurrencias": d.get("ocurrencias")}
        else:
            fila = {"aviso_id": d["id"], "evento_id": d["evento_id"], "tipo": d["tipo"]}
        linea = json.dumps({**fila, "enviado_at": db.ahora_utc(), "texto": texto}, ensure_ascii=False) + "\n"
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.ruta, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)  # también si el archivo ya existía con otros permisos
                os.write(fd, linea.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            raise ErrorCanal("temporal", "archivo_no_escribible") from None


class CanalTelegram:
    """sendMessage de la Bot API, sin parse_mode (texto plano). El token va en la URL."""
    nombre = "telegram"

    def __init__(self, token: str, chat_id: str, transport=None):
        self._token = token
        self._chat = int(chat_id) if _CHAT_NUMERICO.fullmatch(chat_id) else chat_id
        self._http = _cliente_http(transport)

    def __repr__(self) -> str:  # nunca el token ni el chat
        return "<CanalTelegram>"

    def enviar(self, texto: str, registro, presupuesto: Presupuesto | None = None) -> None:
        if len(texto) > MAX_TEXTO_TELEGRAM:
            texto = texto[:MAX_TEXTO_TELEGRAM - 1] + "…"
        cuerpo = {"chat_id": self._chat, "text": texto, "disable_web_page_preview": True}
        estado, datos = _post(self._http, f"https://api.telegram.org/bot{self._token}/sendMessage",
                              cuerpo, {}, presupuesto, leer_json=True)
        if estado == 200:
            if isinstance(datos, dict) and datos.get("ok") is True:
                return
            raise ErrorCanal("permanente", "telegram_no_ok")
        if estado in (408, 429) or estado >= 500:
            raise ErrorCanal("temporal", f"http_{estado}")
        if 300 <= estado < 500:
            raise ErrorCanal("permanente", f"http_{estado}")  # incluye redirecciones no seguidas
        raise ErrorCanal("temporal", f"http_{estado}")


class CanalN8n:
    """POST JSON a un webhook de n8n. formato 'aviso' o 'error' (el contrato está en
    parte-a/n8n/CONTRATO.md)."""
    nombre = "n8n"

    def __init__(self, url: str, secreto: str | None = None, transport=None, formato: str = "aviso"):
        self._url, self._secreto, self._formato = url, secreto, formato
        self._http = _cliente_http(transport)

    def __repr__(self) -> str:  # nunca la URL ni el secreto
        return "<CanalN8n>"

    def enviar(self, texto: str, registro, presupuesto: Presupuesto | None = None) -> None:
        d = dict(registro)
        if self._formato == "error":
            cuerpo = cuerpo_n8n_error(d)
        else:
            cuerpo = cuerpo_n8n_aviso(d, texto)
        cabeceras = {"X-Webhook-Secret": self._secreto} if self._secreto else {}
        estado, _ = _post(self._http, self._url, cuerpo, cabeceras, presupuesto, leer_json=False)
        if 200 <= estado < 300:
            return
        if estado in (404, 408, 429) or estado >= 500:  # un flujo inactivo devuelve 404: recuperable
            raise ErrorCanal("temporal", f"http_{estado}")
        raise ErrorCanal("permanente", f"http_{estado}")


def cuerpo_n8n_aviso(aviso: dict, texto: str) -> dict:
    """Contrato del webhook de avisos de n8n."""
    return {"tipo": aviso["tipo"], "evento_id": aviso["evento_id"], "texto": texto,
            "creado_at": aviso["creado_at"]}


def cuerpo_n8n_error(incidencia: dict) -> dict:
    """Contrato del webhook de errores de n8n."""
    return {"codigo": incidencia["codigo"], "detalle": incidencia.get("detalle"),
            "ocurrencias": incidencia.get("ocurrencias"), "ts": incidencia.get("creado_at")}


class CanalCompuesto:
    """Envía a todos los canales en orden. Es 'enviado' si al menos uno tuvo éxito; si todos
    fallan, el error es permanente solo si todos fueron permanentes. Semántica "al menos una
    vez" por canal: un reintento vuelve a enviar a todos."""

    def __init__(self, canales: list):
        self.canales = list(canales)
        self.nombre = ",".join(c.nombre for c in self.canales)

    def __repr__(self) -> str:
        return f"<CanalCompuesto {self.nombre}>"

    def enviar(self, texto: str, registro, presupuesto: Presupuesto | None = None) -> None:
        exitos, fallos = [], []
        for canal in self.canales:
            try:
                canal.enviar(texto, registro, presupuesto)
                exitos.append(canal.nombre)
            except ErrorCanal as e:
                fallos.append((canal.nombre, e))
            except Exception as e:  # solo el tipo
                fallos.append((canal.nombre, ErrorCanal("temporal", type(e).__name__)))
        if exitos:
            if fallos:
                log.warning("éxito parcial: ok=%s fallo=%s", ",".join(exitos),
                            ",".join(f"{n}:{e.tipo}" for n, e in fallos))
            return
        tipo = "permanente" if all(e.tipo == "permanente" for _, e in fallos) else "temporal"
        raise ErrorCanal(tipo, "+".join(f"{n}:{e.codigo}" for n, e in fallos))


def crear_canal(cfg: config.Config, destino: str = "avisos") -> CanalCompuesto:
    """Canal (compuesto) de 'avisos' o de 'errores' según la configuración."""
    nombres = cfg.avisos_canales if destino == "avisos" else cfg.errores_canales
    archivo = cfg.avisos_archivo if destino == "avisos" else cfg.errores_archivo
    url_n8n = cfg.n8n_avisos_url if destino == "avisos" else cfg.n8n_errores_url
    fabricas = {
        "archivo": lambda: CanalArchivo(archivo),
        "telegram": lambda: CanalTelegram(cfg.telegram_bot_token, cfg.telegram_chat_id),
        "n8n": lambda: CanalN8n(url_n8n, cfg.n8n_secreto, formato="aviso" if destino == "avisos" else "error"),
    }
    return CanalCompuesto([fabricas[n]() for n in nombres])
