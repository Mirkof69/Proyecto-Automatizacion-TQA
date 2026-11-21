"""Grabación en video de cada prueba de interfaz (Selenium WebDriver y Selenium IDE).

Se activa con QA_GRABAR=1 (el panel web lo pone al marcar «Grabar video»). Por cada navegador abierto corre un hilo
que toma una imagen de la página cada QA_GRABAR_INTERVALO segundos (0,5 por defecto) mientras haya una prueba en
curso; al terminar la prueba, ffmpeg arma reports/<run_id>/videos/VID-<TC>-....mp4 y el resultado del caso lo enlaza.

En Chrome y Edge las imágenes se piden por el protocolo DevTools en una conexión propia (debuggerAddress), no por
el canal de WebDriver: así la grabadora no se mete en la cola de comandos de la prueba ni cierra alertas nativas.
En Firefox se usa la captura de WebDriver. La última imagen queda en reports/<run_id>/en_vivo.jpg para la vista en
vivo del panel. Si no hay ffmpeg, la prueba sigue igual y simplemente no deja video.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import traceback
from collections import defaultdict
from contextlib import contextmanager

from utils import config, evidencias

ACTIVA = config.env("QA_GRABAR", "0") == "1"
INTERVALO = float(config.env("QA_GRABAR_INTERVALO", "0.5") or 0.5)
PUERTO_IDE = int(config.env("QA_GRABAR_PUERTO_IDE", "9339") or 9339)
DIR_VIDEOS = evidencias.DIR_RUN / "videos"
EN_VIVO = evidencias.DIR_RUN / "en_vivo.jpg"

_caso: dict = {"clave": None}
_cuadros: dict[tuple[str, int], list[tuple[float, bytes]]] = defaultdict(list)
_bloqueo = threading.Lock()
_pausa = threading.Event()
_grabadoras: dict = {}
_activas: set[int] = set()   # grabadoras de los navegadores que usa la prueba en curso
_ids = itertools.count(1)
_pendientes: list[threading.Thread] = []
_codificadores = threading.Semaphore(2)


def ffmpeg() -> str:
    hallado = config.env("FFMPEG") or shutil.which("ffmpeg")
    if hallado:
        return hallado
    patron = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*",
                          "bin", "ffmpeg.exe")
    candidatos = sorted(glob.glob(patron))
    return candidatos[-1] if candidatos else ""


# ---------------------------------------------------------------------- fuentes de imágenes
class FuenteCDP:
    """Captura por el protocolo DevTools de Chrome/Edge (host:puerto de depuración)."""

    def __init__(self, direccion: str):
        # Chrome informa «localhost»: en Windows eso prueba IPv6 primero y pierde ~2 s por conexión
        self.direccion = direccion.replace("localhost", "127.0.0.1")
        self.ws = None
        self.objetivo = ""
        self.n = 0

    def _pagina(self) -> dict | None:
        with urllib.request.urlopen(f"http://{self.direccion}/json", timeout=1) as r:  # noqa: S310 - solo localhost
            lista = json.load(r)
        paginas = [t for t in lista if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]
        return paginas[0] if paginas else None

    def cuadro(self) -> bytes | None:
        import websocket
        if self.ws is None or self.n % 6 == 0:   # cada ~3 s se revisa si la prueba cambió de pestaña
            pagina = self._pagina()
            if not pagina:
                return None
            if self.ws is None or pagina["id"] != self.objetivo:
                self.cerrar()
                url = pagina["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:")
                self.ws = websocket.create_connection(url, timeout=1, suppress_origin=True)
                self.objetivo = pagina["id"]
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": "Page.captureScreenshot",
                                 "params": {"format": "jpeg", "quality": 62}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                break
        return base64.b64decode(m["result"]["data"]) if "result" in m else None

    def cerrar(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None


class FuenteWebDriver:
    """Firefox: captura de WebDriver convertida a JPEG."""

    def __init__(self, driver):
        self.driver = driver

    def cuadro(self) -> bytes | None:
        from PIL import Image
        png = self.driver.get_screenshot_as_png()
        salida = io.BytesIO()
        Image.open(io.BytesIO(png)).convert("RGB").save(salida, "JPEG", quality=62)
        return salida.getvalue()

    def cerrar(self):
        pass


# ---------------------------------------------------------------------- grabadora
class Grabadora(threading.Thread):
    def __init__(self, fuente):
        super().__init__(daemon=True, name="grabadora")
        self.fuente = fuente
        self.id = next(_ids)
        self.alto = threading.Event()
        self.uso = threading.Lock()   # la fuente la usan el hilo y la captura final del caso
        _grabadoras[self.id] = self

    def capturar(self, clave: str) -> bool:
        with self.uso:
            try:
                jpg = self.fuente.cuadro()
            except Exception:   # navegador cerrándose o todavía sin abrir (Selenium IDE): se reintenta
                jpg = None
                self.fuente.cerrar()
                _anotar_error()
        if jpg and _caso["clave"] == clave:
            with _bloqueo:
                _cuadros[(clave, self.id)].append((time.monotonic(), jpg))
            _en_vivo(jpg)
        return bool(jpg)

    def run(self):
        while not self.alto.is_set():
            inicio = time.monotonic()
            clave = _caso["clave"]
            ritmo = INTERVALO
            if clave and self.id in _activas and not _pausa.is_set() and not self.capturar(clave):
                ritmo = 0.15   # navegador abriéndose o navegando: se reintenta pronto
            # ritmo de muestreo de la grabadora, no una espera de la prueba
            self.alto.wait(max(0.05, ritmo - (time.monotonic() - inicio)))
        with self.uso:
            self.fuente.cerrar()
        _grabadoras.pop(self.id, None)

    def detener(self):
        self.alto.set()
        self.join(timeout=8)


def _anotar_error():
    if config.env("QA_GRABAR_DEBUG") == "1":
        with open(evidencias.DIR_RUN / "grabacion_errores.log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())


def _en_vivo(jpg: bytes):
    try:
        EN_VIVO.parent.mkdir(parents=True, exist_ok=True)
        tmp = EN_VIVO.with_suffix(".tmp")
        tmp.write_bytes(jpg)
        os.replace(tmp, EN_VIVO)
    except OSError:   # el panel puede estar leyendo el archivo justo ahora
        pass


def adjuntar_driver(driver) -> None:
    """Empieza a grabar un navegador de WebDriver; se detiene solo al cerrarlo (driver.quit)."""
    if not ACTIVA:
        return
    caps = driver.capabilities
    direccion = (caps.get("goog:chromeOptions") or caps.get("ms:edgeOptions") or {}).get("debuggerAddress")
    g = Grabadora(FuenteCDP(direccion) if direccion else FuenteWebDriver(driver))
    _activas.add(g.id)   # un navegador recién creado es de la prueba en curso
    g.start()
    driver._grabadora_id = g.id
    cerrar_original = driver.quit

    def quit_con_grabadora():
        g.detener()
        cerrar_original()

    driver.quit = quit_con_grabadora


@contextmanager
def grabar_puerto(puerto: int = PUERTO_IDE):
    """Selenium IDE: graba el Chrome que abre selenium-side-runner con --remote-debugging-port."""
    if not ACTIVA:
        yield
        return
    g = Grabadora(FuenteCDP(f"127.0.0.1:{puerto}"))
    _activas.add(g.id)
    g.start()
    try:
        yield
    finally:
        g.detener()


@contextmanager
def pausa():
    """Suspende la grabación (p. ej. mientras hay una alerta nativa abierta)."""
    _pausa.set()
    try:
        yield
    finally:
        _pausa.clear()


# ---------------------------------------------------------------------- casos y videos
def iniciar_caso(clave: str) -> None:
    _activas.clear()
    _caso["clave"] = clave


def usar(driver) -> None:
    """La prueba en curso usa este navegador (p. ej. el de un rol creado en una prueba anterior)."""
    gid = getattr(driver, "_grabadora_id", None)
    if gid:
        _activas.add(gid)


def cuadro_final(clave: str) -> None:
    """Al terminar el cuerpo de la prueba: imagen de cómo quedó la pantalla (así también las pruebas de menos de
    un segundo dejan video). Se llama antes del cierre de los fixtures, con el navegador todavía abierto."""
    if not ACTIVA or _pausa.is_set() or _caso["clave"] != clave:
        return
    for gid in list(_activas):
        g = _grabadoras.get(gid)
        if g is not None and g.is_alive():
            g.capturar(clave)


def terminar_caso(clave: str, tc: str) -> list[str]:
    """Cierra la grabación del caso y lanza la codificación; devuelve las rutas relativas de sus videos."""
    _caso["clave"] = None
    with _bloqueo:
        grupos = [(k, _cuadros.pop(k)) for k in sorted(list(_cuadros), key=lambda x: x[1]) if k[0] == clave]
    grupos = [(k, c) for k, c in grupos if c]
    ff = ffmpeg()
    if not grupos or not ff:
        return []
    sufijo = hashlib.sha1(clave.encode()).hexdigest()[:6]
    tc_limpio = re.sub(r"[^A-Za-z0-9_-]+", "_", tc)[:30]
    rutas = []
    for i, (_, cuadros) in enumerate(grupos, 1):
        nombre = f"VID-{tc_limpio}-{sufijo}{'-' + str(i) if len(grupos) > 1 else ''}.mp4"
        rutas.append(f"videos/{nombre}")
        hilo = threading.Thread(target=_codificar, args=(ff, cuadros, DIR_VIDEOS / nombre), daemon=True)
        hilo.start()
        _pendientes.append(hilo)
    return rutas


def _codificar(ff: str, cuadros: list[tuple[float, bytes]], destino) -> None:
    with _codificadores:
        destino.parent.mkdir(parents=True, exist_ok=True)
        duracion = cuadros[-1][0] - cuadros[0][0]
        fps = max(0.5, min(10.0, (len(cuadros) - 1) / duracion)) if duracion > 0 else 2.0
        orden = [ff, "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", f"{fps:.3f}",
                 "-i", "-", "-vf", "scale=1280:-2,format=yuv420p,tpad=stop_mode=clone:stop_duration=1.5",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart", str(destino)]
        try:
            p = subprocess.Popen(orden, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _, jpg in cuadros:
                p.stdin.write(jpg)
            p.stdin.close()
            p.wait(timeout=180)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def esperar_videos(maximo_s: float = 240) -> None:
    """Al cerrar la sesión de pytest: espera a que ffmpeg termine los videos pendientes."""
    limite = time.monotonic() + maximo_s
    for h in list(_pendientes):
        h.join(timeout=max(0.1, limite - time.monotonic()))
    try:
        EN_VIVO.unlink(missing_ok=True)
    except OSError:
        pass
