/* frontend/realtime/sse.js — 唯一拥有 EventSource 生命周期（Task 14）
 *
 * 约束：
 *   - 唯一调用 `new EventSource` 的模块。start() 幂等——已有活动连接时不重建，
 *     不允许多个并发连接。
 *   - 从 store 读取 lastEventTs 并发送 since 补发；成功接收事件后由 store
 *     用 Math.max 推进光标（陈旧事件由 store 拒绝）。
 *   - onerror 标记 disconnected、关闭失败源（禁用 EventSource 自带的、会复用
 *     原始 since=0 地址的无限自动重连），并启动**一个**有界轮询降级（单
 *     setInterval、MAX_POLLS 上限）；同时调度**至多一个**有界延迟重连，重连
 *     回调在读取 store 最新 getLastEventTs() 后调用 start()，因此新 URL 总是
 *     携带最新重放游标。重连总次数也有界（MAX_RECONNECTS），达到上限后停止
 *     调度，仅在成功 onopen 时复位计数。onopen 清空降级与未决重连；显式
 *     start() 取代未决的孤儿重连计时器。轮询采用在途栅栏：同一时间至多一个
 *     fetch 在途（pending 时不启动新的），并使用与 api/client.js 一致的有界
 *     AbortController 超时；stop() 关闭 source、取消未决重连、清空计时器与
 *     重连计数并中止在途轮询。陈旧源的回调（onopen/onerror）经 source 持有
 *     者比对成为无副作用 no-op。
 *   - 不直接操作 DOM；业务同步绝不依赖与整页刷新。所有层回调只在事件被 store
 *     采纳后触发——store.applySseEvent 返回 false（陈旧/未知）时不转发 onEvent、
 *     也不触发终态任务刷新。
 *   - 终态 task_update（succeeded/failed/cancelled/expired）刷新：通过在
 *     构造时注入的 client 调用 getTask(taskId) 回填 store（store 本身客户端
 *     无关，不 import client）。onEvent 回调把每条事件转发给应用组合层，
 *     用于其它轻量钩子。
 *   - 轮询降级使用 fetch 走同一 http/ 入口的解析语义（parseStatus），
 *     匹配既有 hub/static/app.js 的降级习惯。
 *
 * 导入 client.js 仅复用 resolveApiBaseUrl（apiBaseUrl 单一事实来源），
 * 不产生循环依赖；本模块自身不 import 浏览器 DOM 全局。
 */
import { parseSseEvent, parseStatus } from '../api/contracts.js';
import { resolveApiBaseUrl } from '../api/client.js';

/* -- 常量 ------------------------------------------------------------------ */

// store 终态刷新阈值（与 hub/domain/task.py 终态集合一致）
export var TERMINAL_TASK_STATES = ['succeeded', 'failed', 'cancelled',
                                   'expired'];
// 四个既有事件类型 + 两个允许名单内的 session 概要类型（Task 11）。
// 概要帧只携带 allowlisted 摘要字段——session_id / sequence / cursor /
// kind / capture_quality / status / ts——绝不内嵌 transcript/凭证。
var SSE_EVENT_TYPES = ['machine_update', 'task_update', 'task_log',
                       'fleet_event', 'session_update', 'session_summary'];
var POLL_INTERVAL_MS = 10000;   // 降级轮询间隔
var MAX_POLLS = 60;             // 有界降级：最多 60 次（约 10 分钟）
var POLL_TIMEOUT_MS = 15000;    // 单次降级轮询超时（与 api/client.js
                                // DEFAULT_TIMEOUT_MS 一致的有界 AbortController）
var RECONNECT_DELAY_MS = 3000;  // 传输错误后的有界延迟重连间隔：新 URL 从
                                // store 最新游标重建
export var MAX_RECONNECTS = MAX_POLLS; // 总重连次数上限：累计达到后停止调度
                                // （与 MAX_POLLS 同量级的有界降级习惯）；
                                // 仅在成功 onopen 后复位，stop() 清空计时器与计数
var NO_STORE_CACHE = 'no-store';

function stripTrailingSlash(base) {
  return String(base || '').replace(/\/+$/, '');
}

/**
 * 构建 SSE URL：`apiBaseUrl + "/stream?since=" + encodeURIComponent(lastEventTs)`。
 * 纯函数，便于 fixture 直接校验。
 */
export function buildStreamUrl(apiBaseUrl, lastEventTs) {
  var base = stripTrailingSlash(apiBaseUrl) || '/api';
  var since = (typeof lastEventTs === 'number' && isFinite(lastEventTs) &&
               lastEventTs > 0) ? lastEventTs : 0;
  return base + '/stream?since=' + encodeURIComponent(since);
}

/* -- SseClient -------------------------------------------------------------- */

