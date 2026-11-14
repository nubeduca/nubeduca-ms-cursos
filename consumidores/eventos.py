"""
Lambda de la cola nubeduca-<stage>-cursos-eventos: junta las reacciones de cada evento
que ms-cursos consume (regla ReglaConsumidor del serverless.yml).

- lms.users.term.started.v1       -> consumidores/periodos.py (ACA-03)
- lms.users.user.provisioned.v1   -> consumidores/usuarios.py (ACA-04)
"""

from consumidores import periodos, usuarios
from lib import inbox

REACCIONES = {**periodos.REACCIONES, **usuarios.REACCIONES}


def lambda_handler(event, context):
    return inbox.manejar_lote(event, REACCIONES)
