"""Readable public activity from known tool fields, separate from execution."""
import shlex


def preview(value, maximum=2048):
    if not isinstance(value, str):
        return ''
    raw = value.encode('utf-8')
    return raw[:maximum].decode('utf-8', errors='ignore') + ('\n…（预览已截断）' if len(raw) > maximum else '')


def _service_action_result(result):
    states = {
        'command': ('命令状态', 'status', {
            'queued': '等待执行', 'leased': '已领取', 'accepted': '已接收',
            'running': '执行中', 'succeeded': '已完成', 'failed': '失败',
            'unknown': '结果未知', 'expired': '已过期',
        }),
        'grant': ('审批状态', 'state', {
            'pending': '等待审批', 'approved': '已批准', 'rejected': '已拒绝',
            'consuming': '正在消费', 'consumed': '已消费（执行结果需查询原命令）',
            'expired': '已过期',
        }),
        'result': ('服务状态', 'state', {
            'healthy': '健康', 'unhealthy': '不健康', 'degraded': '降级',
            'unsupported': '不支持', 'unknown': '未知', 'succeeded': '操作成功',
            'failed': '操作失败',
        }),
    }
    lines = []
    for key, (label, field, labels) in states.items():
        item = result.get(key)
        value = item.get(field) if isinstance(item, dict) else None
        if isinstance(value, str) and value in labels:
            lines.append(label + '：' + labels[value])
    return '\n'.join(lines)


def tool_activity(tool, arguments, receipt=None):
    arguments = arguments if isinstance(arguments, dict) else {}
    titles = {'workspace.exec': '执行命令', 'workspace.read': '读取文件',
              'workspace.list': '列出目录', 'workspace.write': '写入文件',
              'workspace.digest': '计算文件摘要', 'workspace.artifact': '保存产物',
              'platform.get_system_info': '查看项目系统总览',
              'platform.admin_inspect': '查询项目管理信息', 'platform.admin_mutate': '管理项目配置',
              'platform.get_defaults': '查看默认配置', 'fleet.list_services': '列出服务',
              'service.get_health': '查看服务健康', 'service.read_logs': '读取服务日志',
              'service.request_action': '申请服务操作', 'service.get_action': '查看服务操作',
              'service.get_approval': '查看服务审批',
              'fleet.list_machines': '查看 VM 清单', 'session.list': '查找远端 Agent 会话',
              'session.get': '查看远端 Agent 会话', 'session.read_events': '读取远端会话记录',
              'session.send_message': '向远端 Agent 发送任务', 'session.get_command': '核对远端控制回执',
              'conversation.list': '查找会话', 'conversation.rename': '重命名会话',
              'browser.open': '打开网页', 'browser.close': '关闭网页'}
    title = titles.get(tool, '调用工具 · ' + tool)
    if tool in {'platform.admin_inspect', 'platform.admin_mutate'}:
        title += ' · ' + preview(arguments.get('operation'), 96)
    if tool == 'session.control':
        title = {'pause_session': '暂停远端 Agent', 'resume_session': '恢复远端 Agent 进程',
                 'terminate_session': '终止远端 Agent', 'quarantine_session': '隔离远端 Agent',
                 'cancel_attempt': '取消远端 Agent 执行'}.get(arguments.get('action'), '控制远端 Agent')
    if tool == 'service.request_action':
        action = arguments.get('action')
        if action == 'inspect':
            title = '检查服务'
        elif action == 'restart':
            title = '申请重启服务'
    source = ''
    if tool == 'workspace.exec':
        argv = arguments.get('argv')
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            source = preview(shlex.join(argv), 4096)
    elif tool.startswith('workspace.'):
        source = preview(arguments.get('path') or '.', 1024)
    elif tool in {'service.get_health', 'service.read_logs', 'service.request_action', 'service.get_action'}:
        source = preview(arguments.get('service_id'), 512)
    elif tool == 'service.get_approval':
        source = preview(arguments.get('grant_id'), 512)
    elif tool.startswith('session.'):
        source = preview(arguments.get('session_id') or arguments.get('machine_id'), 512)
    activity = {'title': preview(title + (' · ' + source.replace('\n', ' ') if source else ''), 384)}
    if source:
        activity['input'] = source
        if tool == 'session.send_message':
            activity['input'] += '\n' + preview(arguments.get('text'), 1600)
    if receipt is not None:
        result = receipt.result if isinstance(receipt.result, dict) else {}
        if tool == 'workspace.exec':
            text = ('退出码：' + str(result['returncode']) + '\n') if 'returncode' in result else ''
            text += preview(result.get('output'))
        elif tool == 'workspace.read':
            text = preview(result.get('content'))
        elif tool == 'workspace.list' and isinstance(result.get('entries'), list):
            text = preview('\n'.join(str(item.get('name', '')) + ('/' if item.get('kind') == 'directory' else '')
                                     for item in result['entries'] if isinstance(item, dict)))
            text = '目录为空' if not result['entries'] else text
        elif tool == 'workspace.write' and 'size' in result:
            text = '已写入 ' + str(result['size']) + ' 字节'
        elif tool == 'workspace.digest':
            text = preview(result.get('sha256'))
        elif tool in {'service.request_action', 'service.get_action', 'service.get_approval'}:
            text = _service_action_result(result)
        elif tool == 'service.read_logs':
            logs = result.get('logs', result)
            text = preview(logs.get('text')) if isinstance(logs, dict) else ''
        elif tool in {'session.control', 'session.send_message', 'session.get_command'}:
            command = result.get('receipt', result)
            status = command.get('status', '')
            labels = {'pending': '已入队，等待节点回执', 'queued': '等待节点领取',
                      'delivered': '节点已领取，等待执行回执', 'accepted': '节点已接收',
                      'executing': '节点执行中', 'succeeded': '节点确认动作成功',
                      'already_finished': '目标已经结束', 'failed': '动作失败',
                      'rejected': '节点拒绝', 'expired': '命令已过期', 'unknown': '执行结果未知'}
            text = labels.get(status, preview(status, 64))
            if command.get('command_id'):
                text += '\n命令：' + preview(command['command_id'], 128)
            if command.get('reason'):
                text += '\n原因：' + preview(command['reason'], 160)
        elif tool == 'session.read_events':
            text = '已读取 ' + str(len(result.get('events', []))) + ' 条远端会话记录'
        elif tool == 'session.list':
            text = '找到 ' + str(len(result.get('sessions', []))) + ' 个远端 Agent 会话'
        elif tool in {'platform.admin_inspect', 'platform.admin_mutate', 'platform.get_system_info'}:
            text = '管理服务已返回结果'
            if result.get('restart_required'):
                text += '；配置已保存，需要重启后生效'
            if result.get('removed'):
                text = '节点登记已移除（云 VM 未销毁）'
        elif tool == 'fleet.list_machines':
            text = '已读取 ' + str(len(result.get('machines', []))) + ' 台机器的状态'
        else:
            text = ''
        if text:
            activity['output'] = preview(text + ('\n（工具输出已截断）' if receipt.truncated else ''), 3072)
    return activity
