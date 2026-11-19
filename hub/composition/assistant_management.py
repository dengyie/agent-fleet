"""Connect normal main-assistant Runs to Hub administration after account setup."""


def wire_assistant_management(app, config, platform, sessions, core, control):
    worker = platform.platform_worker_service
    if worker is None:
        return
    from hub.application.assistant_admin_service import AssistantAdminService
    accounts = app.extensions.get('accounts')
    admin = AssistantAdminService(app.extensions['fleet'],
        authorize=accounts.is_active_admin if accounts is not None else None)
    worker.assistant_admin = admin
    app.extensions['fleet']['services']['assistant_admin'] = admin
    if sessions.session_service is None:
        return
    from hub.application.agent_session_service import AgentSessionService
    from hub.application.session_model_service import SessionModelService

    models = None
    if config.supervisor_enabled and platform.platform_repository is not None:
        models = SessionModelService(platform.platform_repository, platform.model_secrets,
            sessions.session_service, control.supervisor_service,
            authorize=accounts.is_active_admin if accounts is not None else None)
        app.extensions['fleet']['services']['session_models'] = models

    service = AgentSessionService(
        sessions.session_service, observe=core.observe_service,
        supervisor=control.supervisor_service if config.supervisor_enabled else None,
        authorize=accounts.is_active_admin if accounts is not None else None,
        models=models,
    )
    worker.agent_sessions = service
    app.extensions['fleet']['services']['agent_sessions'] = service
