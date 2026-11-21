"""Envío del reporte por correo (SMTP con STARTTLS; Gmail con contraseña de aplicación).

Variables de .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD y MAIL_TO (el destinatario principal; varios
separados por coma). Sin SMTP_PASSWORD no intenta enviar y lo informa.

Desde el panel se puede además: enviar a otros correos (por ejemplo, el Gmail de un docente), escribir el asunto y
un mensaje propio y elegir los adjuntos: reporte PDF, Word o HTML, los resultados en CSV o JSON, el ZIP con las
evidencias y el Capítulo 6. Gmail admite hasta 25 MB por mensaje: el envío se rechaza antes si se pasa.
"""
from __future__ import annotations

import csv
import html as html_mod
import io
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from reporting.modelo import cargar
from utils import config

LIMITE_BYTES = 24 * 1024 * 1024
MAX_DESTINATARIOS = 10
PATRON_CORREO = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ADJUNTOS_POR_DEFECTO = ["pdf", "html", "csv"]
CAPITULO6 = config.RAIZ / "capitulo6" / "entregables" / "Capitulo_6_Estrategia_y_Ejecucion_de_Pruebas.pdf"
WORD = "vnd.openxmlformats-officedocument.wordprocessingml.document"


def configurado() -> bool:
    return all(config.env(v) for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"))


def principales() -> list[str]:
    return [x.strip() for x in (config.env("MAIL_TO") or "").split(",") if x.strip()]


def validar_direcciones(lista) -> list[str]:
    """Separa por coma, punto y coma o espacios, quita repetidos y valida el formato. ValueError si alguno no es
    un correo válido."""
    if isinstance(lista, str):
        lista = [lista]
    salida = []
    for x in lista or []:
        for d in re.split(r"[,;\s]+", str(x)):
            d = d.strip()
            if not d:
                continue
            if not PATRON_CORREO.match(d) or len(d) > 120:
                raise ValueError(f"«{d}» no es un correo válido")
            if d.lower() not in (y.lower() for y in salida):
                salida.append(d)
    if len(salida) > MAX_DESTINATARIOS:
        raise ValueError(f"Como máximo {MAX_DESTINATARIOS} destinatarios adicionales")
    return salida


# ---------------------------------------------------------------------- adjuntos
def _csv(r) -> bytes:
    from reporting.modelo import NOMBRE_TIPO
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    w.writerow(["Caso", "Prueba", "Módulo", "Tipos de prueba", "Herramienta", "Estado", "Duración (s)", "Videos",
                "Evidencias", "Clasificación del fallo", "Error"])
    for c in r.casos:
        w.writerow([c["tc"], c["nombre"], c["modulo"], ", ".join(NOMBRE_TIPO[t] for t in c["tipos"]), c["herramienta"],
                    c["estado"], str(c["duracion_s"]).replace(".", ","), len(c.get("videos") or []),
                    len(c.get("evidencias") or []), c["clasificacion"], (c["error"] or "").replace("\n", " ")[:500]])
    return ("﻿" + salida.getvalue()).encode("utf-8")   # con BOM: Excel lo abre con tildes


def _catalogo_adjuntos(r) -> dict[str, dict]:
    d: Path = r.dir_run
    zip_ = next(iter(sorted(d.glob("*.zip"))), None)
    archivos = {
        "pdf": ("Reporte en PDF", d / "reporte.pdf", ("application", "pdf")),
        "docx": ("Reporte en Word", d / "reporte.docx", ("application", WORD)),
        "html": ("Reporte HTML (se abre en el navegador)", d / "reporte.html", ("text", "html")),
        "json": ("Resultados en JSON (results.json)", d / "results.json", ("application", "json")),
        "zip": ("ZIP con evidencias, reportes y videos", zip_, ("application", "zip")),
        "capitulo6": ("Capítulo 6: estrategia y ejecución de pruebas (PDF)", CAPITULO6, ("application", "pdf")),
    }
    salida = {}
    for clave, (nombre, ruta, tipo) in archivos.items():
        existe = bool(ruta) and ruta.is_file()
        salida[clave] = {"nombre": nombre, "ruta": ruta if existe else None, "tipo": tipo,
                         "bytes": ruta.stat().st_size if existe else 0,
                         "archivo": (ruta.name if clave in ("capitulo6", "zip") else f"{r.run_id}_{ruta.name}") if existe else ""}
    contenido = _csv(r)
    salida["csv"] = {"nombre": "Resultados de cada prueba en CSV (Excel)", "contenido": contenido,
                     "tipo": ("text", "csv"), "bytes": len(contenido), "archivo": f"{r.run_id}_resultados.csv"}
    return {k: salida[k] for k in ("pdf", "csv", "html", "docx", "json", "zip", "capitulo6")}


# ---------------------------------------------------------------------- textos
NOMBRES_SUITE = {"completa": "todas las pruebas", "regresion": "regresión", "humo": "humo", "e2e": "extremo a extremo",
                 "ide": "Selenium IDE", "busqueda": "buscadores y filtros", "ui": "interfaz", "api": "API",
                 "barrido": "barrido de pantallas", "rendimiento": "rendimiento", "autenticacion": "autenticación",
                 "roles": "roles y permisos", "pacientes": "pacientes", "clinico": "historia clínica",
                 "ia": "ecografía e IA"}


def _suite(r) -> str:
    s = r.datos.get("suite") or "completa"
    return NOMBRES_SUITE.get(s, s)


def _n(valor: float) -> str:
    return f"{valor}".replace(".", ",")


def _plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def asunto_por_defecto(r) -> str:
    estado = "con fallos" if r.totales["FAIL"] else "sin fallos"
    return (f"[Fetal Medical QA] Corrida {r.run_id} ({_suite(r)}) {estado}: {r.totales['PASS']}/{r.totales['total']} "
            f"PASS, salud {_n(r.tasa_exito)} %")


def mensaje_por_defecto(r) -> str:
    t = r.totales
    return (f"Le envío los resultados de la corrida {r.run_id} de las pruebas automatizadas del sistema Fetal Medical "
            f"(suite: {_suite(r)}): {_plural(t['total'], 'prueba', 'pruebas')}, "
            f"{_plural(t['PASS'], 'aprobada', 'aprobadas')} y {_plural(t['FAIL'], 'fallida', 'fallidas')}, con una "
            f"salud de {_n(r.tasa_exito)} %. Adjunto el reporte y los resultados de cada prueba.\n\n"
            "Este es un mensaje automático del Centro de Pruebas QA.")


def _metricas() -> list[tuple[str, str]]:
    """Coberturas calculadas por tools/exportar_casos.py (docs/cobertura.json), si existen."""
    import json
    f = config.RAIZ / "docs" / "cobertura.json"
    if not f.exists():
        return []
    c = json.loads(f.read_text(encoding="utf-8"))
    h = c.get("por_herramienta", {})
    return [("Casos de prueba diseñados", str(c["casos_diseñados"])),
            ("Casos automatizados", f"{c['automatizados']} de {c['seleccionados_automatizar']} seleccionados "
                                    f"({c['cobertura_automatizacion_pct']} %)"),
            ("Por herramienta", ", ".join(f"{k}: {v}" for k, v in h.items())),
            ("Cobertura de requisitos", f"{c['rf_con_casos'] + c['rnf_con_casos']} de {c['rf_total'] + c['rnf_total']} "
                                        f"({c['cobertura_requisitos_pct']} %)")]


def _tabla(encabezados: list[str], filas: list[list]) -> str:
    celda = "border:1px solid #d9e2e2;padding:5px 8px"
    cab = "".join(f"<th style='{celda};background:#1f3864;color:#fff;text-align:left'>{h}</th>" for h in encabezados)
    cuerpo = "".join("<tr>" + "".join(f"<td style='{celda}'>{v}</td>" for v in f) + "</tr>" for f in filas)
    return f"<table style='border-collapse:collapse;font-size:13px'><tr>{cab}</tr>{cuerpo}</table>"


def _cuerpo(r, mensaje: str, adjuntos: list[str]) -> tuple[str, str]:
    t = r.totales
    fallos = "".join(f"<li><b>{c['tc']}</b> {html_mod.escape(c['nombre'])} — {html_mod.escape(c['error'][:160])}</li>"
                     for c in r.fallos[:15])
    por_tipo = [[x["tipo"], x["total"], x["PASS"], x["FAIL"], x["BLOCKED"] + x["NOT RUN"], f"{x['tasa']} %"]
                for x in r.por_tipo]
    texto = (f"{mensaje}\n\nCorrida {r.run_id} ({r.datos.get('suite') or 'completa'}): {t['total']} pruebas, "
             f"{t['PASS']} PASS, {t['FAIL']} FAIL, {t['BLOCKED']} BLOCKED, {t['NOT RUN']} NOT RUN. "
             f"Salud {r.tasa_exito} % sobre ejecutadas.\n\nPor tipo de prueba:\n"
             + "\n".join(f"- {x[0]}: {x[2]}/{x[1]} PASS ({x[5]})" for x in por_tipo))
    mensaje_html = html_mod.escape(mensaje).replace("\n", "<br>")
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#1b2a2c;max-width:760px">
<p style="font-size:15px;line-height:1.5">{mensaje_html}</p>
<h2 style="margin:18px 0 6px">Fetal Medical · Reporte de pruebas automatizadas</h2>
<p style="margin:0 0 12px;color:#5b6e70">Corrida {r.run_id} · suite {r.datos.get('suite') or 'completa'} · {r.duracion_legible}</p>
<table cellpadding="8" style="border-collapse:collapse;font-size:14px">
<tr><td style="border:1px solid #d9e2e2">Pruebas<br><b style="font-size:20px">{t['total']}</b></td>
<td style="border:1px solid #d9e2e2;color:#1e7f4f">PASS<br><b style="font-size:20px">{t['PASS']}</b></td>
<td style="border:1px solid #d9e2e2;color:#b3261e">FAIL<br><b style="font-size:20px">{t['FAIL']}</b></td>
<td style="border:1px solid #d9e2e2;color:#a15c00">BLOCKED<br><b style="font-size:20px">{t['BLOCKED']}</b></td>
<td style="border:1px solid #d9e2e2;color:#6b7280">NOT RUN<br><b style="font-size:20px">{t['NOT RUN']}</b></td>
<td style="border:1px solid #d9e2e2">Salud<br><b style="font-size:20px">{r.tasa_exito} %</b></td></tr></table>
<p style="font-size:13px;color:#5b6e70">Salud = PASS ÷ (PASS + FAIL).</p>
<h3>Resultados por tipo de prueba</h3>{_tabla(["Tipo", "Pruebas", "PASS", "FAIL", "No ejecutadas", "Salud"], por_tipo)}
<p style="font-size:12px;color:#5b6e70">Una prueba puede ser de más de un tipo (por ejemplo, humo y API).</p>
{'<h3>Fallos</h3><ul>' + fallos + '</ul>' if fallos else '<p>Sin fallos en esta corrida.</p>'}
<h3>Resultados por módulo</h3>{_tabla(["Módulo", "Pruebas", "PASS", "FAIL", "Salud"],
                                     [[m['modulo'], m['total'], m['PASS'], m['FAIL'], f"{m['tasa']} %"] for m in r.por_modulo])}
<h3>Métricas</h3>{_tabla(["Métrica", "Valor"], [[f"<b>{k}</b>", v] for k, v in _metricas()]
    + [[f"<b>Carga ({x['escenario']})</b>", f"{x['peticiones']} peticiones, p95 {x['p95_ms']:.0f} ms, errores {x['error_pct']} %"] for x in r.rendimiento]
    + [["<b>Defectos</b>", f"{len(r.defectos)} registrados, {sum(d['estado'] == 'Verificado' for d in r.defectos)} corregidos y verificados"]])}
<p style="color:#5b6e70;font-size:12px">Adjuntos: {html_mod.escape(', '.join(adjuntos) or 'ninguno')}. Datos sintéticos QA;
sin datos de pacientes reales. Mensaje automático del Centro de Pruebas QA de Fetal Medical.</p></div>"""
    return texto, html


# ---------------------------------------------------------------------- panel y envío
def opciones(run_id: str) -> dict:
    r = cargar(run_id)
    cat = _catalogo_adjuntos(r)
    return {"configurado": configurado(), "remitente": config.env("SMTP_USER") or "", "principal": principales(),
            "asunto": asunto_por_defecto(r), "mensaje": mensaje_por_defecto(r), "limite_mb": LIMITE_BYTES // 2**20,
            "adjuntos": [{"clave": k, "nombre": v["nombre"], "kb": max(1, -(-v["bytes"] // 1024)) if v["bytes"] else 0,
                          "disponible": bool(v.get("ruta") or v.get("contenido")),
                          "marcado": k in ADJUNTOS_POR_DEFECTO} for k, v in cat.items()]}


def enviar(run_id: str | None = None, principal: bool = True, otros: list[str] | None = None,
           asunto: str | None = None, mensaje: str | None = None, adjuntos: list[str] | None = None) -> dict:
    r = cargar(run_id)
    if not configurado():
        return {"enviado": False, "motivo": "Falta configurar SMTP en .env (SMTP_PASSWORD de aplicación de Gmail)"}
    try:
        otros = validar_direcciones(otros or [])
    except ValueError as e:
        return {"enviado": False, "motivo": str(e)}
    destinatarios = (principales() if principal else []) + [x for x in otros if x.lower() not in
                                                             (p.lower() for p in principales())]
    if not destinatarios:
        return {"enviado": False, "motivo": "No hay destinatarios"}
    claves = ADJUNTOS_POR_DEFECTO if adjuntos is None else [a for a in adjuntos if a]
    cat = _catalogo_adjuntos(r)
    desconocidos = [a for a in claves if a not in cat]
    if desconocidos:
        return {"enviado": False, "motivo": f"Adjunto desconocido: {', '.join(desconocidos)}"}
    elegidos = [cat[a] for a in claves if cat[a].get("ruta") or cat[a].get("contenido")]
    total = sum(a["bytes"] for a in elegidos)
    if total > LIMITE_BYTES:
        return {"enviado": False, "motivo": f"Los adjuntos suman {total / 2**20:.1f} MB y Gmail admite hasta 25 MB: "
                                            "quite el ZIP o el Capítulo 6"}
    asunto = (asunto or "").strip()[:200] or asunto_por_defecto(r)
    mensaje = (mensaje or "").strip()[:5000] or mensaje_por_defecto(r)
    nombres = [a["archivo"] for a in elegidos]
    texto, html = _cuerpo(r, mensaje, nombres)

    m = EmailMessage()
    m["Subject"] = asunto
    m["From"] = config.env("SMTP_USER")
    m["To"] = ", ".join(destinatarios)
    m.set_content(texto)
    m.add_alternative(html, subtype="html")
    for a in elegidos:
        datos = a["contenido"] if a.get("contenido") else a["ruta"].read_bytes()
        m.add_attachment(datos, maintype=a["tipo"][0], subtype=a["tipo"][1], filename=a["archivo"])
    try:
        with smtplib.SMTP(config.env("SMTP_HOST"), int(config.env("SMTP_PORT", "587")), timeout=90) as s:
            s.starttls()
            s.login(config.env("SMTP_USER"), config.env("SMTP_PASSWORD"))
            s.send_message(m)
    except smtplib.SMTPAuthenticationError as e:
        return {"enviado": False, "motivo": f"Gmail rechazó la clave ({e.smtp_code}). Use una contraseña de aplicación: "
                                            "myaccount.google.com/apppasswords"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"enviado": False, "motivo": "Gmail rechazó el destinatario: " + ", ".join(e.recipients)}
    except smtplib.SMTPServerDisconnected:
        # Gmail responde 534 «Application-specific password required» y corta la conexión cuando se le da la
        # contraseña normal de una cuenta con verificación en dos pasos
        return {"enviado": False, "motivo": "Gmail cerró la conexión al autenticar: la cuenta exige una contraseña de "
                                            "aplicación (myaccount.google.com/apppasswords), no la contraseña normal"}
    except Exception as e:  # el reporte ya existe en disco: un fallo del correo no invalida la corrida
        return {"enviado": False, "motivo": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"enviado": True, "para": destinatarios, "adjuntos": nombres, "asunto": asunto}
