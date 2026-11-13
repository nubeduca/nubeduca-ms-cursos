"""
Consumidor de lms.users.term.started.v1 (ms-usuarios, ACA-01).

Regla del bus -> cola nubeduca-<stage>-cursos-eventos (con DLQ) -> este Lambda.
Guarda la proyección PROY#TERM#<termId> con status ACTIVE: POST /courses solo acepta
periodos activos. El inbox descarta duplicados y eventos viejos.
"""

from api.comun import sk_periodo
from lib import inbox


def guardar_periodo(evento: inbox.Evento) -> list:
    datos = evento.data
    return [{
        "Put": {
            "Item": {
                "tenant_id": evento.tenant_id,
                "sk": sk_periodo(datos["termId"]),
                "entidad": "PROYECCION",
                "termId": datos["termId"],
                "name": datos.get("name"),
                "startsOn": datos.get("startsOn"),
                "endsOn": datos.get("endsOn"),
                "status": "ACTIVE",
                "recibidoDe": evento.source,
                "version": evento.aggregate_version,
            }
        }
    }]


REACCIONES = {
    "lms.users.term.started.v1": guardar_periodo,
}


def lambda_handler(event, context):
    return inbox.manejar_lote(event, REACCIONES)
