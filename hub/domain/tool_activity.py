"""Readable public activity from known tool fields, separate from execution."""
import shlex


def preview(value, maximum=2048):
    if not isinstance(value, str):
        return ''
    raw = value.encode('utf-8')
    return raw[:maximum].decode('utf-8', errors='ignore') + ('\n…（预览已截断）' if len(raw) > maximum else '')


def tool_activity(tool, arguments, receipt=None):
    arguments = arguments if isinstance(arguments, dict) else {}
    titles = {'workspace.exec': '执行命令', 'workspace.read': '读取文件',
              'workspace.list': '列出目录', 'workspace.write': '写入文件',
              'workspace.digest': '计算文件摘要', 'workspace.artifact': '保存产物',
              'platform.get_defaults': '查看默认配置', 'fleet.list_services': '列出服务',
              'service.inspect': '检查服务', 'service.logs': '读取服务日志',
              'conversation.list': '查找会话', 'conversation.rename': '重命名会话',
              'browser.open': '打开网页', 'browser.close': '关闭网页'}
    title = titles.get(tool, '调用工具 · ' + tool)
    source = ''
    if tool == 'workspace.exec':
        argv = arguments.get('argv')
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            source = preview(shlex.join(argv), 4096)
    elif tool.startswith('workspace.'):
        source = preview(arguments.get('path') or '.', 1024)
    elif tool in {'service.inspect', 'service.logs'}:
        source = preview(arguments.get('service_id'), 512)
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
        else:
            text = ''
        if text:
            activity['output'] = preview(text + ('\n（工具输出已截断）' if receipt.truncated else ''), 3072)
    return activity
