import * as platformApi from '../../api/platform.js';

// The view depends on this use-case surface, not on the platform transport.
export function createAssistantService(api = platformApi) {
  return {
    getDefaults: (...args) => api.getPlatformDefaults(...args),
    getConversations: (...args) => api.getConversations(...args),
    getArtifacts: (...args) => api.getPlatformArtifacts(...args),
    getArtifactPreview: (...args) => api.getPlatformArtifactPreview(...args),
    getMemories: (...args) => api.getPlatformMemories(...args),
    searchMemories: (...args) => api.searchPlatformMemories(...args),
    createConversation: (...args) => api.createConversation(...args),
    getConversation: (...args) => api.getConversation(...args),
    appendTurn: (...args) => api.appendConversationTurn(...args),
    getRun: (...args) => api.getRun(...args),
    getRunEvents: (...args) => api.getRunEvents(...args),
    cancelRun: (...args) => api.cancelRun(...args),
    getUnknownCommands: (...args) => api.getUnknownCommands(...args),
    createLegacyTask: (...args) => api.createPlatformLegacyTask(...args),
    getLegacyTask: (...args) => api.getPlatformLegacyTask(...args),
    submitCommandReconcile: (...args) => api.submitCommandReconcile(...args),
    requestCommandPostcheck: (...args) => api.requestCommandPostcheck(...args),
    getCommandPostcheck: (...args) => api.getCommandPostcheck(...args),
    createExecutionWindow: (...args) => api.createExecutionWindow(...args),
    listExecutionWindows: (...args) => api.listExecutionWindows(...args),
    getExecutionWindow: (...args) => api.getExecutionWindow(...args),
    reconnectExecutionWindow: (...args) => api.reconnectExecutionWindow(...args),
    attachExecutionWindow: (...args) => api.attachExecutionWindow(...args),
    acquireExecutionWindowWriter: (...args) => api.acquireExecutionWindowWriter(...args),
    renewExecutionWindowWriter: (...args) => api.renewExecutionWindowWriter(...args),
    releaseExecutionWindowWriter: (...args) => api.releaseExecutionWindowWriter(...args),
    closeExecutionWindow: (...args) => api.closeExecutionWindow(...args),
    getExecutionWindowEvents: (...args) => api.getExecutionWindowEvents(...args),
    getArtifactContentUrl: (...args) => api.getPlatformArtifactContentUrl(...args),
    getExecutionWindowFrameUrl: (...args) => api.getExecutionWindowFrameUrl(...args),
    getWorkflowTemplates: (...args) => api.getWorkflowTemplates(...args),
    createWorkflow: (...args) => api.createWorkflow(...args),
    listWorkflows: (...args) => api.listWorkflows(...args),
    getWorkflow: (...args) => api.getWorkflow(...args),
    getWorkflowEvents: (...args) => api.getWorkflowEvents(...args),
    controlWorkflow: (...args) => api.controlWorkflow(...args),
    controlWorkflowStep: (...args) => api.controlWorkflowStep(...args),
    acceptWorkflow: (...args) => api.acceptWorkflow(...args),
    createTurnRequest({text, clientToken, modelProfileId, memoryContext}) {
      const payload = {text: String(text || '').trim(), client_token: clientToken};
      if (modelProfileId) payload.overrides = {model_profile_id: modelProfileId};
      if (memoryContext && Array.isArray(memoryContext.memoryIds) && memoryContext.memoryIds.length) {
        payload.memory_context = {
          enabled: true,
          memory_ids: memoryContext.memoryIds.slice(),
          revisions: Object.assign({}, memoryContext.revisions),
          max_items: memoryContext.maxItems,
          max_bytes: memoryContext.maxBytes
        };
      }
      return payload;
    }
  };
}