/**
 * @param {FleetStore} store — DOM/客户端无关的状态存储（必须含
 *   applySseEvent / setConnection / getLastEventTs）。
 * @param {object} options
 *   @param {string} [options.apiBaseUrl]      同源 API 基址（缺省走
 *     resolveApiBaseUrl()）。
 *   @param {function} [options.onEvent]      每收到一条事件回调
 *    (type, parsedModel)；用于应用组合层轻量钩子。
 *   @param {object} [options.client]         注入的客户端（须含 getTask），
 *     用于终态任务刷新（Task 14 Step 3）；非必填。
 *   @param {number} [options.reconnectDelayMs] 传输错误后的延迟重连毫秒数；
 *     缺省 RECONNECT_DELAY_MS（仅测试注入小值用，生产保持默认）。
 */
export function SseClient(store, options) {
  if (!store || typeof store.applySseEvent !== 'function' ||
      typeof store.setConnection !== 'function' ||
      typeof store.getLastEventTs !== 'function') {
    throw new Error('SseClient 需要含 applySseEvent/setConnection/getLastEventTs 的 store');
  }
  options = options || {};
  this._store = store;
  this._base = (typeof options.apiBaseUrl === 'string' && options.apiBaseUrl)
    ? stripTrailingSlash(options.apiBaseUrl) : resolveApiBaseUrl();
  this._base = this._base || '/api';
  this._onEvent = (typeof options.onEvent === 'function') ? options.onEvent : null;
  this._client = (options.client && typeof options.client.getTask === 'function')
    ? options.client : null;
  this._source = null;
  this._pollTimer = null;
  this._pollCount = 0;
  this._pollPending = false;       // 在途轮询请求栅栏：阻止叠加 fetch
  this._pollController = null;     // 当前轮询的 AbortController（stop 时中止）
  this._reconnectTimer = null;     // 至多一个未决的延迟重连计时器
  this._reconnectCount = 0;        // 累计重连尝试（有界：成功 onopen 才复位）
  this._reconnectDelayMs = (typeof options.reconnectDelayMs === 'number' &&
    options.reconnectDelayMs > 0) ? options.reconnectDelayMs
    : RECONNECT_DELAY_MS;
}

SseClient.prototype._setConnection = function (status) {
  this._store.setConnection(status);
};

/** start() 幂等：已有活动连接时直接返回，绝不创建第二个 EventSource。 */
SseClient.prototype.start = function () {
  // 显式 start() 取代任何未决的延迟重连：清除无害孤儿计时器，避免其后
  // 再次触发 _doReconnect 重建（_doReconnect 内部的 start() 走到这里时
  // 计时器已被其回调置空，故这是一次干净的 no-op）。
  this._cancelReconnect();
  if (this._source) {
    return this;
  }
  var url = buildStreamUrl(this._base, this._store.getLastEventTs());
  var source = new EventSource(url);
  this._source = source;
  var self = this;

  SSE_EVENT_TYPES.forEach(function (name) {
    source.addEventListener(name, function (e) {
      self._handle(name, e);
    });
  });

  source.onopen = function () {
    // 只响应当前持有者：关闭后迟到的旧源 onopen 必须是 no-op
    if (self._source !== source) {
      return;
    }
    self._pollCount = 0;
    self._clearPoll();
    self._cancelReconnect(); // 连接恢复：取消未决的延迟重连，不再重建
    self._reconnectCount = 0; // 成功 onopen 才复位总重连计数（有界上限松绑）
    self._setConnection('open');
  };

  source.onerror = function () {
    // 只响应当前持有者：关闭后迟到的旧源 onerror 不得再触发重建
    if (self._source !== source) {
      return;
    }
    self._setConnection('disconnected');
    self._startPoll();               // 有界轮询降级（原有降级路径）
    // 传输错误：关闭失败源，禁用 EventSource 自带的对原始 since=0 地址
    // 的无限自动重连，改调度至多一个有界延迟重连；重连时读取 store
    // 最新 getLastEventTs() 重建 URL（携带最新重放游标）。
    self._closeAndScheduleReconnect();
  };

  return this;
};

SseClient.prototype._handle = function (name, e) {
  var data;
  try {
    data = JSON.parse(e.data);
  } catch (err) {
    return; // 有界：忽略畸形帧
  }
  var parsed;
  try {
    parsed = parseSseEvent(name, data);
  } catch (err) {
    return; // 契约校验失败：忽略
  }
  // 交给 store 应用（推进光标 / 拒绝陈旧事件 / 有界状态）。
  // applySseEvent 返回 false 表示陈旧或未知事件：不得转发外部 onEvent，
  // 也不得触发终态 getTask 刷新——只推进/保持游标。
  var applied = this._store.applySseEvent({ type: name, data: parsed });
  if (!applied) {
    return;
  }
  if (this._onEvent) {
    try {
      this._onEvent(name, parsed);
    } catch (err) {
      // 应用钩子失败不影响 SSE 生命周期
    }
  }
  if (name === 'task_update' &&
      TERMINAL_TASK_STATES.indexOf(parsed.state) !== -1) {
    this._refreshTerminalTask(parsed.task_id);
  }
};

