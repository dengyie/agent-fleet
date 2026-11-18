/* Display names only; the server owns current authority and allowed actions. */
const ADOPTION_LABELS = Object.freeze({
  pending: '纳管确认中', adopted: '已纳管', revoked: '纳管已撤销',
  missing: '纳管记录缺失', unavailable: '纳管服务不可用',
});

export function sessionAccessLabel(session) {
  return ADOPTION_LABELS[session.adoption_status] || (session.managed ? '受管采集' : '非受管采集');
}
