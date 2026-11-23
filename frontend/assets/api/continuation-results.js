// Bounded public per-turn DTOs; no vendor identity, paths or command payloads.
export function parseContinuationResults(data, {detail = false} = {}) {
  function invalid() { throw new Error('续聊成果响应不合法'); }
  const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value);
  const cursor = (value, empty = false) => typeof value === 'string' && /^(0|[1-9]\d{0,18})$/.test(value) &&
    (empty || value !== '0') && BigInt(value) <= 9223372036854775807n;
  if (!data || typeof data !== 'object' || !identity(data.session_id)) invalid();
  function row(value, full) {
    if (!value || typeof value !== 'object') invalid();
    const result = {};
    for (const key of ['command_id', 'session_id', 'task_id', 'cursor', 'execution_state', 'collection_state', 'collection_reason', 'finished_at', 'diff_stat']) {
      if (typeof value[key] !== 'string' || value[key].length > (key === 'diff_stat' ? 5120 : 160)) invalid();
      result[key] = value[key];
    }
    if (!['succeeded', 'failed', 'cancelled', 'unknown'].includes(result.execution_state) ||
        !['complete', 'partial', 'unavailable'].includes(result.collection_state) ||
        !['', 'result_unavailable', 'result_too_large', 'test_summary_unavailable'].includes(result.collection_reason) ||
        !['command_id', 'session_id', 'task_id'].every(key => identity(result[key])) ||
        !cursor(result.cursor) || new TextEncoder().encode(result.diff_stat).length > 5120 ||
        !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(result.finished_at) || !Number.isFinite(Date.parse(result.finished_at))) invalid();
    if (!Number.isInteger(value.exit_code) || value.exit_code < -(2**31) || value.exit_code >= 2**31 ||
        !Number.isFinite(value.duration_s) || value.duration_s < 0 || value.duration_s > 365 * 86400) invalid();
    result.exit_code = value.exit_code; result.duration_s = value.duration_s;
    for (const key of ['diff_truncated', 'stat_truncated', 'redacted']) {
      if (typeof value[key] !== 'boolean') invalid();
      result[key] = value[key];
    }
    if (full) {
      if (typeof value.diff_patch !== 'string' || new TextEncoder().encode(value.diff_patch).length > 102400) invalid();
      result.diff_patch = value.diff_patch;
      result.test_summary = null;
      if (value.test_summary != null) {
        const summary = value.test_summary;
        if (!summary || typeof summary !== 'object' || Array.isArray(summary)) invalid();
        result.test_summary = {};
        for (const key of ['passed', 'failed', 'skipped', 'errors']) if (key in summary) {
          if (!Number.isSafeInteger(summary[key]) || summary[key] < 0) invalid();
          result.test_summary[key] = summary[key];
        }
        if ('framework' in summary) {
          if (!['pytest', 'unittest', 'unknown'].includes(summary.framework)) invalid();
          result.test_summary.framework = summary.framework;
        }
        if ('duration_s' in summary) {
          if (!Number.isFinite(summary.duration_s) || summary.duration_s < 0) invalid();
          result.test_summary.duration_s = summary.duration_s;
        }
        if ('failed_names' in summary) {
          if (!Array.isArray(summary.failed_names) || summary.failed_names.length > 20 || summary.failed_names.some(x => typeof x !== 'string' || x.length > 200)) invalid();
          result.test_summary.failed_names = [...summary.failed_names];
        }
      }
    }
    return result;
  }
  if (detail) return {session_id: data.session_id, result: row(data.result, true)};
  if (!Array.isArray(data.results) || data.results.length > 50 || typeof data.has_more !== 'boolean' ||
      !cursor(data.before_cursor, true)) invalid();
  return {session_id: data.session_id, results: data.results.map(x => row(x, false)),
    has_more: data.has_more, before_cursor: data.before_cursor};
}
