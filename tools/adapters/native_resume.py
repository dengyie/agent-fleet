"""Pure CLI continuation syntax; no process, storage, identity or authorization.

Supervisor proves the session and executes the returned argv. This module owns
vendor flags and the Node script prefix so orchestration is family independent.
"""
from __future__ import annotations

_RESUME_FORBIDDEN_FLAGS = frozenset({"--ephemeral", "--ignore-user-config", "--no-session-persistence"})


def continuation_options(family: str, command: list[str]) -> list[str]:
    """Keep explicit provider/model/policy options, without replaying the prompt.

    These are options supported by the family's resume command. Output paths,
    session selectors and initial-turn attachments must not be replayed.
    """
    valued = {
        'codex': {'-c', '--config', '-m', '--model', '--enable', '--disable'},
        'claude_code': {'--model', '--fallback-model', '--settings', '--setting-sources',
                        '--permission-mode', '--effort', '--agent', '--agents',
                        '--system-prompt', '--append-system-prompt', '--mcp-config'},
        'pi': {'--provider', '--model', '--thinking', '--session-dir', '--tools',
               '--exclude-tools', '--system-prompt', '--append-system-prompt'},
        'zcode': {'--mode', '--surface', '--locale', '--toolsearch-mode', '--workflow-mode'},
        'hermes': {'--model', '-m', '--provider', '--reasoning', '--toolsets', '-t', '--run-budget', '--max-turns'},
    }.get(family, set())
    switches = {
        'codex': {'--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                  '--strict-config', '--ignore-rules', '--approve-for-me'},
        'claude_code': {'--dangerously-skip-permissions', '--bare', '--strict-mcp-config'},
        'pi': {'--offline', '--no-extensions', '--no-mcp', '--no-skills',
               '--no-prompt-templates', '--no-context-files', '--no-tools', '--approve', '--no-approve'},
        'zcode': {'--force-mcs', '--no-color'},
        'hermes': {'--no-restore-cwd', '--ignore-rules', '--safe-mode'},
    }.get(family, set())
    result = []
    start = 1
    # A trusted node launcher needs its original script before any CLI flags.
    executable = command[0].replace('\\', '/').rsplit('/', 1)[-1].lower() if command else ''
    if (executable in {'node', 'node.exe'} and len(command) > 1
            and command[1].lower().endswith(('.js', '.cjs', '.mjs'))):
        result.append(command[1])
        start = 2
    args = iter(command[start:])
    for arg in args:
        if arg == '--':
            break
        flag = arg.split('=', 1)[0]
        if flag in valued:
            if '=' in arg:
                result.append(arg)
            else:
                value = next(args, None)
                if value is not None:
                    result.extend((arg, value))
        elif arg in switches:
            result.append(arg)
    return result


def native_resume_argv(family: str, exe: str, token: str, text: str) -> list[str]:
    """Build the family-specific native resume argv."""
    if not exe or not token or not isinstance(text, str) or not text:
        return []
    # token 以 '-' 开头会被当作选项；text 以 '-' 开头时只有支持 '--' 的
    # CLI（clap / commander）插 '--'，其余拒绝。
    if str(token).startswith("-"):
        return []
    leading_dash = text.startswith("-")
    if family == "codex":
        argv = [exe, "exec", "resume", "--json", token] + (["--", text] if leading_dash else [text])
    elif family == "claude_code":
        argv = [exe, "--print", "--verbose", "--output-format", "stream-json", "--resume", token] + (["--", text] if leading_dash else [text])
    elif family == "pi":
        # --resume is an interactive picker; --session takes path or id.
        argv = [exe, "--print", "--mode", "json", "--session", token] + (["--", text] if leading_dash else [text])
    elif family == 'zcode':
        if leading_dash:
            return []
        argv = [exe, '--json', '--resume', token, '-p', text]
    elif family == 'hermes':
        if leading_dash:
            return []
        argv = [exe, 'chat', '--quiet', '--resume', token, '--query', text]
    else:
        return []
    if any(flag in argv for flag in _RESUME_FORBIDDEN_FLAGS):
        return []
    return argv



def build_resume_argv(family: str, exe: str, token: str, text: str,
                      options=(), *, resume_builder=None) -> list[str]:
    """Compose retained adapter options without interpreting prompt or shell text."""
    builder = resume_builder or native_resume_argv
    argv = builder(family, exe, token, text)
    if not argv:
        return []
    options = list(options)
    script = []
    if (exe.replace('\\', '/').rsplit('/', 1)[-1].lower() in {'node', 'node.exe'}
            and options and options[0].lower().endswith(('.js', '.cjs', '.mjs'))):
        script, options = options[:1], options[1:]
    at = 2 if family in {'codex', 'hermes'} else 1
    argv[at:at] = options
    argv[1:1] = script
    return argv
