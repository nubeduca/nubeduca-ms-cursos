"""ACA-03: cursos con ciclo DRAFT -> PUBLISHED -> ARCHIVED y sus eventos."""

import json
import re

import pytest

from api import cursos, salud
from consumidores import periodos
from lib import esquemas
from relay import stream_a_bus
from tests.conftest import (
    ADMIN,
    ALUMNO,
    DOCENTE,
    OTRO_DOCENTE,
    TERM,
    eventos,
    items,
    mensaje_term_started,
    peticion,
    registros_stream,
    token,
)

CURSO = {"termId": TERM, "code": "CS2032", "title": "Cloud Computing", "description": "Serverless y eventos"}
CRS = re.compile(r"^crs_[0-9A-HJKMNP-TV-Z]{26}$")
NO_EXISTE = "crs_01JCQRZHQT9F4WY2JYPDV546K7"


def activar(term_id=TERM, tenant="utec", event_id="01JCYZ385TV79Y8FX1CCDNVNRT"):
    lote = {"Records": [{"messageId": "m", "body": mensaje_term_started(term_id, tenant, event_id=event_id)}]}
    assert periodos.lambda_handler(lote, None) == {"batchItemFailures": []}


def llamar(metodo, headers=None, body=None, course_id=None, accion=None, query=None):
    headers = token() if headers is None else headers
    resource = "/courses" + ("/{courseId}" if course_id else "") + (f"/{accion}" if accion else "")
    params = {"courseId": course_id} if course_id else None
    return cursos.lambda_handler(peticion(metodo, headers, body, params, query, resource), None)


def crear(body=CURSO, headers=None):
    return llamar("POST", headers, body)


def nuevo_curso(headers=None):
    r = crear(headers=headers)
    assert r["statusCode"] == 201, r["body"]
    return json.loads(r["body"])["courseId"]


def cuerpo(r):
    return json.loads(r["body"])


# --- crear -------------------------------------------------------------------------

def test_salud_dice_ms_cursos():
    assert cuerpo(salud.lambda_handler({}, None))["service"] == "ms-cursos"


def test_crear_curso_en_periodo_activo_publica_course_created(aws):
    activar()
    r = crear()
    assert r["statusCode"] == 201
    curso = cuerpo(r)
    assert CRS.match(curso["courseId"])
    assert curso | {"courseId": "x", "createdAt": "t"} == {
        **CURSO, "courseId": "x", "createdAt": "t", "status": "DRAFT", "ownerId": DOCENTE}

    evs = eventos()
    assert len(evs) == 1
    ev = evs[0]
    assert ev["_detailType"] == "lms.courses.course.created.v1" and ev["_source"] == "lms.courses"
    assert ev["metadata"]["courseId"] == curso["courseId"] == ev["metadata"]["aggregateId"]
    assert ev["metadata"]["aggregateVersion"] == 1
    assert ev["data"] == {"termId": TERM, "code": "CS2032", "title": "Cloud Computing", "ownerId": DOCENTE}
    assert esquemas.errores(ev["_detailType"], {"metadata": ev["metadata"], "data": ev["data"]}) == []

    # Outbox -> Stream -> relay -> bus -> regla de ms-contenidos -> su cola.
    assert stream_a_bus.publicar(registros_stream()) == 1
    mensajes = aws["sqs"].receive_message(QueueUrl=aws["cola"], MaxNumberOfMessages=10)["Messages"]
    assert len(mensajes) == 1
    assert json.loads(mensajes[0]["Body"])["detail"]["metadata"]["courseId"] == curso["courseId"]


def test_crear_curso_en_periodo_no_activo_da_409(aws):
    r = crear()
    assert r["statusCode"] == 409
    assert cuerpo(r)["code"] == "PERIODO_NO_ACTIVO"
    activar(tenant="upc")  # activo en otro tenant no sirve
    assert crear()["statusCode"] == 409
    assert items("COURSE#") == [] and eventos() == []


