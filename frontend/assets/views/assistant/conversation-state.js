export function createConversationState() {
  return {messages: new Map(), runs: new Map(), history: null, contextWindow: null};
}

export function mergeConversationPage(state, conversation, earlier) {
  const page = conversation && typeof conversation === 'object' ? conversation : {};
  const incomingMessages = Array.isArray(page.messages) ? page.messages : [];
  const incomingRuns = Array.isArray(page.runs) ? page.runs : [];
  const incomingUsers = incomingMessages.filter(message => message.role === 'user');
  const existingSequences = Array.from(state.messages.values())
    .map(message => message.turn_sequence)
    .filter(Number.isFinite);
  const maxSequence = existingSequences.reduce((max, value) => Math.max(max, value), -Infinity);
  const hasGap = !earlier && incomingUsers.length && existingSequences.length &&
    incomingUsers[0].turn_sequence > maxSequence + 1;
  const messages = hasGap ? new Map() : new Map(state.messages);
  const runs = hasGap ? new Map() : new Map(state.runs);

  incomingMessages.forEach(message => messages.set(message.message_id, message));
  incomingRuns.forEach(run => runs.set(run.run_id, run));

  const orderedMessages = Array.from(messages.values()).sort((a, b) => {
    if (!Number.isFinite(a.turn_sequence) || !Number.isFinite(b.turn_sequence)) return 0;
    return (a.turn_sequence - b.turn_sequence) || (a.role === b.role ? 0 : a.role === 'user' ? -1 : 1);
  });
  const sequence = new Map(orderedMessages.map(message => [message.message_id, message.turn_sequence]));
  const orderedRuns = Array.from(runs.values()).sort((a, b) =>
    sequence.get(a.trigger_message_id) - sequence.get(b.trigger_message_id));

  return {
    messages,
    runs,
    history: hasGap ? (page.history || null) : page.history && (earlier || !state.history) ? page.history : state.history,
    contextWindow: hasGap ? (page.context_window || null) : page.context_window || state.contextWindow,
    orderedMessages,
    orderedRuns
  };
}
