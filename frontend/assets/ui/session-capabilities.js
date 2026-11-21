/* Pure presentation over validated public DTOs; no Agent/OS policy or I/O. */
const DELIVERY_LABELS = Object.freeze({
  native_resume_unsupported: '该会话不支持原生续聊',
  native_session_unbound: '尚未绑定可验证的原生会话',
  session_busy: '当前回合仍在执行，请等待完成',
  control_unavailable: '当前无可用消息投递能力',
});

export function sessionControls(session, features = {}) {
  const authorized = session?.control_capability === 'available' &&
    typeof session.session_id === 'string' && !!session.session_id;
  const actions = authorized && Array.isArray(session.allowed_actions) ? session.allowed_actions : [];
  const delivery = session?.message_delivery;
  return {
    actions,
    canControl: !!authorized && actions.length > 0,
    appendUserTurn: features.append_user_turn === true && actions.includes('append_user_turn') &&
      delivery?.available !== false,
    applyLocalProfile: features.apply_local_profile === true && actions.includes('apply_local_profile'),
    deliveryLabel: delivery?.available === false ? (DELIVERY_LABELS[delivery.reason] || '') : '',
  };
}
