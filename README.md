# nubeduca-ms-cursos

Microservicio de **cursos, secciones, matrículas y anuncios** de Nubeduca. Se creó desde [`nubeduca-plantilla-servicio`](https://github.com/nubeduca/nubeduca-plantilla-servicio) (LIB-01) y sigue el contrato [`apis/ms-cursos.openapi.yaml`](https://github.com/nubeduca/nubeduca-contratos/blob/main/apis/ms-cursos.openapi.yaml).

| Tarea | Responsable | Estado |
|---|---|---|
| ACA-03 · Cursos y Secciones | Dev 3 @btoroled, Dev 4 @Edinson695 | Cursos con ciclo DRAFT → PUBLISHED → ARCHIVED, secciones, consumidor de `term.started` y seed de 30.000 cursos |
| ACA-04 · Matrícula e importación CSV | Dev 3 @btoroled | Pendiente (depende de ACA-02 y ACA-03) |
| COL-01 · Anuncios | Dev 3 @btoroled | Pendiente (depende de ACA-04) |

## API (ACA-03)

El tenant sale siempre del token (`custom:tenant_id`, o `Bearer mock.<tenant>.<ROL>.<userId>` en modo mock), nunca de la ruta ni del body.

| Método y ruta | Roles | Qué hace | Evento |
|---|---|---|---|
| `GET /health` | pública | Prueba de vida | |
| `GET /courses?termId=&status=&limit=&nextToken=` | STUDENT, TEACHER, ADMIN | ADMIN: todos. TEACHER: los suyos. STUDENT: los PUBLISHED donde está matriculado (ACA-04; hasta entonces, ninguno) | |
| `POST /courses` | TEACHER, ADMIN | Crea en `DRAFT`. 409 `PERIODO_NO_ACTIVO` o `CURSO_DUPLICADO` (mismo `code` en el periodo) | `lms.courses.course.created.v1` |
| `GET /courses/{courseId}` | quien puede ver el curso | Al alumno sin acceso se le responde 404; al docente que no es dueño, 403 | |
| `PATCH /courses/{courseId}` | TEACHER dueño, ADMIN | Solo `title` y `description`. 409 `CURSO_ARCHIVADO` | |
| `POST /courses/{courseId}/publish` | TEACHER dueño, ADMIN | `DRAFT` → `PUBLISHED`. 409 `CURSO_NO_EN_BORRADOR` | `lms.courses.course.published.v1` |
| `POST /courses/{courseId}/archive` | TEACHER dueño, ADMIN | `DRAFT` o `PUBLISHED` → `ARCHIVED` (solo lectura). 409 `CURSO_ARCHIVADO` | `lms.courses.course.archived.v1` |
| `GET /courses/{courseId}/sections` | quien puede ver el curso | Secciones, en orden de creación | |
| `POST /courses/{courseId}/sections` | TEACHER dueño, ADMIN | Crea una sección. 409 `CURSO_ARCHIVADO` | |

Los errores tienen la forma `{code, message, requestId}`.

## Eventos

| Evento | Dirección | Cuándo |
|---|---|---|
| `lms.courses.course.created.v1` | publica → ms-contenidos | `POST /courses` |
| `lms.courses.course.published.v1` | publica → ms-notificaciones | `POST /courses/{id}/publish` |
| `lms.courses.course.archived.v1` | publica | `POST /courses/{id}/archive` |
| `lms.users.term.started.v1` | consume ← ms-usuarios | Guarda el periodo como activo en `PROY#TERM#<termId>` |

Los eventos propios salen por **outbox**: el cambio y el ítem `EVENTO#<eventId>` van en la misma `TransactWriteItems`, y antes se validan contra su JSON Schema de `nubeduca-contratos` (`contratos/esquemas/`). Los eventos del curso llevan `metadata.courseId` y una `aggregateVersion` que sube con cada transición (1 creado, 2 publicado, 3 archivado). El consumidor usa el inbox de la plantilla: descarta duplicados y eventos viejos, y los fallos van a la DLQ tras 5 intentos.

## Tabla `<stage>-t_cursos`

PK `tenant_id` (HASH) + `sk` (RANGE), `PAY_PER_REQUEST`, Stream `NEW_IMAGE` y TTL en `ttl`.

| `sk` | Entidad |
|---|---|
| `COURSE#<courseId>` | Curso |
| `COURSECODE#<termId>#<code>` | Reserva del código del curso dentro del periodo (409 si se repite) |
| `SECTION#<courseId>#<sectionId>` | Sección del curso |
| `PROY#TERM#<termId>` | Periodo activo, proyección de `term.started` |
| `ENROLL#<courseId>#<userId>` | Reservado para ACA-04 (matrícula) |
| `EVENTO#`, `INBOX#`, `AGG#` | Outbox, inbox y versión de agregados de la plantilla |

```json
{
  "tenant_id": "utec",
  "sk": "COURSE#crs_01JCQRZHQT9F4WY2JYPDV546K7",
  "entidad": "COURSE",
  "courseId": "crs_01JCQRZHQT9F4WY2JYPDV546K7",
  "termId": "trm_01JCP0KWJDXF9GJ9YNTK2H84A9",
  "code": "CS2032",
  "title": "Cloud Computing",
  "description": "Serverless y eventos",
  "status": "PUBLISHED",
  "ownerId": "c62d9930-b473-4b1d-adec-72fdda7cf599",
  "createdAt": "2026-11-13T15:00:00Z",
  "version": 2
}
```

```json
{
  "tenant_id": "utec",
  "sk": "SECTION#crs_01JCQRZHQT9F4WY2JYPDV546K7#sec_01JCR0A4T7Q9W2N8M5K3H6G1F0",
  "entidad": "SECTION",
  "courseId": "crs_01JCQRZHQT9F4WY2JYPDV546K7",
  "sectionId": "sec_01JCR0A4T7Q9W2N8M5K3H6G1F0",
  "name": "Sección 1",
  "createdBy": "c62d9930-b473-4b1d-adec-72fdda7cf599",
  "createdAt": "2026-11-13T15:10:00Z"
}
```

```json
{
  "tenant_id": "utec",
  "sk": "PROY#TERM#trm_01JCP0KWJDXF9GJ9YNTK2H84A9",
  "entidad": "PROYECCION",
  "termId": "trm_01JCP0KWJDXF9GJ9YNTK2H84A9",
  "name": "2026-2",
  "startsOn": "2026-08-17",
  "endsOn": "2026-12-12",
  "status": "ACTIVE",
  "recibidoDe": "lms.users",
  "version": 2
}
```

## Seed: 30.000 cursos

```bash
python scripts/poblar_datos.py --stage dev               # UTEC 15.000, UPC 10.000, PUCP 5.000
python scripts/poblar_datos.py --stage dev --escala 0.01 # 1 % para probar
```

- No escribe `EVENTO#`, así que no inunda el bus.
- Es idempotente: los ids son fijos y al correrlo de nuevo se reescriben los mismos ítems.
- Cada tenant recibe un periodo activo de seed (`trm_…SEED`).
- Los estados quedan en 60 % PUBLISHED, 30 % DRAFT y 10 % ARCHIVED, con 20 docentes de seed por tenant.
- Probado a escala completa con moto: 30.000 cursos en unos 30 s.

## Probar

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

37 pruebas con moto (sin AWS). Cubren:
- permisos por rol y por dueño, y aislamiento por tenant;
- validación del body y transiciones inválidas;
- código repetido y periodo no activo;
- paginación con filtros;
- el consumidor de `term.started` (duplicados y mensajes rotos);
- el seed;
- el recorrido completo: outbox → Stream → relay → bus → regla de ms-contenidos → su cola.

## Desplegar

Con las credenciales de la cuenta cargadas (en `nubeduca-backend`: `source scripts/cuenta.sh && cargar_cuenta`):

```bash
serverless deploy --stage dev
python scripts/poblar_datos.py --stage dev
```

El despliegue publica la URL de la API en SSM (`/nubeduca/<stage>/api/cursos/url`), de donde la lee el stack de Amplify. Para crear cursos reales hace falta un periodo iniciado en ms-usuarios (`POST /terms/{termId}/start`), que llega por el bus.
