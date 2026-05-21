# -*- coding: utf-8 -*-
"""
core.state_machine

State machine de ejecucion de una senal en un seguidor.
Verdad unica para: precedencia, terminalidad y mapeo a columnas.

Reglas:
  - Un estado TERMINAL NO puede sobrescribirse jamas.
  - Una transicion solo se aplica si NEW.precedence > CURRENT.precedence.
  - El seq monotonico por (signal_id, follower_id) es la 2da defensa
    contra ACKs out-of-order (Telegram no garantiza orden).
"""

# Precedencias: mas alta = mas avanzado.
# Estados terminales comparten 100 (todos son "fin de camino").
STATE_PRECEDENCE = {
    "enviada":          10,   # madre creo la fila, mando a Telegram
    "recibida":         20,   # seguidor leyo el mensaje
    "validada":         30,   # paso v, dedupe, freshness, formato
    "encolada":         40,   # entro a la queue del executor
    "ejecutando":       50,   # buy() en curso
    # ----- terminales -----
    "ejecutada":       100,
    "fallida_broker":  100,
    "fallida_sistema": 100,
    "timeout":         100,
    "duplicada":       100,
}

TERMINAL = frozenset({
    "ejecutada", "fallida_broker", "fallida_sistema", "timeout", "duplicada"
})

ALL_STATES = frozenset(STATE_PRECEDENCE)

# Columna timestamp por estado (en tabla executions)
STATE_TIMESTAMP_COL = {
    "enviada":         "enviada_at",
    "recibida":        "received_at",
    "validada":        "validated_at",
    "encolada":        "enqueued_at",
    "ejecutando":      "executing_at",
    "ejecutada":       "executed_at",
    "fallida_broker":  "finished_at",
    "fallida_sistema": "finished_at",
    "timeout":         "finished_at",
    "duplicada":       "finished_at",
}


def is_terminal(state):
    return state in TERMINAL


def can_transition(current, new):
    """True si NEW puede aplicarse sobre CURRENT.

    - current=None  => cualquier transicion valida
    - current TERMINAL => NUNCA se sobreescribe
    - en otro caso => solo si NEW.precedence > CURRENT.precedence
    """
    if new not in ALL_STATES:
        return False
    if current is None:
        return True
    if current in TERMINAL:
        return False
    return STATE_PRECEDENCE[new] > STATE_PRECEDENCE[current]


def can_apply(current_state, current_seq, new_state, new_seq):
    """Combina precedencia + seq.

    Aplicar SOLO si:
      - el seq nuevo es estrictamente mayor (anti out-of-order)
      - Y la transicion de estado es valida
    """
    if current_state is None:
        return True
    if new_seq <= (current_seq or 0):
        return False
    return can_transition(current_state, new_state)