/** 终态刷新：getTask(taskId) 回填 store；失败保持 SSE 已落定的状态。 */
SseClient.prototype._refreshTerminalTask = function (taskId) {
  var client = this._client;
  if (!client || typeof client.getTask !== 'function' ||
      typeof taskId !== 'string' || !taskId) {
    return;
  }
  var store = this._store;
  // 将调用放入 promise 链，避免 client.getTask 的同步异常逃出 SSE
  // 事件处理器；异步拒绝与同步失败采用同一有界降级路径。
  Promise.resolve()
    .then(function () {
      return client.getTask(taskId);
    })
    .then(function (res) {
      store.setTask(res);
    })
    .catch(function () {
      // 有界：终态状态已由 task_update 事件落定，刷新失败不折腾
    });
};

/* -- 有界轮询降级 ---------------------------------------------------------- */

SseClient.prototype._startPoll = function () {
  if (this._pollTimer) {
    return; // 只允许一个降级轮询
  }
  this._pollCount = 0;
  var self = this;
  var poll = function () {
    self._pollCount += 1;
    if (self._pollCount > MAX_POLLS) {
      self._clearPoll(); // 有界：达到上限停止降级，等待 EventSource 自愈
      return;
    }
    self._pollOnce();
  };
  this._pollTimer = setInterval(poll, POLL_INTERVAL_MS);
};

SseClient.prototype._pollOnce = function () {
  if (this._pollPending) {
    return; // 在途请求尚未结束：本轮跳过，绝不叠加第二个 fetch
  }
  this._pollPending = true;
  var controller = new AbortController();
  var timer = setTimeout(function () {
    controller.abort(); // 有界超时（POLL_TIMEOUT_MS），与 api/client 一致
  }, POLL_TIMEOUT_MS);
  this._pollController = controller;
  var self = this;
  fetch(this._base + '/status', {
    cache: NO_STORE_CACHE,
    signal: controller.signal,
  })
    .then(function (r) {
      if (r.status !== 200) {
        return null;
      }
      return r.json();
    })
    .then(function (data) {
      if (!data) {
        return;
      }
      try {
        self._store.setStatus(parseStatus(data));
      } catch (err) {
        // 契约校验失败：降级轮询保持静默
      }
    })
    .catch(function () {
      // 网络失败 / 超时中止：下一次轮询重试；EventSource 重连成功即停止
    })
    .then(function () {
      clearTimeout(timer);
      // 栅栏复位必须与轮次所有权绑定：仅当本轮的 controller 仍是当前
      // 持有者时才释放在途栅栏。stop()/restart 后迟到回收的旧轮询
      // 绝不能放掉新轮询的在途栅栏。
      if (self._pollController === controller) {
        self._pollController = null;
        self._pollPending = false;
      }
    });
};

SseClient.prototype._clearPoll = function () {
  if (this._pollTimer) {
    clearInterval(this._pollTimer);
    this._pollTimer = null;
  }
  if (this._pollController) {
    this._pollController.abort(); // 中止在途轮询请求
    this._pollController = null;
  }
  this._pollPending = false;
  this._pollCount = 0;
};

/* -- 有界延迟重连 ------------------------------------------------------------ */

/**
 * onerror 路径：关闭失败源并调度至多一个延迟重连。
 * 关闭后 this._source 为 null，EventSource 自带的对原始 since 地址的自动重连
 * 随即失效（其默认行为是复用初始 URL，会从陈旧游标整段重放）。
 */
SseClient.prototype._closeAndScheduleReconnect = function () {
  if (this._source) {
    this._source.close();
    this._source = null;
  }
  this._scheduleReconnect();
};

/** 调度延迟重连：总次数有界、且至多一个未决计时器（绝不叠加）。 */
SseClient.prototype._scheduleReconnect = function () {
  if (this._reconnectTimer) {
    return; // 已有未决计时器：绝不叠加第二个
  }
  if (this._reconnectCount >= MAX_RECONNECTS) {
    return; // 有界：达到总重连上限后停止调度，交由轮询降级 / 手动重启
  }
  this._reconnectCount += 1;      // 计数一次尝试；仅在成功 onopen 时复位
  var self = this;
  this._reconnectTimer = setTimeout(function () {
    self._reconnectTimer = null;
    self._doReconnect();
  }, this._reconnectDelayMs);
};

/**
 * 重连回调：start() 自身幂等且会读取 store.getLastEventTs() 重建 URL，
 * 因此新连接总是携带最新重放游标。_source 非空时跳过（已有活动连接或缺席
 * 于 stop 后）——绝不并发第二个 EventSource。
 */
SseClient.prototype._doReconnect = function () {
  if (this._source) {
    return;
  }
  this.start();
};

/** 取消未决的延迟重连（onopen 恢复 / stop 卸载时调用）。 */
SseClient.prototype._cancelReconnect = function () {
  if (this._reconnectTimer) {
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
  }
};

/** stop()：关闭 EventSource、取消未决重连、清空计时器与重连计数，标记 closed。 */
SseClient.prototype.stop = function () {
  if (this._source) {
    this._source.close();
    this._source = null;
  }
  this._cancelReconnect();
  this._reconnectCount = 0; // stop 卸载后计时器与计数一并清空
  this._clearPoll();
  this._setConnection('closed');
  return this;
};

SseClient.prototype.isOpen = function () {
  if (!this._source) {
    return false;
  }
  return this._source.readyState === EventSource.OPEN;
};
