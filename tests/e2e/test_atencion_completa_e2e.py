"""Prueba de extremo a extremo (E2E): una atención completa por la interfaz, con el rol que hace cada paso.

Recepción registra a la paciente → el médico abre su embarazo → registra el primer control prenatal → analiza una
ecografía con la IA → abre la historia clínica y encuentra a la paciente. Cada paso se hace por la pantalla y se
comprueba en la API: que el sistema diga «guardado» no basta. Los datos son sintéticos QA y se dan de baja al
terminar la sesión de pytest (fixture limpieza_qa).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from selenium.webdriver.common.by import By

from conftest import cliente
from data import fabrica
from pages.formularios_clinicos import ControlFormPage, EmbarazoFormPage, IaMedicaPage
from pages.pacientes_page import PacientesPage

pytestmark = [pytest.mark.e2e, pytest.mark.historia_clinica]

IMAGENES = Path(__file__).resolve().parents[2] / "data" / "imagenes_ia"


def _paciente_por_ci(ci: str) -> list[dict]:
    return [p for p in cliente("recepcion").lista("/pacientes/", search=ci) if p.get("ci") == ci]


@pytest.mark.tc("TC-173")
def test_tc173_atencion_completa_de_punta_a_punta(navegador):
    """Atención completa de punta a punta: registro, embarazo, control prenatal, ecografía con IA e historia clínica."""
    # 1. Recepción registra a la paciente con el asistente
    rec = navegador("recepcion")
    pacientes = PacientesPage(rec).abrir()
    datos = fabrica.paciente()
    pacientes.abrir_asistente().completar_paso_personal(datos).siguiente().completar_contacto(datos)
    pacientes.siguiente().marcar_consentimiento().evidencia("1-registro", "Recepción: asistente de registro completo")
    pacientes.registrar()
    pacientes.mensaje_exito(20)
    pacientes.descartar_pregunta_embarazo()
    halladas = pacientes.esperar_que(lambda: _paciente_por_ci(datos["ci"]), 20, "La paciente no llegó a la base")
    for p in halladas:
        fabrica.anotar("pacientes", p["id"])
    assert len(halladas) == 1, f"Se esperaba 1 paciente con la CI {datos['ci']}, hay {len(halladas)}"
    paciente = halladas[0]

    # 2. El médico abre el embarazo de esa paciente
    med = navegador("medico")
    embarazo_pag = EmbarazoFormPage(med).abrir()
    fum = date.today() - timedelta(days=203)
    embarazo_pag.registrar(paciente["ci"], fum).evidencia("2-embarazo", "Médico: embarazo con FUM de hace 29 semanas")
    embarazo_pag.guardar("Crear Embarazo")
    embarazos = embarazo_pag.esperar_que(lambda: cliente("medico").lista("/embarazos/", paciente=paciente["id"]), 25,
                                         "El embarazo no llegó a la base")
    assert len(embarazos) == 1, f"Se esperaba 1 embarazo, hay {len(embarazos)}"
    embarazo = embarazos[0]

    # 3. Primer control prenatal del embarazo
    control_pag = ControlFormPage(med).abrir(f"{ControlFormPage.RUTA}?paciente={paciente['id']}&embarazo={embarazo['id']}")
    control_pag.texto_presente((By.CSS_SELECTOR, ".ant-layout-content"), paciente["id_clinico"], 20)
    control = {"numero_control": 1, "edad_gestacional_semanas": 29, "peso_actual": 64, "temperatura": 36.6,
               "presion_arterial_sistolica": 112, "presion_arterial_diastolica": 72, "frecuencia_cardiaca": 80,
               "altura_uterina": 28, "frecuencia_cardiaca_fetal": 142}
    control_pag.completar(control, date.today()).evidencia("3-control", "Médico: control prenatal con signos normales")
    control_pag.guardar("Guardar Control")
    controles = control_pag.esperar_que(lambda: cliente("medico").lista("/controles/", embarazo=embarazo["id"]), 25,
                                        "El control no llegó a la base")
    assert len(controles) == 1 and controles[0]["embarazo"] == embarazo["id"], f"Controles del embarazo: {controles}"

    # 4. Ecografía analizada con las redes neuronales
    planos = IMAGENES / "planos"
    imagen = sorted(planos.glob("*.png"))[0] if planos.exists() else \
        sorted(p for p in IMAGENES.glob("*.*") if p.suffix.lower() in (".png", ".jpg", ".jpeg"))[0]
    ia = IaMedicaPage(med).abrir()
    ia.subir(str(imagen)).evidencia("4-ecografia", f"Médico: ecografía de prueba {imagen.name}")
    ia.analizar()
    ia.texto_presente((By.CSS_SELECTOR, ".ant-layout-content"), "%", 60)
    ia.evidencia("4-ia", "Resultado de la IA con su confianza")

    # 5. La historia clínica reúne a la paciente
    historia = PacientesPage(med).abrir(f"/dashboard/pacientes/{paciente['id']}/historia")
    historia.texto_presente((By.CSS_SELECTOR, ".ant-layout-content"), paciente["ci"], 25)
    historia.evidencia("5-historia", "Historia clínica de la paciente atendida")
