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
              'platform.get_defaults': '查看默认配置', 'fleet.list_services': '列出服务',
              'service.get_health': '查看服务健康', 'service.read_logs': '读取服务日志',
              'service.request_action': '申请服务操作', 'service.get_action': '查看服务操作',
              'service.get_approval': '查看服务审批',
              'conversation.list': '查找会话', 'conversation.rename': '重命名会话',
              'browser.open': '打开网页', 'browser.close': '关闭网页'}
    title = titles.get(tool, '调用工具 · ' + tool)
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
    activity = {'title': preview(title + (' · ' + source.replace('\n', ' ') if source else ''), 384)}
    if source:
        activity['input'] = source
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
        else:
            text = ''
        if text:
            activity['output'] = preview(text + ('\n（工具输出已截断）' if receipt.truncated else ''), 3072)
    return activity
