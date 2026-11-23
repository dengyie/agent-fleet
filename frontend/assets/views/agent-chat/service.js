// Native use cases consume the public HTTP client. No platform Run/conversation
// copies, machine localhost access, CLI arguments or capability inference.
export function createNativeChatService(api) {
  return {
    async readSession(id, {position = {tail: true}, signal} = {}) {
      const jobs = [api.getSession(id, {signal})];
      jobs.push(api.getSessionEvents(id, {...position, limit: 100,
        includeSource: false, signal}));
      const [detail, transcript] = await Promise.allSettled(jobs);
      const session = detail.status === 'fulfilled' ? detail.value.session : null;
      const page = transcript?.status === 'fulfilled' ? transcript.value : null;
      if (session && session.session_id !== id) throw new Error('会话响应与请求标识不一致');
      if (page?.events.some(event => event.session_id !== id)) throw new Error('事件不属于当前会话');
      return {session, page, authorityVerified: detail.status === 'fulfilled',
        error: detail.status === 'rejected' ? detail.reason : transcript?.status === 'rejected' ? transcript.reason : null};
    },
    async readTask(id, signal) {
      const {task} = await api.getTask(id, {signal});
      if (task.task_id !== id) throw new Error('任务响应与请求标识不一致');
      return task;
    },
    create: input => api.createTask(input),
    send: (id, operation) => api.sendSessionMessage(id, operation.text, operation.client_token),
    control: (id, action, payload) => api.controlSession(id, action, 'operator_requested', payload),
    async receipt(id, sessionId, signal) {
      const receipt = await api.getControlReceipt(id, {signal});
      if (receipt.command_id !== id || receipt.session_id !== sessionId) throw new Error('命令回执与当前会话不一致');
      return receipt;
    },
  };
}

// Per-tab recovery journal. It is scoped to the API and authenticated account;
// restoration only renders a card. A user action is required to replay a POST.
export function createMessageJournal(storage, key) {
  return {
    read() {
      try {
        const value = JSON.parse(storage?.getItem(key) || 'null');
        if (typeof value?.text !== 'string' || !value.text.trim() || [...value.text].length > 2000 ||
            typeof value.client_token !== 'string' || !/^[\w-]{1,128}$/.test(value.client_token)) return null;
        return {text: value.text, client_token: value.client_token,
          ...(typeof value.command_id === 'string' && value.command_id ? {command_id: value.command_id} : {})};
      } catch (_) { return null; }
    },
    write(operation) {
      try { storage?.setItem(key, JSON.stringify({text: operation.text, client_token: operation.client_token, command_id: operation.command_id})); }
      catch (_) { /* The in-memory operation remains recoverable in this view. */ }
    },
    clear() { try { storage?.removeItem(key); } catch (_) {} },
  };
}

export function createTaskJournal(storage, key) {
  return {
    read() {
      try {
        const value = JSON.parse(storage?.getItem(key) || 'null');
        if (!value || !['machine', 'project', 'agent_type', 'instruction', 'client_token'].every(field => typeof value[field] === 'string' && value[field].trim()) ||
            [...value.instruction].length > 2000 || !/^[\w-]{1,128}$/.test(value.client_token)) return null;
        return Object.freeze({machine:value.machine, project:value.project, agent_type:value.agent_type,
          instruction:value.instruction, client_token:value.client_token});
      } catch (_) { return null; }
    },
    write(operation) { try { storage?.setItem(key, JSON.stringify(operation)); } catch (_) {} },
    clear() { try { storage?.removeItem(key); } catch (_) {} },
  };
}
