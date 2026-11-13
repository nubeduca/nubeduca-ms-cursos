"""Seed de ACA-03: 30.000 cursos (UTEC 15.000, UPC 10.000, PUCP 5.000) sin inundar el bus."""

import json

from api import comun, cursos
from lib import esquemas
from scripts import poblar_datos
from tests.conftest import ADMIN, items, peticion, token


def test_reparto_oficial():
    assert poblar_datos.REPARTO == {"utec": 15000, "upc": 10000, "pucp": 5000}
    assert sum(poblar_datos.REPARTO.values()) == 30000


def test_carga_el_reparto_sin_eventos_y_es_idempotente(aws):
    reparto = {"utec": 30, "upc": 20, "pucp": 10}
    assert poblar_datos.poblar(reparto) == reparto
    assert poblar_datos.poblar(reparto) == reparto  # correrlo de nuevo no duplica
    for tenant, cantidad in reparto.items():
        assert len(items("COURSE#", tenant)) == cantidad
        assert len(items("COURSECODE#", tenant)) == cantidad
        assert items("EVENTO#", tenant) == []  # el seed no publica al bus
        periodo = items("PROY#TERM#", tenant)
        assert len(periodo) == 1 and periodo[0]["status"] == "ACTIVE"


def test_los_cursos_del_seed_cumplen_el_contrato(aws):
    poblar_datos.poblar({"utec": 12, "upc": 0, "pucp": 0})
    estados = {c["status"] for c in items("COURSE#")}
    assert estados == {"DRAFT", "PUBLISHED", "ARCHIVED"}
    for item in items("COURSE#"):
        curso = comun.a_curso(item)
        assert comun.COURSE_ID.match(curso["courseId"]) and comun.TERM_ID.match(curso["termId"])
        data = {k: curso[k] for k in ("termId", "code", "title", "ownerId")}
        meta = {"eventId": "01JCB7FV64BFKS3ZX0NJD0BXNJ", "tenantId": "utec", "occurredAt": curso["createdAt"],
                "aggregateType": "course", "aggregateId": curso["courseId"], "aggregateVersion": 1,
                "actorId": curso["ownerId"], "courseId": curso["courseId"]}
        assert esquemas.errores("lms.courses.course.created.v1", {"metadata": meta, "data": data}) == []
    r = cursos.lambda_handler(peticion("GET", token(rol="ADMIN", user=ADMIN), query={"limit": "100"},
                                       resource="/courses"), None)
    assert len(json.loads(r["body"])["items"]) == 12