def test_codigo_repetido_en_el_periodo_da_409(aws):
    activar()
    nuevo_curso()
    r = crear(headers=token(rol="ADMIN", user=ADMIN))
    assert r["statusCode"] == 409
    assert cuerpo(r)["code"] == "CURSO_DUPLICADO"
    assert len(items("COURSE#")) == 1 and len(eventos()) == 1


@pytest.mark.parametrize("cambio", [
    {"termId": "periodo-1"},
    {"code": "cs2032"},
    {"code": "X"},
    {"title": ""},
    {"title": "x" * 201},
    {"description": "x" * 2001},
    {"title": None},
])
def test_crear_valida_el_body(aws, cambio):
    activar()
    body = {k: v for k, v in {**CURSO, **cambio}.items() if v is not None}
    r = crear(body)
    assert r["statusCode"] == 400, cambio
    assert cuerpo(r)["code"] == "DATOS_INVALIDOS"


def test_crear_permisos(aws):
    activar()
    assert crear(headers={})["statusCode"] == 401
    assert crear(headers=token(rol="STUDENT", user=ALUMNO))["statusCode"] == 403


# --- leer y editar ---------------------------------------------------------------

def test_get_y_patch_del_dueno(aws):
    activar()
    cid = nuevo_curso()
    assert cuerpo(llamar("GET", course_id=cid))["title"] == "Cloud Computing"
    r = llamar("PATCH", body={"title": "Cloud Computing 2"}, course_id=cid)
    assert r["statusCode"] == 200
    assert cuerpo(r)["title"] == "Cloud Computing 2"
    assert cuerpo(r)["description"] == "Serverless y eventos"
    assert len(eventos()) == 1  # editar no publica


def test_patch_valida_y_respeta_permisos(aws):
    activar()
    cid = nuevo_curso()
    assert llamar("PATCH", body={}, course_id=cid)["statusCode"] == 400
    assert llamar("PATCH", body={"title": ""}, course_id=cid)["statusCode"] == 400
    assert llamar("PATCH", body={"code": "OTRO"}, course_id=cid)["statusCode"] == 400  # solo title y description
    otro = token(user=OTRO_DOCENTE)
    assert llamar("PATCH", otro, {"title": "x"}, cid)["statusCode"] == 403
    assert llamar("GET", otro, course_id=cid)["statusCode"] == 403
    admin = token(rol="ADMIN", user=ADMIN)
    assert llamar("PATCH", admin, {"title": "Por admin"}, cid)["statusCode"] == 200


def test_curso_de_otro_tenant_o_inexistente_da_404(aws):
    activar()
    cid = nuevo_curso()
    assert llamar("GET", token(tenant="upc"), course_id=cid)["statusCode"] == 404
    assert llamar("GET", course_id=NO_EXISTE)["statusCode"] == 404
    assert llamar("GET", course_id="no-es-un-id")["statusCode"] == 404
    assert cuerpo(llamar("GET", course_id=NO_EXISTE))["code"] == "CURSO_NO_ENCONTRADO"


def test_alumno_no_ve_cursos_hasta_matricularse(aws):
    """La matrícula es ACA-04: hasta entonces el alumno no ve ningún curso."""
    activar()
    cid = nuevo_curso()
    llamar("POST", course_id=cid, accion="publish")
    alumno = token(rol="STUDENT", user=ALUMNO)
    assert llamar("GET", alumno, course_id=cid)["statusCode"] == 404
    assert cuerpo(llamar("GET", alumno))["items"] == []


# --- ciclo de vida ---------------------------------------------------------------

