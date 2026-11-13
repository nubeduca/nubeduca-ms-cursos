"""Consumidor de lms.users.term.started.v1: ms-cursos solo crea cursos en periodos activos."""

import json

from consumidores import periodos
from lib import esquemas, inbox
from tests.conftest import EJEMPLOS, TENANT, TERM, items, mensaje_term_started


def test_ejemplo_oficial_de_term_started_cumple_su_esquema():
    evento = json.loads((EJEMPLOS / "lms.users.term.started.v1.json").read_text(encoding="utf-8"))
    assert esquemas.errores(evento["detail-type"], evento["detail"]) == []


def test_guarda_el_periodo_activo(aws):
    lote = {"Records": [{"messageId": "m1", "body": mensaje_term_started()}]}
    assert periodos.lambda_handler(lote, None) == {"batchItemFailures": []}
    proy = items(f"PROY#TERM#{TERM}")
    assert len(proy) == 1
    assert proy[0]["status"] == "ACTIVE"
    assert proy[0]["name"] == "2026-2"
    assert proy[0]["startsOn"] == "2026-08-17"


def test_duplicado_y_otro_tenant(aws):
    cuerpo = mensaje_term_started()
    assert inbox.procesar(inbox.leer_mensaje(cuerpo), periodos.REACCIONES) == "PROCESADO"
    assert inbox.procesar(inbox.leer_mensaje(cuerpo), periodos.REACCIONES) == "DUPLICADO"
    assert len(items("PROY#TERM#")) == 1
    assert items("PROY#TERM#", "upc") == []  # el periodo es del tenant del evento
    assert TENANT == "utec"


def test_un_mensaje_roto_no_frena_el_lote(aws):
    lote = {"Records": [{"messageId": "roto", "body": "{no es json"},
                        {"messageId": "ok", "body": mensaje_term_started()}]}
    assert periodos.lambda_handler(lote, None) == {"batchItemFailures": [{"itemIdentifier": "roto"}]}
    assert len(items("PROY#TERM#")) == 1
