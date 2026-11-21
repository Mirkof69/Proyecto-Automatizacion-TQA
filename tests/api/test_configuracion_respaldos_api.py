"""Configuración del sistema y respaldos de la base por la API, con los cinco roles.

Configuración general, umbrales de alertas y horarios: se editan y se dejan como estaban. Respaldos: crear, listar,
descargar, cargar el descargado desde «otro equipo» y eliminar lo creado por la prueba. Restaurar reemplaza toda la
base, así que solo corre con AUTORIZAR_RESTAURAR=1 (y restaura un respaldo creado segundos antes por la misma prueba).
"""
from __future__ import annotations

import os

import pytest

from conftest import cliente
from utils.api_client import _json

pytestmark = [pytest.mark.api, pytest.mark.reportes]

GENERAL, ALERTAS, HORARIOS = "/reportes/configuracion/general/", "/reportes/configuracion/alertas/", \
    "/reportes/configuracion/horarios/"
RESPALDOS = "/reportes/configuracion/backups/"
OTROS_ROLES = ("medico", "enfermera", "laboratorista", "recepcion")
MARCA_PG_DUMP = b"PostgreSQL database dump"


def _respaldos() -> list[str]:
    return [b["filename"] for b in _json(cliente("administrador").get(RESPALDOS, verificar=True))]


@pytest.mark.roles
@pytest.mark.tc("TC-174")
def test_tc174_configuracion_general_editar_y_revertir():
    """El administrador lee la configuración general, cambia un dato, lo ve guardado y lo deja como estaba."""
    admin = cliente("administrador")
    antes = _json(admin.get(GENERAL, verificar=True))
    campo = next(c for c in ("telefono", "direccion", "email_contacto", "nombre_institucion", "nombre_sistema")
                 if c in antes)
    original = antes[campo] or ""
    prueba = "QA 21/11 " + campo
    r = admin._peticion("POST", GENERAL, verificar=False, data={campo: prueba})
    assert r.status_code == 200, f"No guardó la configuración: HTTP {r.status_code} {r.text[:200]}"   # validación 1
    assert _json(admin.get(GENERAL))[campo] == prueba, "El cambio no quedó guardado"                  # validación 2
    r = admin._peticion("POST", GENERAL, verificar=False, data={campo: original})
    assert r.status_code == 200 and (_json(admin.get(GENERAL))[campo] or "") == original, "No se pudo revertir"


@pytest.mark.roles
@pytest.mark.tc("TC-175")
def test_tc175_configuracion_y_respaldos_solo_administrador():
    """General, horarios y respaldos: 403 para médico, enfermería, laboratorio y recepción; 200 para el administrador."""
    for ruta in (GENERAL, HORARIOS, RESPALDOS):
        assert cliente("administrador").get(ruta).status_code == 200, f"El administrador no abre {ruta}"
        for rol in OTROS_ROLES:
            codigo = cliente(rol).get(ruta).status_code
            assert codigo == 403, f"{rol} obtuvo HTTP {codigo} en {ruta} (se esperaba 403)"


@pytest.mark.roles
@pytest.mark.tc("TC-176")
def test_tc176_alertas_las_leen_todos_y_las_cambian_medico_y_admin():
    """Umbrales de alertas: todos los roles los leen; solo médico y administrador los cambian (se reenvían iguales)."""
    actual = _json(cliente("administrador").get(ALERTAS, verificar=True))
    datos = {k: v for k, v in actual.items() if k not in ("id", "actualizado_por", "fecha_actualizacion",
                                                           "fecha_modificacion", "fecha_creacion")}
    for rol in ("enfermera", "laboratorista", "recepcion"):
        assert cliente(rol).get(ALERTAS).status_code == 200, f"{rol} no puede leer los umbrales"
        codigo = cliente(rol).post(ALERTAS, datos).status_code
        assert codigo == 403, f"{rol} pudo cambiar los umbrales de alertas (HTTP {codigo})"
    for rol in ("medico", "administrador"):
        r = cliente(rol).post(ALERTAS, datos)
        assert r.status_code == 200, f"{rol} no pudo guardar los umbrales: HTTP {r.status_code} {r.text[:200]}"


@pytest.mark.tc("TC-177")
def test_tc177_horarios_de_atencion_se_guardan_sin_cambios():
    """El administrador reenvía los horarios tal como están: HTTP 200 y quedan iguales."""
    admin = cliente("administrador")
    horarios = _json(admin.get(HORARIOS, verificar=True))
    assert isinstance(horarios, list) and horarios, "No hay horarios de atención configurados"
    r = admin._peticion("POST", HORARIOS, verificar=False, json=horarios)
    assert r.status_code == 200, f"No guardó los horarios: HTTP {r.status_code} {r.text[:200]}"
    despues = _json(admin.get(HORARIOS))
    clave = lambda h: (h.get("dia"), h.get("hora_apertura"), h.get("hora_cierre"), h.get("activo"))  # noqa: E731
    assert sorted(map(clave, despues)) == sorted(map(clave, horarios)), "Los horarios cambiaron al reenviarlos"


