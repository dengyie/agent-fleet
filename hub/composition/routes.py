"""Accounts, blueprints, request hooks and error handlers.

Registration order is part of the HTTP contract (route precedence, hook and
error-handler order); keep new registrations appended in place.
"""


def register_http(app, config, platform, sessions, control):
    """Wire accounts, then every gated blueprint, then hooks/error handlers."""
    from hub.accounts.http import init_accounts
    init_accounts(app, config)

    _register_blueprints(app, config, platform, sessions, control)

    # 每个请求在 transport 边界打上不透明 request_id（错误响应携带）
    from hub.http.errors import attach_request_id

    app.before_request(attach_request_id)

    # 统一 HTTP 错误契约：/api/* 的 404/405/意外异常使用有界 JSON，
    # 页面路由保留兼容 plain-text。
    from hub.http.errors import register_error_handlers

    register_error_handlers(app)


def _register_blueprints(app, config, platform, sessions, control):
    from hub.http.command_routes import bp as commands_bp
    from hub.http.observe_routes import bp as observe_bp
    from hub.http.operator_routes import bp as operator_bp
    from hub.http.pages import bp as pages_bp
    from hub.http.task_routes import bp as tasks_bp
    from hub.http.v1.observe_routes import bp as observe_v1_bp
    from hub.http.v1.task_routes import bp as tasks_v1_bp

    app.register_blueprint(operator_bp)
    app.register_blueprint(observe_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(pages_bp)
    # 版本化 /api/v1 兼容表面：复用旧 view，旧 /api/* 仍权威。
    app.register_blueprint(observe_v1_bp)
    app.register_blueprint(tasks_v1_bp)
    if platform.platform_service is not None:
        _register_platform_blueprints(app, config, platform)

    # Task 6: session 蓝图仅在显式启用时注册。禁用（默认）不注册任何 session
    # 路由，旧 observation/task/runner/SSE 行为与 startup 完全不变。
    if sessions.session_service is not None:
        from hub.http.session_routes import bp as session_bp
        from hub.http.v1.session_routes import bp as session_v1_bp

        app.register_blueprint(session_bp)
        app.register_blueprint(session_v1_bp)

    # Task 9: supervisor 蓝图仅在显式启用时注册（无 503/404 歧义，旧 surface 不变）。
    # 有签名 key 时 supervisor_service 才真正签发命令；无 key = fail-closed。
    if app.config["SUPERVISOR_ENABLED"]:
        from hub.http.supervisor_routes import bp as supervisor_bp

        app.register_blueprint(supervisor_bp)

    # Task 6: adoption 蓝图仅在显式启用时注册（无 503/404 歧义，旧 surface 不变）。
    # ``adoption_service`` is None unless ``adoption_repositories_enabled`` so a
    # disabled flag never exposes the operator adoption surface.
    if control.adoption_service is not None:
        from hub.http.adoption_routes import bp as adoption_bp

        app.register_blueprint(adoption_bp)

    # Round 7: 节点客户端发布（runner 凭据 + 清单签名）。总是注册；无签名 key 时 503。
    from hub.http.client_release_routes import bp as client_release_bp

    app.register_blueprint(client_release_bp)


def _register_platform_blueprints(app, config, platform):
    from hub.http.platform_routes import bp as platform_bp
    from hub.http.conversation_routes import bp as conversation_bp
    from hub.http.node_routes import bp as node_bp
    from hub.http.artifact_routes import bp as artifact_bp
    from hub.http.legacy_task_routes import bp as legacy_task_bp
    from hub.http.execution_window_routes import bp as execution_window_bp
    app.register_blueprint(platform_bp)
    from hub.http.muse_routes import bp as muse_bp
    from hub.application.muse_service import MuseService
    from hub.infrastructure.muse_repository import MuseRepository
    muse_accounts = app.extensions.get('accounts')
    muse_service = MuseService(MuseRepository(config.state_dir / 'muse'),
                               owner_enabled=muse_accounts.is_active if muse_accounts else None,
                               owner_operation=muse_accounts.background_operation if muse_accounts else None)
    app.extensions['fleet']['services']['muse'] = muse_service
    # create_app 不派生线程：已有 Muse 持久化时的自动审批 worker 由
    # start_background_jobs 启动；运行期操作员显式开启时服务自行按需启动。
    app.register_blueprint(muse_bp)
    app.register_blueprint(conversation_bp)
    app.register_blueprint(node_bp)
    app.register_blueprint(artifact_bp)
    app.register_blueprint(legacy_task_bp)
    # Keep a stable 404 transport while the execution-window gate is
    # closed; the service/repository are still absent in that mode.
    app.register_blueprint(execution_window_bp)
    if platform.service_health is not None:
        from hub.http.service_routes import bp as service_bp
        app.register_blueprint(service_bp)
        from hub.http.incident_routes import bp as incident_bp
        app.register_blueprint(incident_bp)
        # Register the bounded transport even while the gate is closed so
        # an attempted action receives the stable 404 contract rather than
        # Flask's method-not-allowed response caused by the service detail
        # route sharing the same prefix.
        from hub.http.service_action_routes import bp as service_action_bp
        app.register_blueprint(service_action_bp)
    if platform.platform_schedule_service is not None:
        from hub.http.schedule_routes import bp as schedule_bp
        app.register_blueprint(schedule_bp)
    if platform.platform_memory_service is not None:
        from hub.http.memory_routes import bp as memory_bp
        app.register_blueprint(memory_bp)
