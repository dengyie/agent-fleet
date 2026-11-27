"""Resolve one uncertain native append while the existing spool owner is held."""


def recover_append(capture, state, save, *, error):
    pending = state['pending']
    if pending is None:
        return state
    spool, sequence = capture.bridge.spool, pending['sequence']
    info = spool.status()
    if (info['last_sequence'] not in {sequence - 1, sequence}
            or info['ack_sequence'] >= sequence):
        raise ValueError(error)
    accepted = False
    if info['last_sequence'] == sequence:
        rows = spool.read_after(sequence - 1, 1, 65536)
        if not rows or rows[0]['sequence'] != sequence:
            raise ValueError(error)
        accepted = rows[0]['event_id'] == pending['event_id']
        if not accepted and rows[0]['kind'] != 'capture_gap':
            raise ValueError(error)
    if accepted and pending['event_id'] not in state['receipts']:
        state['receipts'].append(pending['event_id'])
    state['pending'] = None
    save(state)
    return state