@pytest.mark.tc("TC-178")
def test_tc178_respaldo_crear_descargar_cargar_y_eliminar(tmp_path):
    """Crea un respaldo, lo descarga (pg_dump real), lo vuelve a cargar como si viniera de otro equipo y borra ambos."""
    admin = cliente("administrador")
    r = admin.post(RESPALDOS + "create/", timeout=600)
    assert r.status_code == 201, f"No creó el respaldo: HTTP {r.status_code} {r.text[:200]}"           # validación 1
    creado = r.json()["filename"]
    cargado = None
    try:
        assert creado in _respaldos(), "El respaldo creado no aparece en la lista"                    # validación 2
        r = admin.get(RESPALDOS + f"{creado}/", timeout=600)
        assert r.status_code == 200 and MARCA_PG_DUMP in r.content[:4096], "La descarga no es un pg_dump"  # validación 3
        archivo = tmp_path / creado
        archivo.write_bytes(r.content)
        with archivo.open("rb") as f:
            r = admin.post(RESPALDOS + "cargar/", archivos={"archivo": (creado, f, "application/sql")}, timeout=600)
        assert r.status_code == 201, f"No cargó el respaldo descargado: HTTP {r.status_code} {r.text[:200]}"  # validación 4
        cargado = r.json()["filename"]
        assert r.json()["size"] == archivo.stat().st_size, "El respaldo cargado no tiene el mismo tamaño"
        assert cargado in _respaldos(), "El respaldo cargado no aparece en la lista"                  # validación 5
        r = admin.post(RESPALDOS + "cargar/", archivos={"archivo": ("no_es_respaldo.sql", b"DROP TABLE x;", "text/plain")})
        assert r.status_code == 400, "Aceptó como respaldo un archivo que no es un pg_dump"            # validación 6
    finally:
        for nombre in (creado, cargado):
            if nombre:
                admin.delete(RESPALDOS + f"{nombre}/delete/")
    restantes = _respaldos()
    assert creado not in restantes and (cargado is None or cargado not in restantes), "No se eliminaron los respaldos QA"


@pytest.mark.roles
@pytest.mark.tc("TC-179")
def test_tc179_respaldos_prohibidos_a_los_otros_roles():
    """Crear, cargar, descargar, restaurar y eliminar respaldos: 403 para los cuatro roles que no administran."""
    for rol in OTROS_ROLES:
        c = cliente(rol)
        intentos = {
            "crear": c.post(RESPALDOS + "create/").status_code,
            "cargar": c.post(RESPALDOS + "cargar/", archivos={"archivo": ("r.sql", b"-- PostgreSQL database dump",
                                                                         "application/sql")}).status_code,
            "descargar": c.get(RESPALDOS + "inexistente.sql/").status_code,
            "restaurar": c.post(RESPALDOS + "inexistente.sql/restore/").status_code,
            "eliminar": c.delete(RESPALDOS + "inexistente.sql/delete/").status_code,
        }
        assert all(v == 403 for v in intentos.values()), f"{rol}: {intentos}"


@pytest.mark.tc("TC-180")
@pytest.mark.skipif(os.environ.get("AUTORIZAR_RESTAURAR") != "1",
                    reason="Restaurar reemplaza toda la base: solo con AUTORIZAR_RESTAURAR=1")
def test_tc180_restaurar_un_respaldo_recien_creado():
    """Restaura un respaldo creado segundos antes: la base queda con las mismas cifras y se puede seguir usando."""
    admin = cliente("administrador")
    antes = _json(admin.get("/pacientes/", params={"page_size": 1})).get("count")
    creado = admin.post(RESPALDOS + "create/", timeout=600, verificar=True).json()["filename"]
    seguridad = None
    try:
        r = admin.post(RESPALDOS + f"{creado}/restore/", timeout=1800)
        assert r.status_code == 200, f"No restauró: HTTP {r.status_code} {r.text[:300]}"               # validación 1
        seguridad = r.json().get("backup_seguridad")
        assert seguridad, "No informó el respaldo de seguridad previo"                                 # validación 2
        despues = _json(cliente("medico").get("/pacientes/", params={"page_size": 1})).get("count")
        assert despues == antes, f"Las pacientes cambiaron al restaurar: {antes} -> {despues}"        # validación 3
    finally:
        for nombre in (creado, seguridad):
            if nombre:
                admin.delete(RESPALDOS + f"{nombre}/delete/")
