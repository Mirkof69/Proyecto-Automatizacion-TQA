r"""Centro de pruebas automatizadas de Fetal Medical (FastAPI): un sistema web aparte que controla Selenium.

    .venv\Scripts\python -m uvicorn runner_web.app:app --host 127.0.0.1 --port 8090      (o EJECUTAR_PANEL.bat)

Qué hace:
- Ejecuta cualquier suite (o todas a la vez) con un botón: pytest + Selenium WebDriver, Selenium IDE, API y Locust.
- Muestra el avance en vivo: porcentaje, PASS/FAIL, caso en curso, la pantalla del navegador y el registro.
- Graba en video cada prueba de interfaz (utils/grabacion.py) y deja verlas en el detalle de la corrida.
- Calcula las métricas: salud (PASS sobre ejecutadas), por módulo, cobertura del catálogo, defectos y rendimiento.
- Genera los reportes HTML, Word, PDF y ZIP y los envía por Gmail (reporting/correo.py) al terminar o a pedido.
- Repite solo los casos que fallaron en una corrida (retest).

Acceso con usuario y clave (RUNNER_USER / RUNNER_PASSWORD de .env) y sesión firmada en una cookie. Una corrida a
la vez: todas comparten el sistema bajo prueba y sus cuentas.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from utils import config

RAIZ = config.RAIZ
REPORTES = RAIZ / "reports"
AQUI = Path(__file__).resolve().parent
BD = AQUI / "historial.sqlite3"
plantillas = Jinja2Templates(directory=str(AQUI / "plantillas"))


@asynccontextmanager
async def _ciclo_de_vida(_app):
    _al_iniciar()
    yield


app = FastAPI(title="Centro de pruebas · Fetal Medical", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_ciclo_de_vida)

COOKIE = "qa_sesion"
DURACION_SESION = 12 * 3600
firmador = URLSafeTimedSerializer(config.env("RUNNER_SECRET") or secrets.token_hex(32), salt="panel-qa")

BARRIDO = "tests/ui/test_barrido_pantallas.py"
# clave: (nombre, descripción, argumentos de pytest, grupo)
SUITES: dict[str, tuple[str, str, list[str], str]] = {
    "completa": ("Todas las pruebas", "Ejecución masiva: API, interfaz, Selenium IDE, barrido de pantallas y carga "
                 "con Locust.", ["tests"], "masiva"),
    "regresion": ("Regresión", "API, interfaz con WebDriver y Selenium IDE (sin carga).",
                  ["tests/api", "tests/ui", "tests/ide"], "masiva"),
    "humo": ("Humo", "Camino crítico: login, pacientes, historia clínica, ecografía con IA.", ["-m", "smoke"],
             "rapida"),
    "e2e": ("Extremo a extremo (E2E)", "Una atención completa por la interfaz: recepción registra a la paciente, el "
            "médico abre el embarazo, registra el control, analiza la ecografía con IA y revisa la historia.",
            ["-m", "e2e"], "rapida"),
    "ide": ("Selenium IDE", "Los 35 proyectos .side reproducidos con selenium-side-runner.", ["tests/ide"],
            "herramienta"),
    "busqueda": ("Buscadores y filtros", "Los 17 listados del sistema y los 4 tipos de localizador.",
                 ["-m", "busqueda"], "herramienta"),
    "ui": ("Interfaz (WebDriver)", "Pruebas de pantalla con Selenium WebDriver y Page Object Model.",
           ["tests/ui", f"--deselect={BARRIDO}"], "herramienta"),
    "api": ("API", "Permisos, flujo clínico, IA y robustez de la API REST.", ["tests/api"], "herramienta"),
    "barrido": ("Barrido de pantallas", "Abre todas las pantallas con cada uno de los 5 roles.", [BARRIDO],
                "herramienta"),
    "rendimiento": ("Rendimiento", "Carga con Locust: 40 usuarios simultáneos.", ["tests/rendimiento"],
                    "herramienta"),
    "autenticacion": ("Autenticación", "Login, doble factor y sesión.", ["-m", "auth"], "modulo"),
    "roles": ("Roles y permisos", "Qué puede ver y hacer cada rol (UI y API).", ["-m", "roles"], "modulo"),
    "pacientes": ("Pacientes", "Registro, búsqueda y edición de pacientes.", ["-m", "pacientes"], "modulo"),
    "clinico": ("Historia clínica", "Embarazo, controles prenatales, triaje y ecografía.",
                ["-m", "historia_clinica"], "modulo"),
    "ia": ("Ecografía e IA", "Análisis de ecografías con las redes neuronales.", ["-m", "ecografia_ia"], "modulo"),
}

ACTUAL: dict = {"proceso": None, "run_id": "", "suite": "", "estado": "libre", "lineas": [], "inicio": 0.0,
                "total": 0, "hechos": 0, "cuenta": {}, "recientes": deque(maxlen=14), "opciones": {}}
CONTEOS: dict[str, int] = {}
TAREAS: dict[str, str] = {}
_cache_sut: dict = {"t": 0.0, "datos": {}}
_intentos: dict[str, list[float]] = {}


# ---------------------------------------------------------------------- historial (SQLite)
def _bd():
    c = sqlite3.connect(BD)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS corridas (run_id TEXT PRIMARY KEY, suite TEXT, navegador TEXT,
                 inicio TEXT, fin TEXT, estado TEXT, total INT, pasaron INT, fallaron INT, bloqueadas INT,
                 no_ejecutadas INT, correo TEXT, ia TEXT)""")
    columnas = {f["name"] for f in c.execute("PRAGMA table_info(corridas)")}
    for col, tipo in (("grabar", "INT"), ("origen", "TEXT")):
        if col not in columnas:
            c.execute(f"ALTER TABLE corridas ADD COLUMN {col} {tipo}")
    return c


