// Native use cases consume the public HTTP client. No platform Run/conversation
// copies, machine localhost access, CLI arguments or capability inference.
export function createNativeChatService(api) {
  async function readBoundTask(id, sessionId, signal) {
    const {task} = await api.getTask(id, {signal});
    if (task.task_id !== id || sessionId && task.session_id !== sessionId) throw new Error('任务结果与当前会话不一致');
    return task;
  }
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
    async readResults(id, sessionId, signal) {
      const task = await readBoundTask(id, sessionId, signal);
      const [diff, files] = await Promise.allSettled([
        task.result?.has_diff_patch ? api.getTaskDiff(id, {signal}) : Promise.resolve(null),
        api.listTaskFiles(id, {signal}),
      ]);
      if ((diff.status === 'fulfilled' && diff.value && diff.value.task_id !== id) ||
          (files.status === 'fulfilled' && files.value.task_id !== id)) throw new Error('任务快照标识不一致');
      // A task may be retried while its snapshots are loading. Do not publish
      // snapshots from a newer attempt bound to a different conversation.
      await readBoundTask(id, sessionId, signal);
      return {task, diff:diff.status === 'fulfilled' ? diff.value : null,
        files:files.status === 'fulfilled' ? files.value.files : null,
        diffError:diff.status === 'rejected' ? diff.reason : null,
        filesError:files.status === 'rejected' ? files.reason : null};
    },
    async readResultFile(id, sessionId, path, signal) {
      await readBoundTask(id, sessionId, signal);
      const file = await api.getTaskFile(id, path, {signal});
      if (file.path !== path) throw new Error('文件快照路径不一致');
      await readBoundTask(id, sessionId, signal);
      return file;
    },
    async readContinuationResults(sessionId, {before, signal} = {}) {
      const page = await api.getContinuationResults(sessionId, {before, signal});
      if (page.session_id !== sessionId || page.results.some(row => row.session_id !== sessionId)) throw new Error('续聊成果与当前会话不一致');
      return page;
    },
    async readContinuationResult(sessionId, commandId, signal) {
      const value = await api.getContinuationResult(sessionId, commandId, {signal});
      if (value.session_id !== sessionId || value.result.session_id !== sessionId || value.result.command_id !== commandId) throw new Error('续聊成果与当前操作不一致');
      return value.result;
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
        if (value.provider_account_id !== undefined && (value.agent_type !== 'agy' || !/^pa_[a-f0-9]{32}$/.test(value.provider_account_id))) return null;
        return Object.freeze({machine:value.machine, project:value.project, agent_type:value.agent_type,
          instruction:value.instruction, client_token:value.client_token,
          ...(value.provider_account_id ? {provider_account_id:value.provider_account_id} : {})});
      } catch (_) { return null; }
    },
    write(operation) { try { storage?.setItem(key, JSON.stringify(operation)); } catch (_) {} },
    clear() { try { storage?.removeItem(key); } catch (_) {} },
  };
}
