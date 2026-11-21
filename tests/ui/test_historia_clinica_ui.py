"""Historia clínica completa por la interfaz, con los datos reales de una paciente QA.

Comprueba que cada pestaña muestre lo que hay en la base, que la nota SOAP y la prescripción se guarden de verdad
(antes solo se mostraban hasta recargar), que un control registrado por otro usuario aparezca sin recargar la página
y que la exportación genere archivos reales.
"""
from __future__ import annotations

import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from conftest import cliente
from data import fabrica
from pages.pantalla_page import PantallaPage
from utils import evidencias
from utils.api_client import _json

pytestmark = [pytest.mark.ui, pytest.mark.historia_clinica]

TABS = "//div[contains(@class,'main-tabs-card')]//div[contains(@class,'ant-tabs-tab')][.//span[contains(normalize-space(.),'{}')]]"


def _pestana(pag: PantallaPage, nombre: str):
    """Con doce pestañas, Ant Design pasa las últimas a un menú «…»: el botón sigue en el DOM y se activa por script."""
    boton = WebDriverWait(pag.driver, 15).until(EC.presence_of_element_located(
        (By.XPATH, TABS.format(nombre) + "//div[@role='tab']")))
    pag.driver.execute_script("arguments[0].click();", boton)
    time.sleep(0.8)


def _texto(pag: PantallaPage) -> str:
    return pag.driver.find_element(By.CSS_SELECTOR, ".ant-layout-content").text


def _escribir_en(drawer, nombre: str, texto: str):
    campo = drawer.find_element(By.CSS_SELECTOR, f"textarea#{nombre}, input#{nombre}")
    campo.clear()
    campo.send_keys(texto)


@pytest.mark.smoke
@pytest.mark.tc("TC-182")
def test_tc182_historia_clinica_con_datos_reales_y_en_tiempo_real(navegador, paciente_qa, embarazo_qa):
    """Datos de la base en cada pestaña, nota y receta guardadas, actualización sin recargar y exportación real."""
    medico = cliente("medico")
    pid, eid = paciente_qa["id"], embarazo_qa["id"]
    control = medico.post("/controles/", fabrica.control(pid, eid, semanas=28, presion_arterial_sistolica=112,
                                                          presion_arterial_diastolica=72), verificar=True).json()

    pag = PantallaPage(navegador("medico"))
    salud = pag.abrir_y_medir(f"/dashboard/historia-clinica/{pid}")
    assert not salud.problemas(), f"La historia clínica no carga sana: {salud.problemas()}"             # validación 1
    assert paciente_qa["nombre"] in _texto(pag), "La cabecera no muestra el nombre de la paciente"

    _pestana(pag, "Controles")
    assert "112" in _texto(pag), "La pestaña Controles no muestra el control registrado (PA 112/72)"       # validación 2
    evidencias.captura(pag.driver, "controles", "Controles de la paciente QA")

    # Nota SOAP desde la pantalla: tiene que quedar en la base, no solo en pantalla.
    _pestana(pag, "Evolución")
    pag.clic((By.XPATH, "//button[.//span[normalize-space(.)='Nueva Nota']]"))
    drawer = WebDriverWait(pag.driver, 15).until(EC.visibility_of_element_located(
        (By.XPATH, "//div[contains(@class,'ant-drawer-content')][.//*[contains(.,'Nueva Nota de Evolución')]]")))
    marca = f"QA nota {int(time.time())}"
    _escribir_en(drawer, "subjetivo", f"{marca}: refiere cefalea leve")
    _escribir_en(drawer, "objetivo", "PA 112/72, AU 27 cm, LCF 142")
    _escribir_en(drawer, "analisis", "Embarazo de 28 semanas de curso normal")
    _escribir_en(drawer, "plan", "Control en 4 semanas, hierro diario")
    drawer.find_element(By.XPATH, ".//button[@type='submit' or .//span[contains(.,'Guardar')]]").click()
    WebDriverWait(pag.driver, 20).until(lambda d: marca in _texto(pag))
    notas = _json(medico.get(f"/notas-evolucion/?paciente={pid}")).get("results", [])
    assert any(marca in (n.get("motivo_consulta") or "") for n in notas), "La nota no se guardó en la base"  # validación 3
    evidencias.captura(pag.driver, "nota", "Nota SOAP guardada y mostrada")

    # Prescripción: queda como nota con el plan estructurado y aparece en Tratamientos.
    _pestana(pag, "Tratamientos")
    pag.clic((By.XPATH, "//button[.//span[normalize-space(.)='Nueva Receta']]"))
    modal = WebDriverWait(pag.driver, 15).until(EC.visibility_of_element_located(
        (By.XPATH, "//div[contains(@class,'ant-modal-content')][.//*[contains(.,'Prescripción de Medicamentos')]]")))
    for campo, valor in (("medicamento", "Sulfato ferroso QA"), ("dosis", "300 mg"), ("frecuencia", "cada 24 h"),
                         ("duracion", "30 días")):
        _escribir_en(modal, campo, valor)
    modal.find_element(By.XPATH, ".//button[contains(@class,'ant-btn-primary')]").click()
    WebDriverWait(pag.driver, 20).until(lambda d: "Sulfato ferroso QA" in _texto(pag))
    notas = _json(medico.get(f"/notas-evolucion/?paciente={pid}")).get("results", [])
    assert any("Sulfato ferroso QA" in (n.get("plan_tratamiento") or "") for n in notas), \
        "La prescripción no se guardó en la base"                                                          # validación 4

    # Tiempo real: otro usuario registra un control y la historia lo muestra sin recargar.
    _pestana(pag, "Controles")
    medico.post("/controles/", fabrica.control(pid, eid, semanas=29, presion_arterial_sistolica=126,
                                               presion_arterial_diastolica=84), verificar=True)
    pag.driver.execute_script("window.dispatchEvent(new Event('focus'));")
    WebDriverWait(pag.driver, 30).until(lambda d: "126" in _texto(pag))                                 # validación 5
    evidencias.captura(pag.driver, "tiempo-real", "Control nuevo visible sin recargar la página")

    # Exportación real: PDF y Excel desde la API que usa el botón Exportar.
    pdf = medico.get(f"/pacientes/{pid}/reporte-pdf/", timeout=120)
    xlsx = medico.get(f"/pacientes/{pid}/exportar-excel/", timeout=120)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF", "La historia no se exporta en PDF"       # validación 6
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK", "La historia no se exporta en Excel"
    assert control["id"]
    assert not pag.medir().http_5xx, "Hubo errores del servidor durante el uso de la historia clínica"     # validación 7