def _guardar(run_id: str, **campos):
    with _bd() as c:
        c.execute("INSERT OR IGNORE INTO corridas (run_id) VALUES (?)", (run_id,))
        for k, v in campos.items():
            c.execute(f"UPDATE corridas SET {k} = ? WHERE run_id = ?", (v, run_id))  # noqa: S608 - claves internas


def _filas_bd() -> dict[str, dict]:
    with _bd() as c:
        return {f["run_id"]: dict(f) for f in c.execute("SELECT * FROM corridas")}


# ---------------------------------------------------------------------- acceso
def _ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def usuario_actual(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return firmador.loads(token, max_age=DURACION_SESION)
    except (BadSignature, SignatureExpired):
        return None


def acceso(request: Request) -> str:
    u = usuario_actual(request)
    if not u:
        raise HTTPException(401, "Sesión vencida: vuelva a ingresar")
    if request.method == "POST" and request.headers.get("x-panel") != "1":   # las acciones solo desde el panel
        raise HTTPException(403, "Solicitud no válida")
    return u


@app.get("/ingresar", response_class=HTMLResponse)
def pantalla_ingreso(request: Request, error: str = ""):
    if usuario_actual(request):
        return RedirectResponse("/", status_code=303)
    return plantillas.TemplateResponse(request, "login.html", {"error": error})


class Credenciales(BaseModel):
    usuario: str
    clave: str


@app.post("/api/ingresar")
def ingresar(datos: Credenciales, request: Request):
    usuario, clave = config.env("RUNNER_USER"), config.env("RUNNER_PASSWORD")
    if not usuario or not clave:
        raise HTTPException(503, "Defina RUNNER_USER y RUNNER_PASSWORD en el archivo .env de la automatización")
    ip, ahora = _ip(request), time.time()
    fallos = [t for t in _intentos.get(ip, []) if ahora - t < 600]
    if len(fallos) >= 8:
        raise HTTPException(429, "Demasiados intentos fallidos. Espere 10 minutos.")
    if not (secrets.compare_digest(datos.usuario.strip().encode(), usuario.encode())
            and secrets.compare_digest(datos.clave.encode(), clave.encode())):
        _intentos[ip] = fallos + [ahora]
        raise HTTPException(401, "Usuario o clave incorrectos")
    _intentos.pop(ip, None)
    r = JSONResponse({"ok": True})
    seguro = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    r.set_cookie(COOKIE, firmador.dumps(usuario), max_age=DURACION_SESION, httponly=True, samesite="lax",
                 secure=seguro)
    return r


@app.post("/api/salir")
def salir():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    u = usuario_actual(request)
    if not u:
        return RedirectResponse("/ingresar", status_code=303)
    return plantillas.TemplateResponse(request, "app.html", {"usuario": u, "sut": config.SUT_URL})


# ---------------------------------------------------------------------- ejecución
PATRON_CASO = re.compile(r"^(\S+::\S+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b")
PATRON_TOTAL = re.compile(r"collected (\d+) items?(?: / (\d+) deselected)?(?: / (\d+) selected)?")
ESTADO_CORTO = {"PASSED": "PASS", "XPASS": "PASS", "FAILED": "FAIL", "ERROR": "FAIL", "SKIPPED": "OMITIDA",
                "XFAIL": "OMITIDA"}


def _leer_linea(linea: str):
    m = PATRON_TOTAL.search(linea)
    if m and not ACTUAL["total"]:
        coleccionados, quitados, elegidos = (int(x) if x else 0 for x in m.groups())
        ACTUAL["total"] = elegidos or (coleccionados - quitados)
        return
    m = PATRON_CASO.match(linea)
    if m:
        estado = ESTADO_CORTO[m.group(2)]
        ACTUAL["hechos"] += 1
        ACTUAL["cuenta"][estado] = ACTUAL["cuenta"].get(estado, 0) + 1
        ACTUAL["recientes"].appendleft({"nodeid": m.group(1), "estado": estado})


def _argumentos(suite: str) -> list[str]:
    if suite.startswith("fallidos:"):
        origen = suite.split(":", 1)[1]
        datos = json.loads((REPORTES / origen / "results.json").read_text(encoding="utf-8"))
        ids = sorted({c["nodeid"] for c in datos["casos"] if c["estado"] in ("FAIL", "BLOCKED")})
        if not ids:
            raise HTTPException(409, "Esa corrida no tiene casos fallidos ni bloqueados")
        return ids
    return SUITES[suite][2]


def _ejecutar(run_id: str, suite: str, args: list[str], op: dict):
    dir_run = REPORTES / run_id
    dir_run.mkdir(parents=True, exist_ok=True)
    entorno = {**os.environ, "QA_RUN_ID": run_id, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "QA_GRABAR": "1" if op["grabar"] else "0"}
    nombre_suite = suite if not suite.startswith("fallidos:") else "retest " + suite.split(":", 1)[1]
    orden = [sys.executable, "-m", "pytest", *args, "--suite", nombre_suite, "--navegador", op["navegador"],
             "--tb=short", "-v"] + (["--visible"] if op["visible"] else [])
    nuevo_grupo = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    def anotar(linea: str):
        linea = linea.rstrip("\n")
        ACTUAL["lineas"].append(linea)
        if len(ACTUAL["lineas"]) > 6000:
            del ACTUAL["lineas"][:1000]
        _leer_linea(linea)
        with open(dir_run / "ejecucion.log", "a", encoding="utf-8") as f:
            f.write(linea + "\n")

    anotar(f"== Corrida {run_id} · {nombre_suite} · {op['navegador']} · {datetime.now():%d/%m/%Y %H:%M:%S}")
    estado = "terminada"
    try:
        p = subprocess.Popen(orden, cwd=str(RAIZ), env=entorno, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", creationflags=nuevo_grupo)
        ACTUAL["proceso"] = p
        for linea in p.stdout:
            anotar(linea)
        p.wait()
        cancelada = ACTUAL["estado"] == "cancelando"
        anotar(f"== pytest terminó con código {p.returncode}{' (cancelada)' if cancelada else ''}")
        if cancelada:   # al cortar el proceso no corre la limpieza de la sesión de pytest: se hace aquí
            estado = "cancelada"
            try:
                sys.path.insert(0, str(RAIZ / "tools"))
                from limpiar_qa import limpiar
                anotar(f"== Limpieza de datos QA tras la cancelación: {limpiar()}")
            except Exception as e:
                anotar(f"== No se pudo limpiar los datos QA: {type(e).__name__}: {e}")
        ACTUAL["estado"] = "reportes"
        if (dir_run / "results.json").exists():
            _post_corrida(run_id, op, anotar)
        else:
            anotar("== La corrida no dejó results.json: no hay reportes")
            estado = estado if estado == "cancelada" else "sin resultados"
    except Exception as e:  # nada debe dejar el panel bloqueado
        anotar(f"== Error del panel: {type(e).__name__}: {e}")
        estado = "error"
    _guardar(run_id, fin=datetime.now().isoformat(timespec="seconds"), estado=estado)
    anotar("== FIN")
    ACTUAL.update(proceso=None, estado="libre")


def _post_corrida(run_id: str, op: dict, anotar):
    from reporting.modelo import cargar
    t = cargar(run_id).totales
    _guardar(run_id, total=t["total"], pasaron=t["PASS"], fallaron=t["FAIL"], bloqueadas=t["BLOCKED"],
             no_ejecutadas=t["NOT RUN"])
    if op["ia"]:
        try:
            from ai import tareas
            r1, r2 = tareas.resumen_ejecutivo(run_id), tareas.analizar_fallos(run_id)
            nota = "hecho" if r1["disponible"] else f"no disponible: {r1['motivo'][:120]}"
            anotar(f"== IA: resumen {nota}; fallos analizados {r2.get('analizados', 0)}")
            _guardar(run_id, ia=nota)
        except Exception as e:
            anotar(f"== IA: error {type(e).__name__}: {e}")
    from reporting import generar
    anotar("== Generando reportes (HTML, Word, PDF y ZIP)…")
    TAREAS[run_id] = "Generando reportes…"
    try:
        s = generar.generar(run_id)
        anotar(f"== Reportes listos: {', '.join(k for k, v in s.items() if v and k != 'run_id')}")
    finally:
        TAREAS.pop(run_id, None)
    if op["correo"]:
        _enviar_correo(run_id, anotar, otros=op.get("correo_otros") or [])


def _enviar_correo(run_id: str, anotar=None, **envio) -> dict:
    from reporting.correo import enviar
    e = enviar(run_id, **envio)
    nota = ("enviado a " + ", ".join(e["para"])) if e["enviado"] else f"no enviado: {e['motivo']}"
    nota = f"{nota} ({datetime.now():%d/%m %H:%M})"
    _guardar(run_id, correo=nota)
    if anotar:
        anotar(f"== Correo: {nota}")
    return {**e, "nota": nota}


class Orden(BaseModel):
    suite: str
    navegador: str = "chrome"
    visible: bool = False
    grabar: bool = True
    correo: bool = False
    correo_otros: list[str] = []
    ia: bool = False


@app.post("/api/ejecutar")
def ejecutar(o: Orden, _: str = Depends(acceso)):
    if o.navegador not in ("chrome", "edge", "firefox"):
        raise HTTPException(400, "Navegador no válido")
    from reporting.correo import validar_direcciones
    try:
        o.correo_otros = validar_direcciones(o.correo_otros)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if o.suite not in SUITES and not re.fullmatch(r"fallidos:\d{8}-\d{6}", o.suite):
        raise HTTPException(400, "Suite no válida")
    if ACTUAL["estado"] != "libre":
        raise HTTPException(409, "Ya hay una corrida en curso: espere a que termine o cancélela")
    args = _argumentos(o.suite)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    op = o.model_dump()
    ACTUAL.update(run_id=run_id, suite=o.suite, lineas=[], estado="en curso", inicio=time.time(), total=0, hechos=0,
                  cuenta={}, recientes=deque(maxlen=14), opciones=op)
    _guardar(run_id, suite=o.suite, navegador=o.navegador, inicio=datetime.now().isoformat(timespec="seconds"),
             estado="en curso", grabar=int(o.grabar), origen="panel")
    threading.Thread(target=_ejecutar, args=(run_id, o.suite, args, op), daemon=True).start()
    return {"run_id": run_id}


@app.post("/api/cancelar")
def cancelar(_: str = Depends(acceso)):
    p = ACTUAL["proceso"]
    if p is None or p.poll() is not None:
        raise HTTPException(409, "No hay una corrida en curso")
    ACTUAL["estado"] = "cancelando"
    if sys.platform == "win32":   # pytest y sus navegadores: se cierra el árbol completo
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.terminate()
    return {"ok": True}


@app.get("/api/en-curso")
def en_curso(desde: int = 0, _: str = Depends(acceso)):
    lineas = ACTUAL["lineas"]
    desde = max(0, min(desde, len(lineas)))
    caso = {}
    if ACTUAL["run_id"] and ACTUAL["estado"] != "libre":
        f = REPORTES / ACTUAL["run_id"] / "actual.json"
        try:
            caso = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except (ValueError, OSError):
            caso = {}
    vivo = REPORTES / ACTUAL["run_id"] / "en_vivo.jpg" if ACTUAL["run_id"] else None
    ultima = _resumen_terminada(ACTUAL["run_id"]) if ACTUAL["estado"] == "libre" and ACTUAL["run_id"] else None
    return {"estado": ACTUAL["estado"], "run_id": ACTUAL["run_id"], "suite": ACTUAL["suite"], "ultima": ultima,
            "nombre_suite": _nombre_suite(ACTUAL["suite"]), "total": ACTUAL["total"], "hechos": ACTUAL["hechos"],
            "cuenta": ACTUAL["cuenta"], "recientes": list(ACTUAL["recientes"]), "caso": caso,
            "transcurrido": round(time.time() - ACTUAL["inicio"]) if ACTUAL["inicio"] else 0,
            "opciones": ACTUAL["opciones"], "lineas": lineas[desde:], "siguiente": len(lineas),
            "vivo": bool(vivo and vivo.exists() and ACTUAL["estado"] == "en curso"
                         and time.time() - vivo.stat().st_mtime < 20)}


_cache_terminada: dict = {}


def _resumen_terminada(run_id: str) -> dict | None:
    """Lo que muestra la tarjeta «Corrida terminada» de la pantalla Ejecutar: salud, videos, reportes y correo."""
    d = REPORTES / run_id
    if not (d / "results.json").is_file():
        return None
    marca = (d / "results.json").stat().st_mtime, (d / "reporte.pdf").is_file(), _filas_bd().get(run_id, {}).get("correo")
    if _cache_terminada.get(run_id, {}).get("marca") != marca:
        from reporting.modelo import cargar
        r = cargar(run_id)
        _cache_terminada[run_id] = {"marca": marca, "datos": {
            "totales": r.totales, "tasa": r.tasa_exito, "duracion": r.duracion_legible,
            "videos": sum(len(c.get("videos") or []) for c in r.casos),
            "con_video": sum(bool(c.get("videos")) for c in r.casos),
            "pdf": (d / "reporte.pdf").is_file(), "html": (d / "reporte.html").is_file(),
            "correo": marca[2] or "", "por_tipo": r.por_tipo}}
    return _cache_terminada[run_id]["datos"]


@app.get("/api/en-vivo.jpg")
def en_vivo(_: str = Depends(acceso)):
    f = REPORTES / ACTUAL["run_id"] / "en_vivo.jpg" if ACTUAL["run_id"] else None
    if not f or not f.exists():
        raise HTTPException(404, "Sin imagen")
    try:
        datos = f.read_bytes()
    except OSError:
        raise HTTPException(404, "Sin imagen")
    from fastapi.responses import Response
    return Response(datos, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------- consultas
def _nombre_suite(clave: str | None) -> str:
    if not clave:
        return "—"
    if clave.startswith("fallidos:") or clave.startswith("retest "):
        return "Retest de " + re.split(r"[: ]", clave, maxsplit=1)[1]
    if clave in SUITES:
        return SUITES[clave][0]
    if clave.startswith("retest"):
        return "Retest de fallidos"
    if "tests/" in clave or "tests\\" in clave:   # corrida lanzada a mano con rutas de pytest
        partes = [Path(x.split("::")[0]).stem.removeprefix("test_").replace("_", " ") for x in clave.split()]
        return "Selección: " + ", ".join(dict.fromkeys(x for x in partes if x)) if partes else clave
    return clave.capitalize()


def _tasa(p, f) -> float | None:
    p, f = p or 0, f or 0
    return round(100 * p / (p + f), 1) if (p + f) else None


def _corridas(limite: int = 80) -> list[dict]:
    from reporting.modelo import corridas
    filas = _filas_bd()
    salida = {}
    for c in corridas(limite=limite + 5):
        if not re.fullmatch(r"\d{8}-\d{6}", c["run_id"]):   # carpetas internas (_conteo) o de prueba
            continue
        b = filas.get(c["run_id"], {})
        salida[c["run_id"]] = {
            "run_id": c["run_id"], "suite": b.get("suite") or c["suite"], "nombre": _nombre_suite(b.get("suite") or c["suite"]),
            "navegador": b.get("navegador") or "chrome", "inicio": c["inicio"], "duracion_s": c["duracion_s"],
            "estado": b.get("estado") or "terminada", "total": c["total"], "PASS": c["PASS"], "FAIL": c["FAIL"],
            "BLOCKED": c["BLOCKED"], "NOT RUN": c["NOT RUN"], "tasa": _tasa(c["PASS"], c["FAIL"]),
            "correo": b.get("correo") or "", "origen": b.get("origen") or "consola"}
    for run_id, b in filas.items():   # corridas del panel sin results.json (canceladas, en curso)
        if run_id not in salida and b.get("inicio"):
            salida[run_id] = {"run_id": run_id, "suite": b.get("suite"), "nombre": _nombre_suite(b.get("suite")),
                              "navegador": b.get("navegador"), "inicio": b.get("inicio"), "duracion_s": None,
                              "estado": b.get("estado"), "total": None, "PASS": None, "FAIL": None, "BLOCKED": None,
                              "NOT RUN": None, "tasa": None, "correo": b.get("correo") or "", "origen": "panel"}
    return sorted(salida.values(), key=lambda x: x["run_id"], reverse=True)[:limite]


def _estado_sut() -> dict:
    if time.time() - _cache_sut["t"] < 15:
        return _cache_sut["datos"]
    datos = {}
    for clave, url in (("frontend", config.SUT_URL + "/login"), ("api", config.SUT_API + "/health/")):
        try:
            r = requests.get(url, timeout=3)
            datos[clave] = r.status_code < 500
        except requests.RequestException:
            datos[clave] = False
    _cache_sut.update(t=time.time(), datos=datos)
    return datos


def _cobertura() -> dict:
    f = RAIZ / "docs" / "cobertura.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


@app.get("/api/resumen")
def resumen(_: str = Depends(acceso)):
    from ai import servicio
    from reporting.correo import configurado
    from reporting.modelo import cargar
    lista = _corridas(40)
    con_datos = [c for c in lista if c["total"]]
    ultima = None
    if con_datos:
        r = cargar(con_datos[0]["run_id"])
        ultima = {**con_datos[0], "tasa": r.tasa_exito, "por_modulo": r.por_modulo,
                  "duracion": r.duracion_legible, "clasificacion": r.clasificacion_fallos,
                  "videos": sum(len(c.get("videos") or []) for c in r.casos)}
    cob = _cobertura()
    from data.defectos import DEFECTOS
    return {"ultima": ultima, "tendencia": list(reversed(con_datos[:20])),
            "corridas": len(lista), "cobertura": {k: cob.get(k) for k in (
                "casos_diseñados", "automatizados", "seleccionados_automatizar", "cobertura_automatizacion_pct",
                "cobertura_requisitos_pct", "manuales", "por_herramienta")},
            "defectos": {"total": len(DEFECTOS), "verificados": sum(d["estado"] == "Verificado" for d in DEFECTOS),
                         "abiertos": sum(d["estado"] != "Verificado" for d in DEFECTOS)},
            "sistema": {**_estado_sut(), "correo": configurado(), "ia": servicio.estado(), "sut": config.SUT_URL},
            "estado": ACTUAL["estado"]}


@app.get("/api/suites")
def suites(_: str = Depends(acceso)):
    ultimas = {}
    for c in _corridas(80):
        if c["suite"] in SUITES and c["duracion_s"] and c["suite"] not in ultimas:
            ultimas[c["suite"]] = {"duracion_s": c["duracion_s"], "tasa": c["tasa"], "run_id": c["run_id"]}
    return [{"clave": k, "nombre": n, "descripcion": d, "grupo": g, "pruebas": CONTEOS.get(k),
             "ultima": ultimas.get(k)} for k, (n, d, _, g) in SUITES.items()]


@app.get("/api/corridas")
def lista_corridas(_: str = Depends(acceso)):
    return _corridas(80)


def _dir_corrida(run_id: str) -> Path:
    if not re.fullmatch(r"\d{8}-\d{6}|[\w-]{1,40}", run_id) or run_id.startswith("_"):
        raise HTTPException(404, "No existe")
    d = REPORTES / run_id
    if not (d / "results.json").is_file():
        raise HTTPException(404, "La corrida no tiene resultados")
    return d


@app.get("/api/corrida/{run_id}")
def corrida(run_id: str, _: str = Depends(acceso)):
    from reporting.modelo import cargar
    d = _dir_corrida(run_id)
    r = cargar(run_id)
    fila = _filas_bd().get(run_id, {})
    casos = [{"nodeid": c["nodeid"], "tc": c["tc"], "nombre": c["nombre"], "titulo": c["titulo_tc"],
              "modulo": c["modulo"], "herramienta": c["herramienta"], "prioridad": c["prioridad"],
              "estado": c["estado"], "duracion_s": c["duracion_s"], "error": c["error"],
              "traceback": c["traceback"][-2500:] if c["estado"] == "FAIL" else "",
              "clasificacion": c["clasificacion"], "defectos": c["defectos"],
              "evidencias": [{"archivo": e["archivo"], "descripcion": e.get("descripcion", "")}
                             for e in c["evidencias"] if (d / e["archivo"]).is_file()],
              "videos": [v for v in (c.get("videos") or []) if (d / v).is_file()],
              "tipos": c["tipos"], "consola": c.get("consola", [])[:8]} for c in r.casos]
    archivos = {k: (d / n).is_file() for k, n in (("html", "reporte.html"), ("docx", "reporte.docx"),
                                                   ("pdf", "reporte.pdf"), ("log", "ejecucion.log"))}
    archivos["zip"] = next((z.name for z in d.glob("*.zip")), None)
    return {"run_id": run_id, "suite": r.datos.get("suite"), "nombre": _nombre_suite(fila.get("suite") or r.datos.get("suite")),
            "inicio": r.datos.get("inicio"), "fin": r.datos.get("fin"), "duracion": r.duracion_legible,
            "ambiente": r.datos.get("ambiente", {}), "totales": r.totales, "tasa": r.tasa_exito,
            "por_modulo": r.por_modulo, "por_tipo": r.por_tipo, "clasificacion": r.clasificacion_fallos,
            "rendimiento": r.rendimiento,
            "cobertura": r.cobertura_catalogo, "casos": casos, "archivos": archivos, "correo": fila.get("correo") or "",
            "tarea": TAREAS.get(run_id, ""), "fallidos": sum(c["estado"] in ("FAIL", "BLOCKED") for c in casos),
            "videos": sum(len(c["videos"]) for c in casos)}


class Envio(BaseModel):
    principal: bool = True          # el destinatario principal de .env (MAIL_TO)
    otros: list[str] = []           # correos de terceros (por ejemplo, un Gmail del docente)
    asunto: str | None = None
    mensaje: str | None = None
    adjuntos: list[str] | None = None


@app.get("/api/corrida/{run_id}/correo")
def opciones_correo(run_id: str, _: str = Depends(acceso)):
    """Lo que necesita la ventana «Enviar por correo»: destinatario principal, asunto y mensaje por defecto y los
    adjuntos disponibles con su tamaño."""
    _dir_corrida(run_id)
    from reporting import correo as m
    return m.opciones(run_id)


@app.post("/api/corrida/{run_id}/correo")
def correo(run_id: str, e: Envio | None = None, _: str = Depends(acceso)):
    _dir_corrida(run_id)
    from reporting.correo import configurado, validar_direcciones
    if not configurado():
        raise HTTPException(409, "Falta configurar el correo (SMTP_PASSWORD con la contraseña de aplicación de Gmail)")
    e = e or Envio()
    try:
        otros = validar_direcciones(e.otros)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    if not e.principal and not otros:
        raise HTTPException(400, "Elija al menos un destinatario")
    r = _enviar_correo(run_id, principal=e.principal, otros=otros, asunto=e.asunto, mensaje=e.mensaje,
                       adjuntos=e.adjuntos)
    if not r["enviado"]:
        raise HTTPException(502, r["motivo"])
    return r


@app.post("/api/corrida/{run_id}/reportes")
def regenerar(run_id: str, _: str = Depends(acceso)):
    _dir_corrida(run_id)
    if run_id in TAREAS:
        raise HTTPException(409, "Ya se están generando los reportes de esta corrida")

    def tarea():
        TAREAS[run_id] = "Generando reportes…"
        try:
            from reporting import generar
            generar.generar(run_id)
        finally:
            TAREAS.pop(run_id, None)
    threading.Thread(target=tarea, daemon=True).start()
    return {"ok": True}


@app.get("/api/metricas")
def metricas(_: str = Depends(acceso)):
    from data.defectos import DEFECTOS
    from reporting.modelo import cargar
    cob = _cobertura()
    lista = [c for c in _corridas(80) if c["total"]]
    rendimiento, origen_rend = [], ""
    for c in lista:
        r = cargar(c["run_id"])
        if r.rendimiento:
            rendimiento, origen_rend = r.rendimiento, c["run_id"]
            break
    defectos = [{k: d.get(k, "") for k in ("id", "titulo", "severidad", "prioridad", "estado", "modulo", "caso",
                                            "commit", "fecha_correccion")} for d in DEFECTOS]
    ejecutadas = sum((c["PASS"] or 0) + (c["FAIL"] or 0) for c in lista)
    return {"cobertura": cob, "defectos": defectos, "rendimiento": rendimiento, "rendimiento_corrida": origen_rend,
            "historico": {"corridas": len(lista), "pruebas": sum(c["total"] or 0 for c in lista),
                          "ejecutadas": ejecutadas, "pass": sum(c["PASS"] or 0 for c in lista),
                          "tasa": _tasa(sum(c["PASS"] or 0 for c in lista), sum(c["FAIL"] or 0 for c in lista))}}


PERMITIDOS = {"reporte.html", "reporte.docx", "reporte.pdf", "ejecucion.log", "results.json"}


@app.get("/corrida/{run_id}/{ruta:path}")
def archivo(run_id: str, ruta: str, _: str = Depends(acceso)):
    """Reportes, evidencias y videos de una corrida (solo dentro de reports/<run_id>/)."""
    base = (REPORTES / run_id).resolve()
    destino = (base / ruta).resolve()
    if REPORTES.resolve() not in base.parents or base not in destino.parents or not destino.is_file():
        raise HTTPException(404, "No existe")
    if destino.parent == base and destino.name not in PERMITIDOS and destino.suffix != ".zip":
        raise HTTPException(404, "No existe")
    descarga = destino.suffix in (".docx", ".zip")
    tipo = "text/plain; charset=utf-8" if destino.suffix in (".log", ".txt") else None
    return FileResponse(destino, filename=f"{run_id}_{destino.name}" if descarga else None, media_type=tipo)


# ---------------------------------------------------------------------- arranque
def _contar_suites():
    """Cuántas pruebas tiene cada suite (pytest --collect-only), para mostrarlo en las tarjetas."""
    for clave, (_, _, args, _) in SUITES.items():
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", *args, "--collect-only", "-q"], cwd=str(RAIZ),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8", "QA_RUN_ID": "_conteo"},
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.search(r"(\d+)(?:/\d+)? tests? (?:collected|selected)", r.stdout)
            if m:
                CONTEOS[clave] = int(m.group(1))
        except (subprocess.SubprocessError, OSError):
            pass
        shutil.rmtree(REPORTES / "_conteo", ignore_errors=True)


def _al_iniciar():
    with _bd() as c:   # una corrida «en curso» de un arranque anterior quedó interrumpida
        c.execute("UPDATE corridas SET estado = 'interrumpida' WHERE estado = 'en curso'")
    threading.Thread(target=_contar_suites, daemon=True).start()
