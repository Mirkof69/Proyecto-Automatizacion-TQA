"""Configuración y copias de seguridad por la interfaz (Selenium WebDriver).

El administrador abre Configuración (cada pestaña sin errores) y Copias de seguridad con sus acciones; un médico no
puede entrar a ninguna de las dos.
"""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By

from pages.pantalla_page import PantallaPage
from utils import evidencias

pytestmark = [pytest.mark.ui, pytest.mark.roles]


@pytest.mark.tc("TC-181")
def test_tc181_configuracion_y_respaldos_en_pantalla(navegador):
    """Configuración: pestañas sanas para el administrador. Copias de seguridad: lista, crear y cargar respaldo."""
    pag = PantallaPage(navegador("administrador"))
    salud = pag.abrir_y_medir("/dashboard/configuracion")
    evidencias.captura(pag.driver, "configuracion", "Configuración del sistema (administrador)")
    assert not salud.problemas(), f"Configuración: {salud.problemas()}"                              # validación 1
    for pestana in pag.pestanas():
        s = pag.abrir_pestana(pestana)
        assert not s.http_5xx and not s.denegada, f"Configuración › {pestana}: {s.problemas()}"       # validación 2

    salud = pag.abrir_y_medir("/dashboard/backup")
    evidencias.captura(pag.driver, "respaldos", "Copias de seguridad (administrador)")
    assert not salud.problemas(), f"Copias de seguridad: {salud.problemas()}"                        # validación 3
    textos = {b.text.strip() for b in pag.driver.find_elements(By.CSS_SELECTOR, "button") if b.text.strip()}
    for boton in ("Crear Backup", "Cargar respaldo", "Actualizar"):
        assert boton in textos, f"Falta el botón «{boton}» en Copias de seguridad: {sorted(textos)}"   # validación 4
    assert pag.driver.find_elements(By.CSS_SELECTOR, "input[type='file'][accept='.sql']"), \
        "El botón Cargar respaldo no acepta archivos .sql"

    medico = PantallaPage(navegador("medico"))
    for ruta in ("/dashboard/configuracion", "/dashboard/backup"):
        s = medico.abrir_y_medir(ruta)
        texto = medico.driver.find_element(By.TAG_NAME, "body").text
        assert s.denegada or "Acceso" in texto or "403" in texto, f"El médico entró a {ruta}"         # validación 5
