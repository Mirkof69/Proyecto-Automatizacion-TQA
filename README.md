# Proyecto de Automatización · TQA

Pruebas automatizadas de punta a punta (E2E) del sistema **Fetal Medical
Bolivia**, para la materia Testing y Aseguramiento de la Calidad (TQA) —
UNIFRANZ.

- **Framework:** [Playwright](https://playwright.dev/) (`@playwright/test`)
- **Lenguaje:** JavaScript (Node.js 20)
- **Navegador:** Chromium

## Qué se prueba

| Archivo | Flujo |
|---|---|
| `tests/flujo-clinico.spec.js` | Paciente → embarazo → control prenatal → ecografía → análisis con IA → parto |
| `tests/cargos.spec.js` | Cargos y permisos: un cargo sin Laboratorio, MFA en el primer ingreso y respuesta 403 de la API |
| `tests/funciones-nuevas.spec.js` | Partograma, Capurro, epicrisis, importación de laboratorio y cardiotocografía |
| `tests/ginecologia.spec.js` | Ecografía ginecológica con IA (masa ovárica y mioma) |
| `tests/ia-modelos.spec.js` | Cada modelo de IA usado desde su pantalla con datos reales |
| `tests/pantallas.spec.js` | Las 68 pantallas en tema claro y oscuro: sin errores de JavaScript, sin 500, sin pantalla en blanco ni desborde |
| `recorrido_listados.js` | Tiempo de carga y peticiones de cada listado |

## Cómo ejecutar

Necesita el sistema en marcha (frontend en `http://localhost:3000`, API en
`http://localhost:8000/api`).

```bash
npm install
npx playwright install chromium
# Las credenciales NO están en el código: se pasan por variables de entorno
export E2E_PASSWORD="<contraseña de las cuentas de demostración>"
npx playwright test                 # todas
npx playwright test tests/pantallas.spec.js --reporter=line
```

Variables opcionales: `E2E_BASE_URL`, `E2E_API`, `E2E_ADMIN_EMAIL`,
`PW_CHROMIUM` (ruta a un Chromium ya instalado).

## Equipo

- Mirkof Guzman — [@Mirkof69](https://github.com/Mirkof69)
- [Integrantes del equipo]

Docente: Reynel Sánchez