def test_publicar_y_archivar(aws):
    activar()
    cid = nuevo_curso()
    r = llamar("POST", course_id=cid, accion="publish")
    assert r["statusCode"] == 200 and cuerpo(r)["status"] == "PUBLISHED"
    r = llamar("POST", course_id=cid, accion="archive")
    assert r["statusCode"] == 200 and cuerpo(r)["status"] == "ARCHIVED"

    evs = sorted(eventos(), key=lambda e: e["metadata"]["aggregateVersion"])
    assert [e["_detailType"] for e in evs] == [
        "lms.courses.course.created.v1", "lms.courses.course.published.v1", "lms.courses.course.archived.v1"]
    assert [e["metadata"]["aggregateVersion"] for e in evs] == [1, 2, 3]
    assert evs[1]["data"] == {"title": "Cloud Computing"}
    assert evs[2]["data"] == {}
    for e in evs:
        assert e["metadata"]["courseId"] == cid
        assert esquemas.errores(e["_detailType"], {"metadata": e["metadata"], "data": e["data"]}) == []


def test_transiciones_invalidas_dan_409_sin_evento(aws):
    activar()
    cid = nuevo_curso()
    llamar("POST", course_id=cid, accion="publish")
    r = llamar("POST", course_id=cid, accion="publish")
    assert r["statusCode"] == 409 and cuerpo(r)["code"] == "CURSO_NO_EN_BORRADOR"
    llamar("POST", course_id=cid, accion="archive")
    assert llamar("POST", course_id=cid, accion="archive")["statusCode"] == 409
    assert llamar("POST", course_id=cid, accion="publish")["statusCode"] == 409
    r = llamar("PATCH", body={"title": "x"}, course_id=cid)
    assert r["statusCode"] == 409 and cuerpo(r)["code"] == "CURSO_ARCHIVADO"
    assert len(eventos()) == 3


def test_un_borrador_se_puede_archivar(aws):
    activar()
    cid = nuevo_curso()
    assert cuerpo(llamar("POST", course_id=cid, accion="archive"))["status"] == "ARCHIVED"


def test_ciclo_respeta_permisos(aws):
    activar()
    cid = nuevo_curso()
    assert llamar("POST", token(user=OTRO_DOCENTE), course_id=cid, accion="publish")["statusCode"] == 403
    assert llamar("POST", token(rol="STUDENT", user=ALUMNO), course_id=cid, accion="archive")["statusCode"] == 403
    assert llamar("POST", course_id=NO_EXISTE, accion="publish")["statusCode"] == 404


# --- listar ------------------------------------------------------------------------

def test_listar_por_rol_filtros_y_paginas(aws):
    activar()
    otro_term = "trm_01JCQRZHQT9F4WY2JYPDV546K7"
    activar(otro_term, event_id="01JCYZ385TV79Y8FX1CCDNVNRV")
    propios = []
    for i in range(5):
        r = crear({**CURSO, "code": f"CS{i:04d}"})
        propios.append(cuerpo(r)["courseId"])
    crear({**CURSO, "code": "AJENO", "termId": otro_term}, headers=token(user=OTRO_DOCENTE))
    llamar("POST", course_id=propios[0], accion="publish")

    def todos(headers, query=None):
        vistos, nxt = [], None
        for _ in range(20):
            q = {"limit": "2", **(query or {})} | ({"nextToken": nxt} if nxt else {})
            r = llamar("GET", headers, query=q)
            assert r["statusCode"] == 200, r["body"]
            pagina = cuerpo(r)
            vistos += [c["courseId"] for c in pagina["items"]]
            nxt = pagina["nextToken"]
            if nxt is None:
                return vistos
        raise AssertionError("la paginación no terminó")

    assert set(todos(token())) == set(propios)  # TEACHER: los suyos
    assert len(todos(token(rol="ADMIN", user=ADMIN))) == 6  # ADMIN: todos
    assert todos(token(), {"status": "PUBLISHED"}) == [propios[0]]
    assert len(todos(token(rol="ADMIN", user=ADMIN), {"termId": otro_term})) == 1
    assert todos(token(tenant="upc")) == []


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "x"}, {"status": "BORRADO"},
                                   {"termId": "periodo"}, {"nextToken": "basura"}])
def test_listar_valida_parametros(aws, query):
    assert llamar("GET", query=query)["statusCode"] == 400


def test_listar_sin_token(aws):
    assert llamar("GET", headers={})["statusCode"] == 401
