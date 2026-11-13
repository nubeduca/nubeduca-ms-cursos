"""ACA-03: secciones de un curso."""

import json
import re

from api import cursos, secciones
from consumidores import periodos
from tests.conftest import ADMIN, ALUMNO, OTRO_DOCENTE, TERM, items, mensaje_term_started, peticion, token

SEC = re.compile(r"^sec_[0-9A-HJKMNP-TV-Z]{26}$")


def _curso():
    periodos.lambda_handler({"Records": [{"messageId": "m", "body": mensaje_term_started()}]}, None)
    r = cursos.lambda_handler(peticion("POST", token(), {"termId": TERM, "code": "CS2032", "title": "Cloud"},
                                       resource="/courses"), None)
    return json.loads(r["body"])["courseId"]


def _secciones(metodo, course_id, headers=None, body=None):
    headers = token() if headers is None else headers
    return secciones.lambda_handler(
        peticion(metodo, headers, body, {"courseId": course_id}, resource="/courses/{courseId}/sections"), None)


def test_crear_y_listar_secciones(aws):
    cid = _curso()
    r = _secciones("POST", cid, body={"name": "Sección 1"})
    assert r["statusCode"] == 201
    sec = json.loads(r["body"])
    assert SEC.match(sec["sectionId"]) and sec["name"] == "Sección 1"
    _secciones("POST", cid, headers=token(rol="ADMIN", user=ADMIN), body={"name": "Sección 2"})
    lista = json.loads(_secciones("GET", cid)["body"])["items"]
    assert [s["name"] for s in lista] == ["Sección 1", "Sección 2"]
    assert len(items("COURSE#")) == 1  # las secciones no se mezclan con los cursos


def test_secciones_validan_y_respetan_permisos(aws):
    cid = _curso()
    assert _secciones("POST", cid, body={"name": ""})["statusCode"] == 400
    assert _secciones("POST", cid, body={"name": "x" * 41})["statusCode"] == 400
    assert _secciones("POST", cid, headers=token(user=OTRO_DOCENTE), body={"name": "x"})["statusCode"] == 403
    assert _secciones("POST", cid, headers=token(rol="STUDENT", user=ALUMNO), body={"name": "x"})["statusCode"] == 403
    assert _secciones("GET", cid, headers={})["statusCode"] == 401
    assert _secciones("GET", "crs_01JCQRZHQT9F4WY2JYPDV546K7")["statusCode"] == 404
    assert _secciones("GET", cid, headers=token(tenant="upc"))["statusCode"] == 404


def test_curso_archivado_no_acepta_secciones(aws):
    cid = _curso()
    cursos.lambda_handler(peticion("POST", token(), path_params={"courseId": cid},
                                   resource="/courses/{courseId}/archive"), None)
    r = _secciones("POST", cid, body={"name": "Tarde"})
    assert r["statusCode"] == 409
    assert json.loads(r["body"])["code"] == "CURSO_ARCHIVADO"
